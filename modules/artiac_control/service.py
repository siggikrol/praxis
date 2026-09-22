from __future__ import annotations

import os
from urllib.parse import urlparse
from typing import Any, Dict, Tuple

import requests
from flask import current_app


DEFAULT_BASE_URL = "http://praxis-artiac-status-api.praxis-artiac.svc.cluster.local"


def _get_api_base() -> str:
    legacy_status_url = (
        os.getenv("PS_ARTIAC_STATUS_URL")
        or os.getenv("ARTIAC_STATUS_URL")
        or ""
    ).strip()
    legacy_base = ""
    if legacy_status_url:
        parsed = urlparse(legacy_status_url)
        legacy_base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    base = (
        os.getenv("ARTIAC_STATUS_API_BASE")
        or current_app.config.get("ARTIAC_STATUS_API_BASE")
        or legacy_base
        or DEFAULT_BASE_URL
    )
    return base.rstrip("/")


def _safe_json(resp: requests.Response) -> Dict[str, Any]:
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def fetch_status_payload(timeout: float = 4.0, *, force_refresh: bool = False) -> Dict[str, Any]:
    base = _get_api_base()
    params = {"refresh": "true"} if force_refresh else None
    resp = requests.get(f"{base}/status", params=params, timeout=timeout)
    resp.raise_for_status()
    return _safe_json(resp)


def fetch_status(timeout: float = 4.0, *, force_refresh: bool = False) -> Tuple[int, Dict[str, Any]]:
    base = _get_api_base()
    params = {"refresh": "true"} if force_refresh else None
    resp = requests.get(f"{base}/status", params=params, timeout=timeout)
    payload = _safe_json(resp)
    return resp.status_code, {
        "ok": resp.ok,
        "base_url": base,
        "data": payload,
    }


def trigger_job(timeout: float = 6.0) -> Tuple[int, Dict[str, Any]]:
    """Trigger only the predefined full Artiac CronJob.

    Deliberately accepts no caller payload, preventing Studio from requesting
    module-scoped runs, force publishing, or Artiac configuration changes.
    """
    base = _get_api_base()
    resp = requests.post(f"{base}/trigger", json={}, timeout=timeout)
    payload = _safe_json(resp)
    return resp.status_code, {
        "ok": resp.ok,
        "base_url": base,
        "data": payload,
    }
