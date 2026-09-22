from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from services.aws_instance_types.common import (
    BackgroundRefreshRegistry,
    acquire_lock,
    cache_dir,
    cache_paths,
    is_stale,
    payload_generated_at,
    read_json,
    release_lock,
    ttl_seconds,
    write_json_atomic,
)

from .cache import clear_cached_branch
from .provider import (
    get_bootstrap_dirs,
    get_deployment_dirs,
    get_helm_app_cds_dirs,
    get_helm_bootstrap_cds_dirs,
    get_helm_dirs,
    get_helm_iac_cds_dirs,
    get_iac_dirs,
)
from .provider_types import PCSections


logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_CACHE_DIR = "/tmp/praxis-cache"

_SCHEMA_VERSION = 1
_CACHE_STEM = "pc_source_sections"
_ASSETS_DIR_NAME = "assets"


@dataclass(frozen=True)
class PcSourceCacheMeta:
    branch: str | None
    cache_path: str
    generated_at: float | None
    stale: bool
    total: int
    counts: dict[str, int]
    source: str  # pc_cache | github
    error: str | None = None


def _safe_token(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()) or "unknown"


def _normalize_branch(branch: str | None) -> str | None:
    b = (branch or "").strip()
    return b or None


def _branch_token(branch: str | None) -> str:
    # Use "default" to represent the repo default branch (i.e., no ref passed).
    return _normalize_branch(branch) or "default"


