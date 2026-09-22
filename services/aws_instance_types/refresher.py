from __future__ import annotations

import logging
import os
import random
import threading
import time


from .common import acquire_lock, normalize_region


logger = logging.getLogger(__name__)

_STARTED = False
_START_LOCK = threading.Lock()


def _truthy(val: str | None) -> bool:
    s = (val or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _refresh_regions() -> list[str]:
    raw = (
        os.getenv("PS_AWS_CATALOG_REFRESH_REGIONS")
        or os.getenv("PS_EKS_INSTANCE_TYPES_REFRESH_REGIONS")
        or ""
    ).strip()
    if raw:
        regions = [r.strip() for r in raw.split(",") if r.strip()]
        return [normalize_region(r) for r in regions]
    return [normalize_region(os.getenv("AWS_DEFAULT_REGION"))]


def _interval_seconds() -> int:
    raw = (
        os.getenv("PS_AWS_CATALOG_REFRESH_INTERVAL_SECONDS")
        or os.getenv("PS_EKS_INSTANCE_TYPES_REFRESH_INTERVAL_SECONDS")
        or ""
    ).strip()
    if not raw:
        return 3600  # check hourly, refresh only if stale
    try:
        v = int(raw)
    except Exception:
        return 3600
    return max(60, v)


def start_aws_catalog_refresher() -> bool:
    """
    Start a lightweight background refresher (per-process).

    It checks the cache(s) periodically and refreshes if older than the configured TTL.
    Safe to call multiple times.
    """
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return False
        enabled = os.getenv("PS_AWS_CATALOG_REFRESHER_ENABLED")
        if enabled is None:
            enabled = os.getenv("PS_EKS_INSTANCE_TYPES_REFRESHER_ENABLED", "1")
        if not _truthy(enabled):
            return False
        _STARTED = True

        t = threading.Thread(
            target=_run,
            name="aws-catalog-refresher",
            daemon=True,
        )
        t.start()
        return True


def start_eks_instance_types_refresher() -> bool:
    """
    Backwards compatible alias (this refresher now covers EKS/Aurora/Redis/Kafka catalogs).
    """
    return start_aws_catalog_refresher()


def _run(stop_event=None) -> None:
    from .common import release_lock
    from .registry import refresh_region
    stop = stop_event or threading.Event()
    interval = _interval_seconds()
    # Followers retry election so a worker restart cannot strand the scheduler.
    while not stop.is_set():
        leader_lock = acquire_lock(
            os.getenv("PS_AWS_CATALOG_REFRESHER_LEADER_LOCK", "/tmp/praxis-cache/aws-catalog-refresher.leader.lock"),
            blocking=False,
        )
        if leader_lock is None:
            stop.wait(30)
            continue
        try:
            while not stop.is_set():
                for region in _refresh_regions():
                    if stop.is_set():
                        break
                    try:
                        refresh_region(region)
                    except Exception:
                        logger.exception("AWS catalog refresh failed region=%s", region)
                stop.wait(interval)
        finally:
            release_lock(leader_lock)
