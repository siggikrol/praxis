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

_EC2_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 3, "mode": "standard"},
)

_SCHEMA_VERSION = 1
_CATALOG = "eks_instance_types"
_CACHE_STEM = "eks_instance_types"

_GROUP_ORDER = [
    "Burstable",
    "General Purpose",
    "Compute Optimized",
    "Memory Optimized",
    "GPU / Accelerated",
    "Storage Optimized",
    "Other",
]

_ARM_FAMILY_RE = re.compile(r"^(?:a1|[a-z]+\d+[a-z0-9]*g[a-z0-9]*)\.")


def _is_x86_instance_type(instance_type: str) -> bool:
    return not _ARM_FAMILY_RE.match(str(instance_type or "").strip().lower())


def _filter_x86_groups(
    groups: list[tuple[str, list[tuple[str, str]]]],
) -> list[tuple[str, list[tuple[str, str]]]]:
    filtered: list[tuple[str, list[tuple[str, str]]]] = []
    for label, options in groups:
        kept = [
            (value, text)
            for value, text in options
            if _is_x86_instance_type(value)
        ]
        if kept:
            filtered.append((label, kept))
    return filtered


def _ttl_seconds() -> int:
    return ttl_seconds("PS_EKS_INSTANCE_TYPES_TTL_SECONDS")


def _cache_dir() -> str:
    return cache_dir("PS_EKS_INSTANCE_TYPES_CACHE_DIR")


def _cache_paths(region: str) -> tuple[str, str]:
    return cache_paths(cache_dir_path=_cache_dir(), cache_stem=_CACHE_STEM, region=region)


def _fetch_ec2_instance_types(region: str) -> list[str]:
    session = boto3.session.Session(region_name=region)
    ec2 = session.client("ec2", config=_EC2_CLIENT_CONFIG)

    items: list[str] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"MaxResults": 100}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = ec2.describe_instance_types(**kwargs)
        for entry in resp.get("InstanceTypes", []) or []:
            it = entry.get("InstanceType")
            architectures = entry.get("ProcessorInfo", {}).get("SupportedArchitectures") or []
            if it and "x86_64" in architectures:
                items.append(str(it))
        next_token = resp.get("NextToken")
        if not next_token:
            break

    return sorted(set(items))


@manual_refresh
def refresh_eks_instance_type_cache(region: str | None = None) -> dict[str, Any]:
    """
    Force-refresh the EKS instance-type cache for a region.

    Returns a small summary dict suitable for logging/UI.
    """
    region = normalize_region(region)
    cache_path, lock_path = _cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        return _refresh_eks_instance_type_cache_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


@catalog_refresh
def _refresh_eks_instance_type_cache_unlocked(region: str, cache_path: str) -> dict[str, Any]:
    try:
        instance_types = [
            it for it in _fetch_ec2_instance_types(region) if _is_x86_instance_type(it)
        ]
        groups = build_optgroups(instance_types, group_order=_GROUP_ORDER)
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
        logger.warning("EKS instance types: refresh failed region=%s: %s", region, exc)
        return {
            "ok": False,
            "region": region,
            "cache_path": cache_path,
            "error": str(exc),
        }
    except Exception as exc:
        logger.exception("EKS instance types: refresh crashed region=%s", region)
        return {
            "ok": False,
            "region": region,
            "cache_path": cache_path,
            "error": str(exc),
        }


def refresh_eks_instance_type_cache_if_stale(
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
        return _refresh_eks_instance_type_cache_unlocked(region, cache_path)
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
                _refresh_eks_instance_type_cache_unlocked(region, cache_path)
            finally:
                release_lock(lock_fh)
        except Exception:
            logger.exception("EKS instance types: background refresh crashed region=%s", region)

    return BackgroundRefreshRegistry.spawn(f"eks-instance-types-refresh:{region}", _runner)


def get_eks_instance_type_options(
    *,
    region: str | None = None,
    fallback_groups: list[tuple[str, list[tuple[str, str]]]] | None = None,
    fallback_choices: list[tuple[str, str]] | None = None,
    refresh_async_if_stale: bool = True,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]], AwsCatalogMeta]:
    """
    Return (optgroup choices, flat choices, meta) for EKS instance types.

    The list is sourced from a cached AWS EC2 DescribeInstanceTypes fetch.
    If the cache is missing or invalid, this falls back to the provided static lists.

    If the cache is stale and refresh_async_if_stale is True, a refresh is started
    in the background (best-effort) while still serving the cached values.
    """
    region = normalize_region(region)
    groups, meta = _load_cached_groups(region)

    if groups and meta:
        groups = _filter_x86_groups(groups)
        if meta.stale and refresh_async_if_stale:
            _spawn_refresh_thread(region)
        choices = flatten_choices(groups)
        return groups, choices, meta

    # No cache available. Start a background refresh, but keep UI responsive.
    if refresh_async_if_stale:
        _spawn_refresh_thread(region)

    fg = _filter_x86_groups(fallback_groups or [])
    fc = [
        (value, text)
        for value, text in (fallback_choices or flatten_choices(fg))
        if _is_x86_instance_type(value)
    ]

    # Ensure a sane shape for WTForms even if fallback is missing/empty.
    if not fg and fc:
        fg = [("Instance Types", [(v, v) for v, _ in fc])]

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
