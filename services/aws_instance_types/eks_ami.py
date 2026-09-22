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
    is_stale,
    normalize_region,
    payload_generated_at,
    read_json,
    release_lock,
    ttl_seconds,
    write_json_atomic,
)


logger = logging.getLogger(__name__)

_SSM_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 3, "mode": "standard"},
)

_SCHEMA_VERSION = 1
_CATALOG = "eks_ami"
_CACHE_STEM = "eks_ami"
_ARCH = "x86_64"
_VARIANT = "standard"


def _ttl_seconds() -> int:
    return ttl_seconds("PS_EKS_AMI_TTL_SECONDS")


def _cache_dir() -> str:
    return cache_dir("PS_EKS_AMI_CACHE_DIR")


def _cache_stem(cluster_version: str, ami_type: str) -> str:
    return f"{_CACHE_STEM}.{cluster_version}.{ami_type}.{_ARCH}.{_VARIANT}"


def _cache_paths(region: str, cluster_version: str, ami_type: str) -> tuple[str, str]:
    return cache_paths(
        cache_dir_path=_cache_dir(),
        cache_stem=_cache_stem(cluster_version, ami_type),
        region=region,
    )


def _parameter_name(cluster_version: str, ami_type: str) -> str | None:
    version = (cluster_version or "").strip()
    image_type = (ami_type or "").strip()
    if not version or image_type != "amazon-linux-2023":
        return None
    return (
        f"/aws/service/eks/optimized-ami/{version}/"
        f"amazon-linux-2023/{_ARCH}/{_VARIANT}/recommended/image_id"
    )


def _fetch_eks_ami_id(region: str, cluster_version: str, ami_type: str) -> str:
    parameter_name = _parameter_name(cluster_version, ami_type)
    if not parameter_name:
        raise ValueError(f"Unsupported EKS AMI lookup: ami_type={ami_type!r}")

    session = boto3.session.Session(region_name=region)
    ssm = session.client("ssm", config=_SSM_CLIENT_CONFIG)
    resp = ssm.get_parameter(Name=parameter_name)
    return str(resp.get("Parameter", {}).get("Value") or "").strip()


@manual_refresh
def refresh_eks_ami_cache(
    region: str | None = None,
    *,
    cluster_version: str,
    ami_type: str,
) -> dict[str, Any]:
    region = normalize_region(region)
    cluster_version = (cluster_version or "").strip()
    ami_type = (ami_type or "").strip()
    cache_path, lock_path = _cache_paths(region, cluster_version, ami_type)
    lock_fh = acquire_lock(lock_path, blocking=True)
    if lock_fh is None:
        return {"ok": False, "error": "Cache lock unavailable", "cache_path": cache_path}
    try:
        return _refresh_eks_ami_cache_unlocked(region, cluster_version, ami_type, cache_path)
    finally:
        release_lock(lock_fh)


@catalog_refresh
def _refresh_eks_ami_cache_unlocked(
    region: str,
    cluster_version: str,
    ami_type: str,
    cache_path: str,
) -> dict[str, Any]:
    try:
        ami_id = _fetch_eks_ami_id(region, cluster_version, ami_type)
        if not ami_id:
            raise ValueError("SSM returned an empty AMI ID")
        payload: dict[str, Any] = {
            "schema": _SCHEMA_VERSION,
            "generated_at": time.time(),
            "region": region,
            "cluster_version": cluster_version,
            "ami_type": ami_type,
            "architecture": _ARCH,
            "variant": _VARIANT,
            "parameter_name": _parameter_name(cluster_version, ami_type),
            "ami_id": ami_id,
            "count": 1,
        }
        write_json_atomic(cache_path, payload)
        return {
            "ok": True,
            "region": region,
            "cluster_version": cluster_version,
            "ami_type": ami_type,
            "cache_path": cache_path,
            "ami_id": ami_id,
            "generated_at": payload["generated_at"],
        }
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "EKS AMI: refresh failed region=%s version=%s ami_type=%s: %s",
            region,
            cluster_version,
            ami_type,
            exc,
        )
        return {
            "ok": False,
            "region": region,
            "cluster_version": cluster_version,
            "ami_type": ami_type,
            "cache_path": cache_path,
            "error": str(exc),
        }
    except Exception as exc:
        logger.exception(
            "EKS AMI: refresh crashed region=%s version=%s ami_type=%s",
            region,
            cluster_version,
            ami_type,
        )
        return {
            "ok": False,
            "region": region,
            "cluster_version": cluster_version,
            "ami_type": ami_type,
            "cache_path": cache_path,
            "error": str(exc),
        }