def _normalize_items(items: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for v in (items or []):
        s = str(v).strip()
        if not s or s == _ASSETS_DIR_NAME:
            continue
        out.append(s)
    # De-dupe + stable sort
    return sorted(set(out), key=str.lower)


def _ttl_seconds() -> int:
    return ttl_seconds("PS_PC_SOURCE_CACHE_TTL_SECONDS", default=DEFAULT_TTL_SECONDS)


def _cache_dir() -> str:
    return cache_dir("PS_PC_SOURCE_CACHE_DIR", default=DEFAULT_CACHE_DIR)


def _cache_paths(branch: str | None) -> tuple[str, str]:
    token = _branch_token(branch)
    return cache_paths(cache_dir_path=_cache_dir(), cache_stem=_CACHE_STEM, region=token)


def pc_source_cache_path(branch: str | None = None) -> str:
    return _cache_paths(branch)[0]


def _empty_sections() -> PCSections:
    return {
        "bootstrap": [],
        "deployment": [],
        "iac": [],
        "helm": [],
        "helm_iac_cds": [],
        "helm_app_cds": [],
        "helm_bootstrap_cds": [],
    }


def _coerce_sections(raw: Any) -> PCSections | None:
    if not isinstance(raw, dict):
        return None
    out = _empty_sections()
    for key in out.keys():
        val = raw.get(key)
        if isinstance(val, (list, tuple)):
            out[key] = _normalize_items([str(v) for v in val])
    return out


def _load_cached_sections(branch: str | None) -> tuple[PCSections | None, PcSourceCacheMeta | None]:
    cache_path, _lock_path = _cache_paths(branch)
    payload = read_json(cache_path)
    if not payload or payload.get("schema") != _SCHEMA_VERSION:
        return None, None

    raw_sections = payload.get("sections")
    sections = _coerce_sections(raw_sections)
    if not sections:
        return None, None

    generated_at = payload_generated_at(payload)
    stale = is_stale(generated_at, _ttl_seconds())

    counts = {k: len(v) for k, v in sections.items()}
    total = int(sum(counts.values()))

    payload_branch = payload.get("branch")
    if isinstance(payload_branch, str) and payload_branch.strip():
        branch_val: str | None = payload_branch.strip()
    elif payload_branch is None:
        branch_val = None
    else:
        branch_val = _normalize_branch(branch)

    error_val = payload.get("error")
    error = str(error_val) if isinstance(error_val, str) and error_val.strip() else None

    meta = PcSourceCacheMeta(
        branch=branch_val,
        cache_path=cache_path,
        generated_at=generated_at,
        stale=stale,
        total=total,
        counts=counts,
        source="pc_cache",
        error=error,
    )
    return sections, meta


def _fetch_sections(
    branch: str | None,
    *,
    force_refresh: bool,
    on_event: Callable[[str, str, str | None], None] | None = None,
    on_step: Callable[[], None] | None = None,
) -> tuple[PCSections, dict[str, str]]:
    """
    Fetch all sections from GitHub.

    Returns: (sections, section_errors)
      - sections may be partial if some sections failed
      - section_errors maps section -> error string
    """
    branch = _normalize_branch(branch)
    out = _empty_sections()
    errs: dict[str, str] = {}

    def _call(section: str, fn) -> list[str]:
        if on_event:
            on_event("info", f"Fetching {section}…", section)
        try:
            vals = fn(branch=branch, force_refresh=force_refresh)
            norm = _normalize_items(vals)
            if on_event:
                on_event("info", f"Fetched {section} ({len(norm)})", section)
            return norm
        except Exception as exc:
            msg = str(exc)
            errs[section] = msg
            logger.warning("PC Source: fetch failed section=%s branch=%s: %s", section, branch or "default", msg)
            if on_event:
                on_event("warning", f"Failed {section}: {msg}", section)
            return []
        finally:
            if on_step:
                on_step()

    out["bootstrap"] = _call("bootstrap", get_bootstrap_dirs)
    out["deployment"] = _call("deployment", get_deployment_dirs)
    out["iac"] = _call("iac", get_iac_dirs)
    out["helm"] = _call("helm", get_helm_dirs)
    out["helm_iac_cds"] = _call("helm_iac_cds", get_helm_iac_cds_dirs)
    out["helm_app_cds"] = _call("helm_app_cds", get_helm_app_cds_dirs)
    out["helm_bootstrap_cds"] = _call("helm_bootstrap_cds", get_helm_bootstrap_cds_dirs)

    return out, errs


def refresh_pc_source_sections(
    branch: str | None = None,
    *,
    force_refresh: bool = True,
    on_event: Callable[[str, str, str | None], None] | None = None,
    on_step: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """
    Force-refresh the on-disk PC source sections cache for a branch.
    """
    branch = _normalize_branch(branch)
    cache_path, lock_path = _cache_paths(branch)
    lock_fh = acquire_lock(lock_path, blocking=True)
    try:
        return _refresh_unlocked(
            branch,
            cache_path,
            force_refresh=force_refresh,
            on_event=on_event,
            on_step=on_step,
        )
    finally:
        release_lock(lock_fh)


def clear_pc_source_sections(branch: str | None = None) -> dict[str, Any]:
    """Delete the disk cache for one branch and clear this worker's memory cache."""
    branch = _normalize_branch(branch)
    cache_path, lock_path = _cache_paths(branch)
    lock_fh = acquire_lock(lock_path, blocking=True)
    deleted = False
    try:
        try:
            os.unlink(cache_path)
            deleted = True
        except FileNotFoundError:
            pass
    finally:
        release_lock(lock_fh)

    memory_entries_cleared = clear_cached_branch(branch)
    return {
        "ok": True,
        "branch": branch,
        "cache_path": cache_path,
        "deleted": deleted,
        "memory_entries_cleared": memory_entries_cleared,
    }


def _refresh_unlocked(
    branch: str | None,
    cache_path: str,
    *,
    force_refresh: bool,
    on_event: Callable[[str, str, str | None], None] | None = None,
    on_step: Callable[[], None] | None = None,
) -> dict[str, Any]:
    # If we have an existing payload, we can keep its values for sections that fail to fetch.
    prev_payload = read_json(cache_path) or {}
    prev_sections = _coerce_sections(prev_payload.get("sections")) or _empty_sections()

    sections, section_errors = _fetch_sections(
        branch,
        force_refresh=force_refresh,
        on_event=on_event,
        on_step=on_step,
    )

    # Merge: keep old values for any section that failed.
    merged = _empty_sections()
    had_success = False
    for key, vals in sections.items():
        if key in section_errors:
            merged[key] = prev_sections.get(key, [])
        else:
            merged[key] = vals
            had_success = True

    if not had_success and section_errors:
        # Nothing refreshed successfully; don't overwrite existing cache.
        return {
            "ok": False,
            "branch": branch,
            "cache_path": cache_path,
            "error": "PC source refresh failed for all sections",
            "section_errors": section_errors,
        }

    counts = {k: len(v) for k, v in merged.items()}
    total = int(sum(counts.values()))
    payload: dict[str, Any] = {
        "schema": _SCHEMA_VERSION,
        "generated_at": time.time(),
        "branch": branch,
        "sections": merged,
        "counts": counts,
        "total": total,
    }
    if section_errors:
        payload["error"] = "partial_refresh"
        payload["section_errors"] = section_errors

    write_json_atomic(cache_path, payload)
    return {
        "ok": True,
        "branch": branch,
        "cache_path": cache_path,
        "generated_at": payload["generated_at"],
        "total": total,
        "counts": counts,
        "partial_errors": section_errors or None,
    }


def refresh_pc_source_sections_if_stale(
    branch: str | None = None, *, ttl_seconds_override: int | None = None
) -> dict[str, Any]:
    branch = _normalize_branch(branch)
    ttl = int(ttl_seconds_override) if isinstance(ttl_seconds_override, int) and ttl_seconds_override > 0 else _ttl_seconds()

    cache_path, lock_path = _cache_paths(branch)
    lock_fh = acquire_lock(lock_path, blocking=True)
    try:
        payload = read_json(cache_path)
        generated_at = payload_generated_at(payload)
        if generated_at and not is_stale(generated_at, ttl):
            return {
                "ok": True,
                "skipped": True,
                "branch": branch,
                "cache_path": cache_path,
                "generated_at": generated_at,
            }
        return _refresh_unlocked(branch, cache_path, force_refresh=True)
    finally:
        release_lock(lock_fh)


def _spawn_refresh_thread(branch: str | None) -> bool:
    branch = _normalize_branch(branch)
    safe = _safe_token(_branch_token(branch))

    def _runner() -> None:
        time.sleep(random.uniform(0.1, 1.5))
        try:
            cache_path, lock_path = _cache_paths(branch)
            lock_fh = acquire_lock(lock_path, blocking=False)
            if lock_fh is None:
                return
            try:
                payload = read_json(cache_path)
                generated_at = payload_generated_at(payload)
                if not is_stale(generated_at, _ttl_seconds()):
                    return
                _refresh_unlocked(branch, cache_path, force_refresh=True)
            finally:
                release_lock(lock_fh)
        except Exception:
            logger.exception("PC Source: background refresh crashed branch=%s", branch or "default")

    return BackgroundRefreshRegistry.spawn(f"pc-source-refresh:{safe}", _runner)


def get_pc_source_sections(
    *, branch: str | None = None, refresh_async_if_stale: bool = True
) -> tuple[PCSections, PcSourceCacheMeta]:
    """
    Load cached PC source folder structure for a branch (disk-backed).

    If stale and refresh_async_if_stale is True, starts a best-effort background refresh.
    If missing, performs a synchronous refresh to populate the cache (may be slow).
    """
    branch = _normalize_branch(branch)
    sections, meta = _load_cached_sections(branch)
    if sections and meta:
        if meta.stale and refresh_async_if_stale:
            _spawn_refresh_thread(branch)
        return sections, meta

    # No cache available; build it once so the wizard still works.
    # A missing disk cache may have been explicitly deleted. Rebuild it from
    # GitHub instead of allowing another worker's in-process cache to restore
    # the just-deleted values.
    res = refresh_pc_source_sections(branch, force_refresh=True)
    if res.get("ok"):
        sections2, meta2 = _load_cached_sections(branch)
        if sections2 and meta2:
            if meta2.stale and refresh_async_if_stale:
                _spawn_refresh_thread(branch)
            return sections2, meta2

    # Last resort: fetch directly (no disk cache).
    sections3, errs = _fetch_sections(branch, force_refresh=False)
    counts = {k: len(v) for k, v in sections3.items()}
    total = int(sum(counts.values()))
    cache_path = pc_source_cache_path(branch)
    meta3 = PcSourceCacheMeta(
        branch=branch,
        cache_path=cache_path,
        generated_at=None,
        stale=True,
        total=total,
        counts=counts,
        source="github",
        error="; ".join([f"{k}: {v}" for k, v in errs.items()]) if errs else None,
    )
    return sections3, meta3
