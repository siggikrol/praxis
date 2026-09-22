from __future__ import annotations

from .resilience import catalog_refresh, manual_refresh

import logging
import random
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .common import (
    AwsCatalogMeta,
    BackgroundRefreshRegistry,
    acquire_lock,
    build_optgroups,
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

_ELASTICACHE_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 3, "mode": "standard"},
)

_SCHEMA_VERSION = 1
_CATALOG = "redis_node_types"
_CACHE_STEM = "redis_node_types"

_GROUP_ORDER = [
    "Burstable",
    "General Purpose",
    "Memory Optimized",
    "Other",
]


def _ttl_seconds() -> int:
    return ttl_seconds("PS_REDIS_NODE_TYPES_TTL_SECONDS")


def _cache_dir() -> str:
    return cache_dir("PS_REDIS_NODE_TYPES_CACHE_DIR")


def _cache_paths(region: str) -> tuple[str, str]:
    return cache_paths(cache_dir_path=_cache_dir(), cache_stem=_CACHE_STEM, region=region)


def _fetch_cache_node_types(region: str) -> list[str]:
    session = boto3.session.Session(region_name=region)
    ec = session.client("elasticache", config=_ELASTICACHE_CLIENT_CONFIG)

    items: list[str] = []
    marker: str | None = None
    while True:
        # ElastiCache doesn't expose an API that directly lists *all* cache node
        # types. The most reliable way to enumerate the available node types is
        # to query the offerings and extract the CacheNodeType field.
        kwargs: dict[str, Any] = {"MaxRecords": 100, "ProductDescription": "redis"}
        if marker:
            kwargs["Marker"] = marker
        resp = ec.describe_reserved_cache_nodes_offerings(**kwargs)
        for entry in resp.get("ReservedCacheNodesOfferings", []) or []:
            typ = entry.get("CacheNodeType")
            if typ:
                items.append(str(typ))
        marker = resp.get("Marker")
        if not marker:
            break

    return sorted(set(items))


@manual_refresh
def refresh_redis_node_type_cache(region: str | None = None) -> dict[str, Any]:
    region = normalize_region(region)
    cache_path, lock_path = _cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        return _refresh_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


@catalog_refresh
def _refresh_unlocked(region: str, cache_path: str) -> dict[str, Any]:
    try:
        types = _fetch_cache_node_types(region)
        groups = build_optgroups(types, group_order=_GROUP_ORDER)
        payload: dict[str, Any] = {
            "schema": _SCHEMA_VERSION,
            "generated_at": time.time(),
            "region": region,
            "groups": [[label, [v for v, _ in opts]] for label, opts in groups],
            "count": len(flatten_choices(groups)),
        }
        write_json_atomic(cache_path, payload)
        return {
            "ok": True,
            "region": region,
            "cache_path": cache_path,
            "count": payload["count"],
            "generated_at": payload["generated_at"],
        }
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Redis node types: refresh failed region=%s: %s", region, exc)
        return {"ok": False, "region": region, "cache_path": cache_path, "error": str(exc)}
    except Exception as exc:
        logger.exception("Redis node types: refresh crashed region=%s", region)
        return {"ok": False, "region": region, "cache_path": cache_path, "error": str(exc)}


def refresh_redis_node_type_cache_if_stale(
    region: str | None = None, *, ttl_seconds_override: int | None = None
) -> dict[str, Any]:
    region = normalize_region(region)
    ttl = int(ttl_seconds_override) if isinstance(ttl_seconds_override, int) and ttl_seconds_override > 0 else _ttl_seconds()

    cache_path, lock_path = _cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        payload = read_json(cache_path)
        generated_at = payload_generated_at(payload)
        if generated_at and not is_stale(generated_at, ttl):
            return {"ok": True, "skipped": True, "region": region, "cache_path": cache_path, "generated_at": generated_at}
        return _refresh_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


def _load_cached_groups(
    region: str,
) -> tuple[list[tuple[str, list[tuple[str, str]]]] | None, AwsCatalogMeta | None]:
    cache_path, _ = _cache_paths(region)
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
        catalog=_CATALOG,
        region=region,
        cache_path=cache_path,
        generated_at=generated_at,
        stale=stale,
        count=len(flatten_choices(groups)),
        source="aws_cache",
        error=None,
    )
    return groups, meta


def _spawn_refresh_thread(region: str) -> bool:
    region = normalize_region(region)

    def _runner() -> None:
        time.sleep(random.uniform(0.1, 1.5))
        try:
            cache_path, lock_path = _cache_paths(region)
            lock_fh = acquire_lock(lock_path, blocking=False)
            if lock_fh is None:
                return
            try:
                payload = read_json(cache_path)
                generated_at = payload_generated_at(payload)
                if not is_stale(generated_at, _ttl_seconds()):
                    return
                _refresh_unlocked(region, cache_path)
            finally:
                release_lock(lock_fh)
        except Exception:
            logger.exception("Redis node types: background refresh crashed region=%s", region)

    return BackgroundRefreshRegistry.spawn(f"redis-node-types-refresh:{region}", _runner)


def get_redis_node_type_options(
    *,
    region: str | None = None,
    fallback_groups: list[tuple[str, list[tuple[str, str]]]] | None = None,
    fallback_choices: list[tuple[str, str]] | None = None,
    refresh_async_if_stale: bool = True,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]], AwsCatalogMeta]:
    region = normalize_region(region)

    groups, meta = _load_cached_groups(region)
    if groups and meta:
        if meta.stale and refresh_async_if_stale:
            _spawn_refresh_thread(region)
        return groups, flatten_choices(groups), meta

    if refresh_async_if_stale:
        _spawn_refresh_thread(region)

    fg = fallback_groups or []
    fc = fallback_choices or flatten_choices(fg)
    if not fg and fc:
        fg = [("Node Types", [(v, v) for v, _ in fc])]

    cache_path, _ = _cache_paths(region)
    meta = AwsCatalogMeta(
        catalog=_CATALOG,
        region=region,
        cache_path=cache_path,
        generated_at=None,
        stale=True,
        count=len(fc),
        source="fallback",
        error=None,
    )
    return fg, fc, meta