def refresh_eks_ami_cache_if_stale(
    region: str | None = None,
    *,
    cluster_version: str,
    ami_type: str,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    region = normalize_region(region)
    cluster_version = (cluster_version or "").strip()
    ami_type = (ami_type or "").strip()
    ttl = int(ttl_seconds) if isinstance(ttl_seconds, int) and ttl_seconds > 0 else _ttl_seconds()

    cache_path, lock_path = _cache_paths(region, cluster_version, ami_type)
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
                "cluster_version": cluster_version,
                "ami_type": ami_type,
                "cache_path": cache_path,
                "skipped": True,
                "generated_at": generated_at,
            }
        return _refresh_eks_ami_cache_unlocked(region, cluster_version, ami_type, cache_path)
    finally:
        release_lock(lock_fh)


def _load_cached_ami(
    region: str,
    cluster_version: str,
    ami_type: str,
) -> tuple[str | None, AwsCatalogMeta | None]:
    cache_path, _lock_path = _cache_paths(region, cluster_version, ami_type)
    payload = read_json(cache_path)
    if not payload:
        return None, None
    if payload.get("schema") != _SCHEMA_VERSION:
        return None, None
    if payload.get("cluster_version") != cluster_version or payload.get("ami_type") != ami_type:
        return None, None

    ami_id = str(payload.get("ami_id") or "").strip()
    if not ami_id:
        return None, None

    generated_at = payload_generated_at(payload)
    meta = AwsCatalogMeta(
        catalog=_CATALOG,
        region=region,
        cache_path=cache_path,
        generated_at=generated_at,
        stale=is_stale(generated_at, _ttl_seconds()),
        count=1,
        source="aws_cache",
    )
    return ami_id, meta


def _spawn_refresh_thread(region: str, cluster_version: str, ami_type: str) -> bool:
    region = normalize_region(region)
    cluster_version = (cluster_version or "").strip()
    ami_type = (ami_type or "").strip()

    def _runner() -> None:
        time.sleep(random.uniform(0.1, 1.5))
        try:
            cache_path, lock_path = _cache_paths(region, cluster_version, ami_type)
            lock_fh = acquire_lock(lock_path, blocking=False)
            if lock_fh is None:
                return
            try:
                payload = read_json(cache_path)
                generated_at = payload_generated_at(payload)
                if not is_stale(generated_at, _ttl_seconds()):
                    return
                _refresh_eks_ami_cache_unlocked(region, cluster_version, ami_type, cache_path)
            finally:
                release_lock(lock_fh)
        except Exception:
            logger.exception(
                "EKS AMI: background refresh crashed region=%s version=%s ami_type=%s",
                region,
                cluster_version,
                ami_type,
            )

    key = f"eks-ami-refresh:{region}:{cluster_version}:{ami_type}"
    return BackgroundRefreshRegistry.spawn(key, _runner)


def get_eks_ami_id(
    *,
    region: str | None = None,
    cluster_version: str,
    ami_type: str,
    refresh_async_if_stale: bool = True,
) -> tuple[str | None, AwsCatalogMeta]:
    region = normalize_region(region)
    cluster_version = (cluster_version or "").strip()
    ami_type = (ami_type or "").strip()
    cache_path, _ = _cache_paths(region, cluster_version, ami_type)

    ami_id, meta = _load_cached_ami(region, cluster_version, ami_type)
    if ami_id and meta:
        if meta.stale and refresh_async_if_stale:
            _spawn_refresh_thread(region, cluster_version, ami_type)
        return ami_id, meta

    if _parameter_name(cluster_version, ami_type):
        refresh = refresh_eks_ami_cache(
            region,
            cluster_version=cluster_version,
            ami_type=ami_type,
        )
        if refresh.get("ok"):
            ami_id, meta = _load_cached_ami(region, cluster_version, ami_type)
            if ami_id and meta:
                return ami_id, meta

    return None, AwsCatalogMeta(
        catalog=_CATALOG,
        region=region,
        cache_path=cache_path,
        generated_at=None,
        stale=True,
        count=0,
        source="fallback",
        error=None if _parameter_name(cluster_version, ami_type) else "unsupported ami_type",
    )
