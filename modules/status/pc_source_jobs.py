from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

from flask import current_app


def _job_dir() -> str:
    # Keep jobs on local disk so any gunicorn worker can serve status polling.
    base = (
        os.getenv("PS_PC_SOURCE_JOB_DIR")
        or os.getenv("PS_PC_SOURCE_CACHE_DIR")
        or "/tmp/praxis-cache"
    )
    return os.path.join(base.rstrip("/"), "pc-source-jobs")


def _job_path(job_id: str) -> str:
    return os.path.join(_job_dir(), f"{job_id}.json")


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def get_job(job_id: str) -> dict[str, Any] | None:
    return _read_json(_job_path(job_id))


def start_refresh_job(*, branches: list[str | None]) -> dict[str, Any]:
    """
    Start an async refresh job for one or more branches.

    branches:
      - None means "default branch" (no ref)
      - otherwise the branch name (string)
    """
    job_id = uuid.uuid4().hex
    created_at = time.time()
    branch_tokens = [(b or "default") for b in (branches or [])]
    total_steps = max(1, len(branch_tokens) * 7)

    payload: dict[str, Any] = {
        "job_id": job_id,
        "created_at_epoch": created_at,
        "started_at_epoch": None,
        "finished_at_epoch": None,
        "state": "queued",  # queued | running | done | error
        "ok": None,
        "branches": branch_tokens,
        "total_steps": total_steps,
        "completed_steps": 0,
        "events": [],
        "results": {},
        "error": None,
    }
    _write_json_atomic(_job_path(job_id), payload)

    app = current_app._get_current_object()
    t = threading.Thread(
        target=_run_refresh_job_in_app_context,
        name=f"pc-source-refresh:{job_id}",
        args=(app, job_id, branches),
        daemon=True,
    )
    t.start()

    return {"ok": True, "job_id": job_id, "state": "queued"}


def _append_event(
    job: dict[str, Any],
    *,
    level: str,
    message: str,
    branch: str | None = None,
    section: str | None = None,
) -> None:
    ev = {
        "ts_epoch": time.time(),
        "level": level,
        "message": message,
        "branch": branch,
        "section": section,
    }
    events = job.get("events")
    if not isinstance(events, list):
        events = []
        job["events"] = events
    events.append(ev)


def _set_state(job: dict[str, Any], state: str) -> None:
    job["state"] = state


def _run_refresh_job_in_app_context(app, job_id: str, branches: list[str | None]) -> None:
    """Run the worker with the Flask application state used by PC Source providers."""
    with app.app_context():
        _run_refresh_job(job_id, branches)


def _run_refresh_job(job_id: str, branches: list[str | None]) -> None:
    path = _job_path(job_id)
    job = _read_json(path) or {}

    try:
        job["started_at_epoch"] = time.time()
        _set_state(job, "running")
        _append_event(job, level="info", message="Job started")
        _write_json_atomic(path, job)

        from services.pc_source_scanner.structure_cache import refresh_pc_source_sections

        results: dict[str, Any] = job.get("results") if isinstance(job.get("results"), dict) else {}
        job["results"] = results

        completed = int(job.get("completed_steps") or 0)

        for branch in (branches or [None]):
            branch_display = branch or "default"

            _append_event(job, level="info", message=f"Refreshing pc_source in branch={branch_display}...", branch=branch_display)
            _write_json_atomic(path, job)

            def on_event(level: str, message: str, section: str | None) -> None:
                _append_event(job, level=level, message=message, branch=branch_display, section=section)
                _write_json_atomic(path, job)

            def on_step() -> None:
                nonlocal completed
                completed += 1
                job["completed_steps"] = completed
                _write_json_atomic(path, job)

            res = refresh_pc_source_sections(branch, force_refresh=True, on_event=on_event, on_step=on_step)
            results[branch_display] = res

            if (res or {}).get("ok"):
                _append_event(job, level="info", message=f"Done branch={branch_display}", branch=branch_display)
            else:
                _append_event(job, level="warning", message=f"Failed branch={branch_display}", branch=branch_display)
            _write_json_atomic(path, job)

        # Determine overall OK.
        ok_all = True
        for b in (branches or [None]):
            bd = b or "default"
            if not (results.get(bd) or {}).get("ok"):
                ok_all = False
        job["ok"] = ok_all
        job["finished_at_epoch"] = time.time()
        _set_state(job, "done")
        _append_event(job, level="info", message="Job finished")
        _write_json_atomic(path, job)

    except Exception as exc:
        job = _read_json(path) or job
        job["ok"] = False
        job["error"] = str(exc)
        job["finished_at_epoch"] = time.time()
        _set_state(job, "error")
        _append_event(job, level="error", message=f"Job crashed: {exc}")
        _write_json_atomic(path, job)
