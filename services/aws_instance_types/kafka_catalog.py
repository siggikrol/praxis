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

_KAFKA_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 3, "mode": "standard"},
)

_EC2_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 3, "mode": "standard"},
)

_SCHEMA_VERSION = 1
_VERSION_CATALOG = "kafka_versions"
_VERSION_CACHE_STEM = "kafka_versions"
_BROKER_TYPE_CATALOG = "kafka_broker_node_instance_types"
_BROKER_TYPE_CACHE_STEM = "kafka_broker_node_instance_types"

_BROKER_TYPE_GROUP_ORDER = [
    "Burstable",
    "General Purpose",
    "Compute Optimized",
    "Memory Optimized",
    "GPU / Accelerated",
    "Storage Optimized",
    "Other",
]

# Conservative broker node type set commonly used with AWS MSK.
_DEFAULT_BROKER_NODE_INSTANCE_TYPES = [
    "kafka.m5.large",
    "kafka.m5.xlarge",
    "kafka.m5.2xlarge",
    "kafka.m7g.large",
    "kafka.m7g.xlarge",
    "kafka.m7g.2xlarge",
]


def _ttl_seconds() -> int:
    return ttl_seconds("PS_KAFKA_CATALOG_TTL_SECONDS")


def _cache_dir() -> str:
    return cache_dir("PS_KAFKA_CATALOG_CACHE_DIR")


def _version_cache_paths(region: str) -> tuple[str, str]:
    return cache_paths(cache_dir_path=_cache_dir(), cache_stem=_VERSION_CACHE_STEM, region=region)


def _broker_type_cache_paths(region: str) -> tuple[str, str]:
    return cache_paths(cache_dir_path=_cache_dir(), cache_stem=_BROKER_TYPE_CACHE_STEM, region=region)


def _version_key(v: str) -> tuple[int, int, int, str]:
    # Sort semantic-ish versions like "3.8.1" descending.
    parts = [p.strip() for p in (v or "").split(".") if p.strip()]
    nums: list[int] = []
    for p in parts[:3]:
        try:
            nums.append(int(p))
        except Exception:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2], (v or "").strip()


def _status_group(status: str) -> str:
    s = (status or "").strip().lower()
    if not s:
        return "Other"
    if "active" in s or "available" in s or "current" in s:
        return "Active"
    if "deprecat" in s or "unsupport" in s:
        return "Deprecated"
    return "Other"


def _fetch_kafka_versions(region: str) -> list[tuple[str, str]]:
    session = boto3.session.Session(region_name=region)
    kafka = session.client("kafka", config=_KAFKA_CLIENT_CONFIG)

    out: list[tuple[str, str]] = []
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = kafka.list_kafka_versions(**kwargs)

        entries = resp.get("KafkaVersions") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            version = str(entry.get("Version") or "").strip()
            status = str(entry.get("Status") or "").strip()
            if version:
                out.append((version, status))

        next_token = resp.get("NextToken")
        if not next_token:
            break
    return out


def _build_version_groups(version_rows: list[tuple[str, str]]) -> list[tuple[str, list[tuple[str, str]]]]:
    buckets: dict[str, list[str]] = {
        "Active": [],
        "Deprecated": [],
        "Other": [],
    }
    for version, status in version_rows:
        buckets[_status_group(status)].append(version)

    ordered_labels = ["Active", "Deprecated", "Other"]
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    for label in ordered_labels:
        versions = sorted(set(buckets.get(label) or []), key=_version_key, reverse=True)
        if versions:
            groups.append((label, [(v, v) for v in versions]))

    if groups:
        return groups

    # If status isn't provided by API in this account/region, still return sorted versions.
    versions = sorted({v for v, _ in version_rows}, key=_version_key, reverse=True)
    if versions:
        return [("Kafka Versions", [(v, v) for v in versions])]
    return []


@catalog_refresh
def _refresh_versions_unlocked(region: str, cache_path: str) -> dict[str, Any]:
    try:
        rows = _fetch_kafka_versions(region)
        groups = _build_version_groups(rows)
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
        logger.warning("Kafka versions: refresh failed region=%s: %s", region, exc)
        return {"ok": False, "region": region, "cache_path": cache_path, "error": str(exc)}
    except Exception as exc:
        logger.exception("Kafka versions: refresh crashed region=%s", region)
        return {"ok": False, "region": region, "cache_path": cache_path, "error": str(exc)}


def _fetch_available_base_instance_types(region: str) -> set[str]:
    session = boto3.session.Session(region_name=region)
    ec2 = session.client("ec2", config=_EC2_CLIENT_CONFIG)

    out: set[str] = set()
    next_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"LocationType": "region", "MaxResults": 100}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = ec2.describe_instance_type_offerings(**kwargs)
        for entry in resp.get("InstanceTypeOfferings", []) or []:
            if not isinstance(entry, dict):
                continue
            value = str(entry.get("InstanceType") or "").strip().lower()
            if value:
                out.add(value)
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return out


