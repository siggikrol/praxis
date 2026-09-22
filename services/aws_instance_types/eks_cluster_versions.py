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

_EKS_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 3, "mode": "standard"},
)

_SCHEMA_VERSION = 1
_CATALOG = "eks_cluster_versions"
_CACHE_STEM = "eks_cluster_versions"

_GROUP_ORDER = [
    "Standard Support",
    "Extended Support",
]


def _ttl_seconds() -> int:
    return ttl_seconds("PS_EKS_CLUSTER_VERSIONS_TTL_SECONDS")


def _cache_dir() -> str:
    return cache_dir("PS_EKS_CLUSTER_VERSIONS_CACHE_DIR")


def _cache_paths(region: str) -> tuple[str, str]:
    return cache_paths(cache_dir_path=_cache_dir(), cache_stem=_CACHE_STEM, region=region)


def _version_key(v: str) -> tuple[int, int, int, str]:
    """
    Sort key for Kubernetes versions like "1.35" (descending preferred).
    """
    s = (v or "").strip()
    parts = s.split(".")
    nums: list[int] = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except Exception:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2], s)


def _status_label(raw_status: str) -> str | None:
    s = (raw_status or "").strip()
    if not s:
        return None

    # Newer field (DescribeClusterVersions): STANDARD_SUPPORT | EXTENDED_SUPPORT | UNSUPPORTED
    up = s.upper()
    if "STANDARD" in up and "SUPPORT" in up and "EXTENDED" not in up:
        return "Standard Support"
    if "EXTENDED" in up and "SUPPORT" in up:
        return "Extended Support"

    # Deprecated field: standard-support | extended-support | unsupported
    low = s.lower()
    if low == "standard-support":
        return "Standard Support"
    if low == "extended-support":
        return "Extended Support"

    return None


def _fetch_cluster_versions(region: str) -> list[dict[str, Any]]:
    session = boto3.session.Session(region_name=region)
    eks = session.client("eks", config=_EKS_CLIENT_CONFIG)

    items: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100, "includeAll": True}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = eks.describe_cluster_versions(**kwargs)
        for entry in resp.get("clusterVersions", []) or []:
            if isinstance(entry, dict):
                items.append(entry)
        next_token = resp.get("nextToken")
        if not next_token:
            break

    return items


def _build_groups(cluster_versions: list[dict[str, Any]]) -> list[tuple[str, list[tuple[str, str]]]]:
    buckets: dict[str, list[str]] = {label: [] for label in _GROUP_ORDER}
    for entry in cluster_versions:
        if not isinstance(entry, dict):
            continue

        v = (entry.get("clusterVersion") or "").strip()
        if not v:
            continue

        # Exclude Outposts cluster-only versions if present.
        ct = (entry.get("clusterType") or "").strip().lower()
        if "outpost" in ct:
            continue

        raw_status = entry.get("versionStatus") or entry.get("status") or ""
        label = _status_label(str(raw_status))
        if label not in buckets:
            continue
        buckets[label].append(v)

    out: list[tuple[str, list[tuple[str, str]]]] = []
    for label in _GROUP_ORDER:
        versions = sorted(set(buckets.get(label) or []), key=_version_key, reverse=True)
        if versions:
            out.append((label, [(v, v) for v in versions]))
    return out


@manual_refresh
def refresh_eks_cluster_versions_cache(region: str | None = None) -> dict[str, Any]:
    """
    Force-refresh the EKS cluster-version cache for a region.

    Returns a small summary dict suitable for logging/UI.
    """
    region = normalize_region(region)
    cache_path, lock_path = _cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        return _refresh_eks_cluster_versions_cache_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


