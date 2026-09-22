from __future__ import annotations


from concurrent.futures import ThreadPoolExecutor
import logging
import os
import threading
import time
from typing import Any

from services.aws_instance_types.common import (
    acquire_lock,
    is_stale,
    normalize_region,
    payload_generated_at,
    read_json,
    release_lock,
    write_json_atomic,
)
from services.pc_source_scanner.structure_cache import (
    refresh_pc_source_sections,
    refresh_pc_source_sections_if_stale,
)


logger = logging.getLogger(__name__)

_STARTED = False
_LOCK = threading.Lock()


def _truthy(val: str | None) -> bool:
    s = (val or "").strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _startup_enabled() -> bool:
    return _truthy(os.getenv("PS_VALIDATE_CACHES_ON_STARTUP", "1"))


def _startup_lock_path() -> str:
    return (
        os.getenv("PS_VALIDATE_CACHES_ON_STARTUP_LOCK_PATH")
        or "/tmp/praxis-cache/startup-cache-validation.lock"
    ).strip()


def _startup_done_path(lock_path: str) -> str:
    raw = (os.getenv("PS_VALIDATE_CACHES_ON_STARTUP_DONE_PATH") or "").strip()
    if raw:
        return raw
    return f"{lock_path}.done.json"


def _startup_done_ttl_seconds() -> int:
    raw = (os.getenv("PS_VALIDATE_CACHES_ON_STARTUP_DONE_TTL_SECONDS") or "").strip()
    if not raw:
        return 300
    try:
        v = int(raw)
    except Exception:
        return 300
    return max(30, v)


def _startup_async() -> bool:
    return _truthy(os.getenv("PS_VALIDATE_CACHES_ON_STARTUP_ASYNC", "1"))


def _startup_force() -> bool:
    return _truthy(os.getenv("PS_VALIDATE_CACHES_ON_STARTUP_FORCE", "0"))


def _validate_aws_caches_enabled() -> bool:
    return _truthy(os.getenv("PS_VALIDATE_AWS_CACHES_ON_STARTUP", "1"))


def _validate_pc_source_cache_enabled() -> bool:
    return _truthy(os.getenv("PS_VALIDATE_PC_SOURCE_CACHE_ON_STARTUP", "1"))


def _validate_spacelift_cache_enabled() -> bool:
    return _truthy(os.getenv("PS_VALIDATE_SPACELIFT_CACHE_ON_STARTUP", "1"))


def _aws_regions() -> list[str]:
    raw = (
        os.getenv("PS_AWS_CATALOG_REFRESH_REGIONS")
        or os.getenv("PS_EKS_INSTANCE_TYPES_REFRESH_REGIONS")
        or ""
    ).strip()
    if raw:
        seen: set[str] = set()
        out: list[str] = []
        for part in raw.split(","):
            region = normalize_region(part.strip())
            key = region.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(region)
        return out or [normalize_region(os.getenv("AWS_DEFAULT_REGION"))]
    return [normalize_region(os.getenv("AWS_DEFAULT_REGION"))]


def _pc_branches() -> list[str | None]:
    raw = (os.getenv("PS_PC_SOURCE_REFRESH_BRANCHES") or "").strip()
    if not raw:
        return [None]
    seen: set[str] = set()
    out: list[str | None] = []
    for part in raw.split(","):
        p = (part or "").strip()
        branch = None if (not p or p.lower() == "default") else p
        key = (branch or "default").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(branch)
    return out or [None]


def _brief(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"ok": False, "error": f"unexpected result type: {type(result).__name__}"}
    return {
        "ok": result.get("ok"),
        "skipped": result.get("skipped"),
        "generated_at": result.get("generated_at"),
        "error": result.get("error"),
        "cache_path": result.get("cache_path"),
    }


def _validate_aws_caches(force: bool) -> None:
    from services.aws_instance_types.registry import refresh_region
    for region in _aws_regions():
        results = refresh_region(region, force=force)
        logger.info("Startup AWS cache validation region=%s results=%s", region, results)


def _validate_pc_source_cache(force: bool) -> None:
    for branch in _pc_branches():
        name = branch or "default"
        try:
            if force:
                result = refresh_pc_source_sections(branch, force_refresh=True)
            else:
                result = refresh_pc_source_sections_if_stale(branch)
            logger.info("Startup cache validation PC Source branch=%s result=%s", name, _brief(result))
        except Exception:
            logger.exception("Startup cache validation failed for PC Source branch=%s", name)


def _validate_spacelift(force: bool) -> None:
    if not _validate_spacelift_cache_enabled():
        logger.info("Startup cache validation Spacelift: skipped (disabled)")
        return
    try:
        from modules.status.service import check_spacelift, refresh_spacelift_status_cache

        result = refresh_spacelift_status_cache() if force else check_spacelift()
        logger.info("Startup cache validation Spacelift result=%s", _brief(result))
    except Exception:
        logger.exception("Startup cache validation failed for Spacelift cache")


