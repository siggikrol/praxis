# Status adapter for the archive views in Artiac Operations.
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

from modules.artiac_control.service import fetch_status_payload, _get_api_base

log = logging.getLogger("modules.artiac_control.archive.artiac_status")

CACHE_TTL_SECONDS = int(os.getenv("PS_ARTIAC_STATUS_CACHE_TTL", "30"))
_cache: Dict[str, Any] = {"expires_at": 0.0, "payload": None}


def fetch_artiac_status() -> Dict[str, Any]:
    now = time.time()
    cached = _cache.get("payload")
    if cached and now < _cache.get("expires_at", 0.0):
        return dict(cached)

    out: Dict[str, Any] = {
        "ok": False,
        "source": f"{_get_api_base()}/status",
        "modules": [],
        "status_by_module": {},
    }

    try:
        data = fetch_status_payload(timeout=3)
        status_file = data.get("statusFile") or {}
        # The published status file is authoritative for archive/version
        # matching. Other API module collections can describe a different run.
        modules = status_file.get("modules") or data.get("modules") or []
        if not isinstance(modules, list):
            raise ValueError("Unexpected modules payload")
        out["modules"] = modules
        out["status_by_module"] = {
            m.get("name"): m for m in modules if isinstance(m, dict) and m.get("name")
        }
        out["ok"] = True
    except Exception as exc:
        out["error"] = str(exc)
        log.warning("Artiac status fetch failed: %s", exc)

    _cache["payload"] = dict(out)
    _cache["expires_at"] = time.time() + CACHE_TTL_SECONDS
    return out
