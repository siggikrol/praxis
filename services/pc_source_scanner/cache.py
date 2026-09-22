from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from flask import current_app

DEFAULT_TTL_SECONDS = 3600  # 1 hour


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


def _get_store() -> Dict[str, CacheEntry]:
    """
    Returns the per-app cache store for PC source data.
    """
    ext = getattr(current_app, "extensions", None)
    if ext is None:
        raise RuntimeError("Flask application context is required")

    store = ext.get("pc_source_cache")
    if store is None:
        store = {}
        ext["pc_source_cache"] = store
    return store


def get_cached(key: str) -> Optional[Any]:
    """
    Returns cached value if present and not expired, otherwise None.
    """
    store = _get_store()
    entry = store.get(key)
    if not entry:
        return None

    now = time.time()
    if entry.expires_at <= now:
        store.pop(key, None)
        return None

    return entry.value


def set_cached(key: str, value: Any, ttl: int = DEFAULT_TTL_SECONDS) -> None:
    """
    Stores value in cache with the given TTL.
    """
    store = _get_store()
    expires_at = time.time() + ttl
    store[key] = CacheEntry(value=value, expires_at=expires_at)


def clear_cached_branch(branch: str | None = None) -> int:
    """Clear this worker's in-memory PC Source entries for one branch."""
    token = (branch or "").strip() or "default"
    suffix = f":{token}"
    store = _get_store()
    keys = [key for key in store if key.endswith(suffix)]
    for key in keys:
        store.pop(key, None)
    return len(keys)