def _startup_already_completed(done_path: str) -> bool:
    payload = read_json(done_path) or {}
    generated_at = payload_generated_at(payload)
    if not generated_at:
        return False
    return not is_stale(generated_at, _startup_done_ttl_seconds())


def _mark_startup_completed(done_path: str, *, force: bool) -> None:
    try:
        write_json_atomic(
            done_path,
            {
                "generated_at": time.time(),
                "force": bool(force),
                "pid": os.getpid(),
            },
        )
    except Exception:
        logger.exception("Startup cache validation: failed writing completion marker %s", done_path)


def _run_step(app, name: str, fn) -> None:
    started = time.time()
    try:
        with app.app_context():
            fn()
    except Exception:
        logger.exception("Startup cache validation %s step crashed", name)
    finally:
        logger.info(
            "Startup cache validation step %s completed in %.2fs",
            name,
            time.time() - started,
        )


def _run_once(app, *, force: bool) -> None:
    started = time.time()
    tasks: list[tuple[str, Any]] = []

    if _validate_aws_caches_enabled():
        tasks.append(("aws", lambda: _validate_aws_caches(force)))
    else:
        logger.info("Startup cache validation AWS: skipped (disabled)")

    if _validate_pc_source_cache_enabled():
        tasks.append(("pc_source", lambda: _validate_pc_source_cache(force)))
    else:
        logger.info("Startup cache validation PC Source: skipped (disabled)")

    if _validate_spacelift_cache_enabled():
        tasks.append(("spacelift", lambda: _validate_spacelift(force)))
    else:
        logger.info("Startup cache validation Spacelift: skipped (disabled)")

    if tasks:
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = [pool.submit(_run_step, app, name, fn) for name, fn in tasks]
            for fut in futures:
                try:
                    fut.result()
                except Exception:
                    # _run_step already logs exceptions, this is defensive.
                    logger.exception("Startup cache validation: unexpected thread failure")

    logger.info(
        "Startup cache validation completed in %.2fs (force=%s)",
        time.time() - started,
        force,
    )


def validate_caches_on_startup(app) -> bool:
    """
    Validate (and refresh if stale) key disk-backed caches once during process startup.

    Controls:
    - PS_VALIDATE_CACHES_ON_STARTUP=1|0 (default: 1)
    - PS_VALIDATE_CACHES_ON_STARTUP_LOCK_PATH=/tmp/...lock
    - PS_VALIDATE_CACHES_ON_STARTUP_DONE_PATH=/tmp/...done.json
    - PS_VALIDATE_CACHES_ON_STARTUP_DONE_TTL_SECONDS=300
    - PS_VALIDATE_CACHES_ON_STARTUP_FORCE=1|0 (default: 0)
    - PS_VALIDATE_CACHES_ON_STARTUP_ASYNC=1|0 (default: 1)
    - PS_VALIDATE_AWS_CACHES_ON_STARTUP=1|0 (default: 1)
    - PS_VALIDATE_PC_SOURCE_CACHE_ON_STARTUP=1|0 (default: 1)
    - PS_VALIDATE_SPACELIFT_CACHE_ON_STARTUP=1|0 (default: 1)
    """
    if not _startup_enabled():
        logger.info("Startup cache validation disabled")
        return False

    global _STARTED
    with _LOCK:
        if _STARTED:
            return False
        _STARTED = True

    lock_path = _startup_lock_path()
    done_path = _startup_done_path(lock_path)
    lock_fh = acquire_lock(lock_path, blocking=False)
    if lock_fh is None:
        if _startup_async():
            logger.info(
                "Startup cache validation already running asynchronously in another worker; skipping"
            )
            return False
        logger.info("Startup cache validation waiting for another worker (lock=%s)", lock_path)
        wait_fh = acquire_lock(lock_path, blocking=True)
        try:
            if _startup_already_completed(done_path):
                logger.info("Startup cache validation skipped in this worker (already completed elsewhere)")
                return False
            logger.warning(
                "Startup cache validation marker missing after lock wait; running validation in this worker"
            )
            _run_once(app, force=_startup_force())
            _mark_startup_completed(done_path, force=_startup_force())
            return True
        finally:
            release_lock(wait_fh)

    if _startup_already_completed(done_path):
        release_lock(lock_fh)
        logger.info("Startup cache validation skipped (recently completed)")
        return False

    force = _startup_force()
    if _startup_async():
        def _runner() -> None:
            try:
                _run_once(app, force=force)
                _mark_startup_completed(done_path, force=force)
            finally:
                release_lock(lock_fh)

        t = threading.Thread(
            target=_runner,
            name="startup-cache-validation",
            daemon=True,
        )
        t.start()
        logger.info("Startup cache validation started asynchronously (force=%s)", force)
        return True

    try:
        _run_once(app, force=force)
        _mark_startup_completed(done_path, force=force)
        return True
    finally:
        release_lock(lock_fh)
