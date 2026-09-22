from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from flask import Blueprint, jsonify, render_template, request

from .service import fetch_status, trigger_job, _get_api_base
from .archive.service.s3_client import clear_archive_cache


bp = Blueprint(
    "artiac_control",
    __name__,
    url_prefix="/artiac",
    template_folder="templates",
)


@dataclass
class _ArchiveRefreshState:
    pending_since: float | None = None


_archive_refresh_state = _ArchiveRefreshState()


def _schedule_stale_hours() -> int:
    try:
        return max(1, int(os.getenv("ARTIAC_SCHEDULE_STALE_HOURS", "36")))
    except ValueError:
        return 36


@bp.get("/")
def index():
    return render_template(
        "artiac_control/index.html",
        api_base=_get_api_base(),
        run_history_url=os.getenv("ARTIAC_RUN_HISTORY_URL", "").strip(),
        schedule_stale_hours=_schedule_stale_hours(),
    )


@bp.get("/api/status")
def api_status():
    try:
        force_refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes", "on"}
        status_code, payload = fetch_status(force_refresh=force_refresh)
    except Exception as exc:
        logging.exception("Error while fetching Artiac status")
        return jsonify({"ok": False, "error": "Internal server error", "base_url": _get_api_base()}), 502
    raw = payload.get("data") if isinstance(payload, dict) else None
    if _archive_refresh_state.pending_since is not None and isinstance(raw, dict):
        cronjob = raw.get("cronjob") if isinstance(raw.get("cronjob"), dict) else {}
        active = cronjob.get("active") or []
        jobs = raw.get("jobs") if isinstance(raw.get("jobs"), list) else []
        has_active_job = bool(active) or any(int(job.get("active", 0) or 0) > 0 for job in jobs if isinstance(job, dict))
        if not has_active_job and time.monotonic() - _archive_refresh_state.pending_since >= 5:
            clear_archive_cache()
            _archive_refresh_state.pending_since = None
    return jsonify(payload), status_code


@bp.post("/api/trigger")
def api_trigger():
    try:
        # Do not forward request JSON. Studio is intentionally limited to
        # triggering the complete, predefined CronJob.
        status_code, payload = trigger_job()
    except Exception as exc:
        logging.exception("Error while triggering Artiac job")
        return jsonify({"ok": False, "error": "Internal server error", "base_url": _get_api_base()}), 502
    if 200 <= status_code < 300:
        clear_archive_cache()
        _archive_refresh_state.pending_since = time.monotonic()
    return jsonify(payload), status_code