def _fetch_kafka_broker_node_instance_types(region: str) -> list[str]:
    available = _fetch_available_base_instance_types(region)
    if not available:
        raise ValueError("No EC2 offerings returned; preserving previous broker catalog")

    out: list[str] = []
    for kafka_type in _DEFAULT_BROKER_NODE_INSTANCE_TYPES:
        base = kafka_type[len("kafka.") :] if kafka_type.startswith("kafka.") else kafka_type
        if base.lower() in available:
            out.append(kafka_type)
    if not out:
        raise ValueError("No curated broker types available in this region")
    return out


def _family(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw.startswith("kafka."):
        raw = raw[len("kafka.") :]
    return raw.split(".", 1)[0]


def _group_for_family(family: str) -> str:
    fam = (family or "").strip().lower()
    if fam.startswith("t"):
        return "Burstable"
    if fam.startswith(("m", "a")):
        return "General Purpose"
    if fam.startswith(("c", "hpc")):
        return "Compute Optimized"
    if fam.startswith(("r", "x", "u", "z")):
        return "Memory Optimized"
    if fam.startswith(("inf", "trn", "dl", "f", "g", "p")):
        return "GPU / Accelerated"
    if fam.startswith(("i", "d", "h", "im", "is")):
        return "Storage Optimized"
    return "Other"


def _build_broker_type_groups(values: list[str]) -> list[tuple[str, list[tuple[str, str]]]]:
    buckets: dict[str, list[str]] = {}
    for value in values:
        v = str(value or "").strip()
        if not v:
            continue
        group = _group_for_family(_family(v))
        buckets.setdefault(group, []).append(v)

    out: list[tuple[str, list[tuple[str, str]]]] = []
    for label in _BROKER_TYPE_GROUP_ORDER:
        items = sorted(set(buckets.get(label) or []))
        if items:
            out.append((label, [(x, x) for x in items]))
    return out


@catalog_refresh
def _refresh_broker_types_unlocked(region: str, cache_path: str) -> dict[str, Any]:
    try:
        values = _fetch_kafka_broker_node_instance_types(region)
        groups = _build_broker_type_groups(values)
        payload: dict[str, Any] = {
            "schema": _SCHEMA_VERSION,
            "source": "curated_ec2_filtered",
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
        logger.warning("Kafka broker node instance types: refresh failed region=%s: %s", region, exc)
        return {"ok": False, "region": region, "cache_path": cache_path, "error": str(exc)}
    except Exception as exc:
        logger.exception("Kafka broker node instance types: refresh crashed region=%s", region)
        return {"ok": False, "region": region, "cache_path": cache_path, "error": str(exc)}


@manual_refresh
def refresh_kafka_version_cache(region: str | None = None) -> dict[str, Any]:
    region = normalize_region(region)
    cache_path, lock_path = _version_cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        return _refresh_versions_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


@manual_refresh
def refresh_kafka_broker_node_instance_type_cache(region: str | None = None) -> dict[str, Any]:
    region = normalize_region(region)
    cache_path, lock_path = _broker_type_cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        return _refresh_broker_types_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


@manual_refresh
def refresh_kafka_catalog_cache(region: str | None = None) -> dict[str, Any]:
    region = normalize_region(region)
    versions = refresh_kafka_version_cache(region)
    broker_types = refresh_kafka_broker_node_instance_type_cache(region)
    return {
        "ok": bool(versions.get("ok")) and bool(broker_types.get("ok")),
        "region": region,
        "versions": versions,
        "broker_node_instance_types": broker_types,
    }


def refresh_kafka_version_cache_if_stale(
    region: str | None = None, *, ttl_seconds_override: int | None = None
) -> dict[str, Any]:
    region = normalize_region(region)
    ttl = int(ttl_seconds_override) if isinstance(ttl_seconds_override, int) and ttl_seconds_override > 0 else _ttl_seconds()

    cache_path, lock_path = _version_cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        payload = read_json(cache_path)
        generated_at = payload_generated_at(payload)
        if generated_at and not is_stale(generated_at, ttl):
            return {"ok": True, "region": region, "cache_path": cache_path, "skipped": True, "generated_at": generated_at}
        return _refresh_versions_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


def refresh_kafka_broker_node_instance_type_cache_if_stale(
    region: str | None = None, *, ttl_seconds_override: int | None = None
) -> dict[str, Any]:
    region = normalize_region(region)
    ttl = int(ttl_seconds_override) if isinstance(ttl_seconds_override, int) and ttl_seconds_override > 0 else _ttl_seconds()

    cache_path, lock_path = _broker_type_cache_paths(region)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        payload = read_json(cache_path)
        generated_at = payload_generated_at(payload)
        if generated_at and not is_stale(generated_at, ttl):
            return {"ok": True, "region": region, "cache_path": cache_path, "skipped": True, "generated_at": generated_at}
        return _refresh_broker_types_unlocked(region, cache_path)
    finally:
        release_lock(lock_fh)


def refresh_kafka_catalog_cache_if_stale(
    region: str | None = None, *, ttl_seconds_override: int | None = None
) -> dict[str, Any]:
    region = normalize_region(region)
    versions = refresh_kafka_version_cache_if_stale(region, ttl_seconds_override=ttl_seconds_override)
    broker_types = refresh_kafka_broker_node_instance_type_cache_if_stale(
        region, ttl_seconds_override=ttl_seconds_override
    )
    return {
        "ok": bool(versions.get("ok")) and bool(broker_types.get("ok")),
        "region": region,
        "versions": versions,
        "broker_node_instance_types": broker_types,
    }


def _load_cached_groups(
    *,
    region: str,
    cache_paths_fn,
    catalog: str,
) -> tuple[list[tuple[str, list[tuple[str, str]]]] | None, AwsCatalogMeta | None]:
    cache_path, _lock_path = cache_paths_fn(region)
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
        catalog=catalog,
        region=region,
        cache_path=cache_path,
        generated_at=generated_at,
        stale=stale,
        count=len(flatten_choices(groups)),
        source=payload.get("source", "curated_unverified") if catalog == _BROKER_TYPE_CATALOG else "aws_cache",
    )
    return groups, meta


def _spawn_refresh_thread(region: str, *, versions: bool, broker_types: bool) -> bool:
    region = normalize_region(region)

    def _runner() -> None:
        time.sleep(random.uniform(0.1, 1.5))
        try:
            if versions:
                cache_path, lock_path = _version_cache_paths(region)
                lock_fh = acquire_lock(lock_path, blocking=False)
                if lock_fh is not None:
                    try:
                        payload = read_json(cache_path)
                        generated_at = payload_generated_at(payload)
                        if is_stale(generated_at, _ttl_seconds()):
                            _refresh_versions_unlocked(region, cache_path)
                    finally:
                        release_lock(lock_fh)

            if broker_types:
                cache_path, lock_path = _broker_type_cache_paths(region)
                lock_fh = acquire_lock(lock_path, blocking=False)
                if lock_fh is not None:
                    try:
                        payload = read_json(cache_path)
                        generated_at = payload_generated_at(payload)
                        if is_stale(generated_at, _ttl_seconds()):
                            _refresh_broker_types_unlocked(region, cache_path)
                    finally:
                        release_lock(lock_fh)
        except Exception:
            logger.exception("Kafka catalogs: background refresh crashed region=%s", region)

    kinds = []
    if versions:
        kinds.append("versions")
    if broker_types:
        kinds.append("broker-types")
    key = f"kafka-catalog-refresh:{'+'.join(kinds)}:{region}"
    return BackgroundRefreshRegistry.spawn(key, _runner)


def get_kafka_version_options(
    *,
    region: str | None = None,
    fallback_groups: list[tuple[str, list[tuple[str, str]]]] | None = None,
    fallback_choices: list[tuple[str, str]] | None = None,
    refresh_async_if_stale: bool = True,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]], AwsCatalogMeta]:
    region = normalize_region(region)
    groups, meta = _load_cached_groups(region=region, cache_paths_fn=_version_cache_paths, catalog=_VERSION_CATALOG)

    if groups and meta:
        if meta.stale and refresh_async_if_stale:
            _spawn_refresh_thread(region, versions=True, broker_types=False)
        choices = flatten_choices(groups)
        return groups, choices, meta

    if refresh_async_if_stale:
        _spawn_refresh_thread(region, versions=True, broker_types=False)

    fg = fallback_groups or []
    fc = fallback_choices or flatten_choices(fg)
    if not fg and fc:
        fg = [("Kafka Versions", [(v, v) for v, _ in fc])]

    cache_path, _ = _version_cache_paths(region)
    meta = AwsCatalogMeta(
        catalog=_VERSION_CATALOG,
        region=region,
        cache_path=cache_path,
        generated_at=None,
        stale=True,
        count=len(fc),
        source="fallback",
        error=None,
    )
    return fg, fc, meta


