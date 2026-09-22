from __future__ import annotations

import logging
import os
import random
import threading
import time

from .structure_cache import refresh_pc_source_sections_if_stale
from services.aws_instance_types.common import acquire_lock


logger = logging.getLogger(__name__)

_STARTED = False
_START_LOCK = threading.Lock()


def _truthy(val: str | None) -> bool:
    s = (val or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _refresh_branches() -> list[str | None]:
    """
    Branches to refresh periodically.

    - If PS_PC_SOURCE_REFRESH_BRANCHES is set: comma-separated list.
      Use "default" to refresh the repo default branch (no ref).
    - If unset: refresh only the default branch.
    """
    raw = (os.getenv("PS_PC_SOURCE_REFRESH_BRANCHES") or "").strip()
    if not raw:
        return [None]
    out: list[str | None] = []
    for part in raw.split(","):
        p = (part or "").strip()
        if not p or p.lower() == "default":
            out.append(None)
        else:
            out.append(p)
    # De-dupe while keeping order.
    seen: set[str] = set()
    uniq: list[str | None] = []
    for b in out:
        key = (b or "").strip().lower() or "default"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)
    return uniq or [None]


def _interval_seconds() -> int:
    raw = (os.getenv("PS_PC_SOURCE_REFRESH_INTERVAL_SECONDS") or "").strip()
    if not raw:
        return 3600  # check hourly, refresh only if stale
    try:
        v = int(raw)
    except Exception:
        return 3600
    return max(60, v)


def start_pc_source_refresher() -> bool:
    """
    Start a lightweight background refresher (per-process).

    It checks the on-disk pc_source cache periodically and refreshes if older than TTL.
    Safe to call multiple times.
    """
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return False

        enabled = os.getenv("PS_PC_SOURCE_REFRESHER_ENABLED", "1")
        if not _truthy(enabled):
            return False

        _STARTED = True
        t = threading.Thread(target=_run, name="pc-source-refresher", daemon=True)
        t.start()
        return True


def _run() -> None:
    leader_lock = acquire_lock(
        os.getenv("PS_PC_SOURCE_REFRESHER_LEADER_LOCK", "/tmp/praxis-cache/pc-source-refresher.leader.lock"),
        blocking=False,
    )
    if leader_lock is None:
        logger.info("PC Source: another worker owns the periodic refresher")
        return
    logger.info("PC Source: this worker owns the periodic refresher")
    # Small jitter so multiple gunicorn workers don't all stampede GitHub.
    time.sleep(random.uniform(0.5, 3.0))

    interval = _interval_seconds()
    branches = _refresh_branches()

    while True:
        for branch in branches:
            try:
                refresh_pc_source_sections_if_stale(branch)
            except Exception:
                logger.exception("PC Source: periodic refresh failed branch=%s", branch or "default")
        time.sleep(interval)
