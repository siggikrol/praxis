from __future__ import annotations

from .resilience import catalog_refresh, manual_refresh

import logging
import random
import re
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .common import (
    AwsCatalogMeta,
    BackgroundRefreshRegistry,
    acquire_lock,
    cache_dir,
    cache_paths,
    flatten_choices,
    is_stale,
    normalize_region,
    payload_generated_at,
    read_json,
    release_lock,
    ttl_seconds,
    write_json_atomic,
)


logger = logging.getLogger(__name__)

_RDS_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=30,
    retries={"max_attempts": 3, "mode": "standard"},
)

_SCHEMA_VERSION = 1
_CATALOG_BASE = "aurora_engine_versions"
_CACHE_STEM_BASE = "aurora_engine_versions"
_DEFAULT_ENGINE = "aurora-postgresql"

_GROUP_ORDER = [
    "Available",
    "Deprecated",
    "Other",
]


def _ttl_seconds() -> int:
    return ttl_seconds("PS_AURORA_ENGINE_VERSIONS_TTL_SECONDS")


def _cache_dir() -> str:
    return cache_dir("PS_AURORA_ENGINE_VERSIONS_CACHE_DIR")


def _safe_engine(engine: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (engine or "").strip().lower()) or "unknown"


def _cache_stem(engine: str) -> str:
    return f"{_CACHE_STEM_BASE}.{_safe_engine(engine)}"


def _cache_paths(engine: str, region: str) -> tuple[str, str]:
    return cache_paths(cache_dir_path=_cache_dir(), cache_stem=_cache_stem(engine), region=region)


def _status_group(status: str) -> str:
    s = (status or "").strip().lower()
    if not s:
        return "Other"
    if "deprecat" in s or "unsupport" in s:
        return "Deprecated"
    if "avail" in s or "active" in s:
        return "Available"
    return "Other"


def _version_key(v: str) -> tuple:
    """
    Sort mixed Aurora version strings descending by numeric tokens first.
    Examples:
      - 15.10
      - 8.0.mysql_aurora.3.08.2
    """
    nums = [int(x) for x in re.findall(r"\d+", (v or "").strip())]
    nums = nums[:10]
    if len(nums) < 10:
        nums.extend([0] * (10 - len(nums)))
    return tuple(nums) + ((v or "").strip().lower(),)


def _fetch_engine_versions(region: str, engine: str) -> list[tuple[str, str]]:
    session = boto3.session.Session(region_name=region)
    rds = session.client("rds", config=_RDS_CLIENT_CONFIG)

    out: list[tuple[str, str]] = []
    marker: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "Engine": engine,
            "MaxRecords": 100,
            "DefaultOnly": False,
        }
        if marker:
            kwargs["Marker"] = marker
        resp = rds.describe_db_engine_versions(**kwargs)
        for entry in resp.get("DBEngineVersions", []) or []:
            if not isinstance(entry, dict):
                continue
            version = str(entry.get("EngineVersion") or "").strip()
            status = str(entry.get("Status") or "").strip()
            if version:
                out.append((version, status))
        marker = resp.get("Marker")
        if not marker:
            break
    return out


def _build_groups(rows: list[tuple[str, str]]) -> list[tuple[str, list[tuple[str, str]]]]:
    buckets: dict[str, list[str]] = {label: [] for label in _GROUP_ORDER}
    for version, status in rows:
        buckets[_status_group(status)].append(version)

    out: list[tuple[str, list[tuple[str, str]]]] = []
    for label in _GROUP_ORDER:
        versions = sorted(set(buckets.get(label) or []), key=_version_key, reverse=True)
        if versions:
            out.append((label, [(v, v) for v in versions]))

    if out:
        return out

    versions = sorted({v for v, _ in rows}, key=_version_key, reverse=True)
    if versions:
        return [("Engine Versions", [(v, v) for v in versions])]
    return []