def get_kafka_broker_node_instance_type_options(
    *,
    region: str | None = None,
    fallback_groups: list[tuple[str, list[tuple[str, str]]]] | None = None,
    fallback_choices: list[tuple[str, str]] | None = None,
    refresh_async_if_stale: bool = True,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[tuple[str, str]], AwsCatalogMeta]:
    region = normalize_region(region)
    groups, meta = _load_cached_groups(
        region=region,
        cache_paths_fn=_broker_type_cache_paths,
        catalog=_BROKER_TYPE_CATALOG,
    )

    if groups and meta:
        if meta.stale and refresh_async_if_stale:
            _spawn_refresh_thread(region, versions=False, broker_types=True)
        choices = flatten_choices(groups)
        return groups, choices, meta

    if refresh_async_if_stale:
        _spawn_refresh_thread(region, versions=False, broker_types=True)

    fg = fallback_groups or []
    fc = fallback_choices or flatten_choices(fg)
    if not fg and fc:
        fg = [("Broker Node Instance Types", [(v, v) for v, _ in fc])]

    cache_path, _ = _broker_type_cache_paths(region)
    meta = AwsCatalogMeta(
        catalog=_BROKER_TYPE_CATALOG,
        region=region,
        cache_path=cache_path,
        generated_at=None,
        stale=True,
        count=len(fc),
        source="fallback",
        error=None,
    )
    return fg, fc, meta