@catalog_refresh
def _refresh_eks_cluster_versions_cache_unlocked(region: str, cache_path: str) -> dict[str, Any]:
    try:
        versions = _fetch_cluster_versions(region)
        groups = _build_groups(versions)
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
        logger.warning("EKS cluster versions: refresh failed region=%s: %s", region, exc)
        return {
            "ok": False,
            "region": region,
            "cache_path": cache_path,
            "error": str(exc),
        }
    except Exception as exc:
        logger.exception("EKS cluster versions: refresh crashed region=%s", region)
        return {
            "ok": False,
            "region": region,
            "cache_path": cache_path,
            "error": str(exc),
        }


def refresh_eks_cluster_versions_cache_if_stale(
    region: str | None = None, *, ttl_seconds: int | None = None
) -> dict[str, Any]:
    """
    Refresh the cache only if it is stale.

    This re-checks staleness under a file lock so multiple gunicorn workers don't
    sequentially hammer AWS when the TTL expires.
    """
    region = normalize_region(region)
    ttl = int(ttl_seconds) if isinstance(ttl_seconds, int) and ttl_seconds > 0 else _ttl_seconds()

    cache_path, lock_path = _cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        payload = read_json(cache_path)
        generated_at = payload_generated_at(payload)
        if generated_at and not is_stale(generated_at, ttl):
            return {
                "ok": True,
                "region": region,
                "cache_path": cache_path,
                "skipped": True,
                "generated_at": generated_at,
            }
        return _refresh_eks_cluster_versions_cache_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


def _load_cached_groups(region: str) -> tuple[list[tuple[str, list[tuple[str, str]]]] | None, AwsCatalogMeta | None]:
    cache_path, _lock_path = _cache_paths(region)
    payload = read_json(cache_path)
    if not payload:
        return None, None
    if payload.get("schema") != _SCHEMA_VERSION:
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
    )
    return groups, meta


def _spawn_refresh_thread(region: str) -> bool:
    region = normalize_region(region)

    def _runner() -> None:
        # Try to avoid all workers syncing at the same instant.
        time.sleep(random.uniform(0.1, 1.5))
        try:
            # Non-blocking: if another worker is already refreshing, just skip.
            cache_path, lock_path = _cache_paths(region)
            lock_fh = acquire_lock(lock_path, blocking=False)
            if lock_fh is None:
                return
            try:
                payload = read_json(cache_path)
                generated_at = payload_generated_at(payload)
                if not is_stale(generated_at, _ttl_seconds()):
                    return
                _refresh_eks_cluster_versions_cache_unlocked(region, cache_path)
            finally:
                release_lock(lock_fh)
        except Exception:
            logger.exception("EKS cluster versions: background refresh crashed region=%s", region)

    return BackgroundRefreshRegistry.spawn(f"eks-cluster-versions-refresh:{region}", _runner)


def get_eks_cluster_version_options(
    *,
    region: str | None = None,
    fallback_groups: list[tuple[str, list[tuple[str, str]]]] | None = None,
    fallback_choices: list[tuple[str, str]] | None = None,
    refresh_async_if_stale: bool = True,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]], AwsCatalogMeta]:
    """
    Return (optgroup choices, flat choices, meta) for EKS cluster versions.

    The list is sourced from a cached AWS EKS DescribeClusterVersions fetch.
    If the cache is missing or invalid, this falls back to the provided static lists.

    If the cache is stale and refresh_async_if_stale is True, a refresh is started
    in the background (best-effort) while still serving the cached values.
    """
    region = normalize_region(region)
    groups, meta = _load_cached_groups(region)

    if groups and meta:
        if meta.stale and refresh_async_if_stale:
            _spawn_refresh_thread(region)
        choices = flatten_choices(groups)
        return groups, choices, meta

    # No cache available. Start a background refresh, but keep UI responsive.
    if refresh_async_if_stale:
        _spawn_refresh_thread(region)

    fg = fallback_groups or []
    fc = fallback_choices or flatten_choices(fg)

    # Ensure a sane shape for WTForms even if fallback is missing/empty.
    if not fg and fc:
        fg = [("Cluster Versions", [(v, v) for v, _ in fc])]

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