@manual_refresh
def refresh_aurora_engine_version_cache(
    region: str | None = None, *, engine: str | None = None
) -> dict[str, Any]:
    region = normalize_region(region)
    engine = (engine or _DEFAULT_ENGINE).strip() or _DEFAULT_ENGINE
    cache_path, lock_path = _cache_paths(engine, region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        return _refresh_unlocked(region, engine, cache_path)
    finally:
        release_lock(lock_fh)


@catalog_refresh
def _refresh_unlocked(region: str, engine: str, cache_path: str) -> dict[str, Any]:
    try:
        rows = _fetch_engine_versions(region, engine)
        groups = _build_groups(rows)
        payload: dict[str, Any] = {
            "schema": _SCHEMA_VERSION,
            "generated_at": time.time(),
            "region": region,
            "engine": engine,
            "groups": [[label, [v for v, _ in opts]] for label, opts in groups],
            "count": len(flatten_choices(groups)),
        }
        write_json_atomic(cache_path, payload)
        return {
            "ok": True,
            "region": region,
            "engine": engine,
            "cache_path": cache_path,
            "count": payload["count"],
            "generated_at": payload["generated_at"],
        }
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Aurora engine versions: refresh failed region=%s engine=%s: %s", region, engine, exc)
        return {"ok": False, "region": region, "engine": engine, "cache_path": cache_path, "error": str(exc)}
    except Exception as exc:
        logger.exception("Aurora engine versions: refresh crashed region=%s engine=%s", region, engine)
        return {"ok": False, "region": region, "engine": engine, "cache_path": cache_path, "error": str(exc)}


def refresh_aurora_engine_version_cache_if_stale(
    region: str | None = None, *, engine: str | None = None, ttl_seconds_override: int | None = None
) -> dict[str, Any]:
    region = normalize_region(region)
    engine = (engine or _DEFAULT_ENGINE).strip() or _DEFAULT_ENGINE
    ttl = int(ttl_seconds_override) if isinstance(ttl_seconds_override, int) and ttl_seconds_override > 0 else _ttl_seconds()

    cache_path, lock_path = _cache_paths(engine, region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        payload = read_json(cache_path)
        generated_at = payload_generated_at(payload)
        if generated_at and not is_stale(generated_at, ttl):
            return {"ok": True, "skipped": True, "region": region, "engine": engine, "cache_path": cache_path, "generated_at": generated_at}
        return _refresh_unlocked(region, engine, cache_path)
    finally:
        release_lock(lock_fh)


def _load_cached_groups(
    region: str, engine: str
) -> tuple[list[tuple[str, list[tuple[str, str]]]] | None, AwsCatalogMeta | None]:
    cache_path, _ = _cache_paths(engine, region)
    payload = read_json(cache_path)
    if not payload or payload.get("schema") != _SCHEMA_VERSION:
        return None, None

    generated_at = payload_generated_at(payload)
    stale = is_stale(generated_at, _ttl_seconds())

    raw_groups = payload.get("groups")
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    if isinstance(raw_groups, list):
        for entry in raw_groups:
            if (
                isinstance(entry, (list, tuple))
                and len(entry) == 2
                and isinstance(entry[0], str)
                and isinstance(entry[1], list)
            ):
                label = entry[0]
                opts = [(str(v), str(v)) for v in entry[1] if str(v).strip()]
                if opts:
                    groups.append((label, opts))

    if not groups:
        return None, None

    meta = AwsCatalogMeta(
        catalog=f"{_CATALOG_BASE}:{engine}",
        region=region,
        cache_path=cache_path,
        generated_at=generated_at,
        stale=stale,
        count=len(flatten_choices(groups)),
        source="aws_cache",
        error=None,
    )
    return groups, meta


def _spawn_refresh_thread(region: str, engine: str, *, force: bool = False) -> bool:
    region = normalize_region(region)
    engine = (engine or _DEFAULT_ENGINE).strip() or _DEFAULT_ENGINE

    def _runner() -> None:
        time.sleep(random.uniform(0.1, 1.5))
        try:
            cache_path, lock_path = _cache_paths(engine, region)
            lock_fh = acquire_lock(lock_path, blocking=False)
            if lock_fh is None:
                return
            try:
                payload = read_json(cache_path)
                generated_at = payload_generated_at(payload)
                if not force and not is_stale(generated_at, _ttl_seconds()):
                    return
                _refresh_unlocked(region, engine, cache_path)
            finally:
                release_lock(lock_fh)
        except Exception:
            logger.exception("Aurora engine versions: background refresh crashed region=%s engine=%s", region, engine)

    key = f"aurora-engine-versions-refresh:{_safe_engine(engine)}:{region}"
    return BackgroundRefreshRegistry.spawn(key, _runner)


def refresh_aurora_engine_version_cache_async(
    region: str | None = None, *, engine: str | None = None
) -> bool:
    """Start a forced refresh without tying up an HTTP request thread."""
    return _spawn_refresh_thread(
        normalize_region(region),
        (engine or _DEFAULT_ENGINE).strip() or _DEFAULT_ENGINE,
        force=True,
    )


def get_aurora_engine_version_options(
    *,
    region: str | None = None,
    engine: str | None = None,
    fallback_groups: list[tuple[str, list[tuple[str, str]]]] | None = None,
    fallback_choices: list[tuple[str, str]] | None = None,
    refresh_async_if_stale: bool = True,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]], AwsCatalogMeta]:
    region = normalize_region(region)
    engine = (engine or _DEFAULT_ENGINE).strip() or _DEFAULT_ENGINE

    groups, meta = _load_cached_groups(region, engine)
    if groups and meta:
        if meta.stale and refresh_async_if_stale:
            _spawn_refresh_thread(region, engine)
        return groups, flatten_choices(groups), meta

    if refresh_async_if_stale:
        _spawn_refresh_thread(region, engine)

    fg = fallback_groups or []
    fc = fallback_choices or flatten_choices(fg)
    if not fg and fc:
        fg = [("Engine Versions", [(v, v) for v, _ in fc])]

    cache_path, _ = _cache_paths(engine, region)
    meta = AwsCatalogMeta(
        catalog=f"{_CATALOG_BASE}:{engine}",
        region=region,
        cache_path=cache_path,
        generated_at=None,
        stale=True,
        count=len(fc),
        source="fallback",
        error=None,
    )
    return fg, fc, meta
