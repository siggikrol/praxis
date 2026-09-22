from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from services.aws_instance_types.common import (
    acquire_lock,
    release_lock,
    read_json,
    write_json_atomic,
)

_touch_lock = threading.Lock()
_last_touch_by_sid: dict[str, float] = {}
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    return value if value > 0 else default


def _iso_utc(epoch: float | int | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _registry_path(session_dir: str) -> str:
    session_root = os.path.abspath(session_dir)
    override = (os.getenv("PS_ACTIVE_SESSIONS_REGISTRY") or "").strip()
    if override:
        override_path = os.path.abspath(override)
        try:
            inside_session_cache = os.path.commonpath([override_path, session_root]) == session_root
        except ValueError:
            inside_session_cache = False
        if not inside_session_cache:
            return override_path
        logger.warning(
            "Ignoring PS_ACTIVE_SESSIONS_REGISTRY inside Flask-Session cache directory: %s",
            override_path,
        )
    # Do not place application metadata inside Flask-Session's cache directory.
    # Its threshold cleanup owns that directory and may remove files it did not
    # create, which previously caused the active-user inventory to reset.
    session_parent = os.path.dirname(session_root)
    return os.path.join(session_parent, "active_sessions.registry.json")


def _read_registry(path: str) -> dict[str, Any]:
    data = read_json(path) or {}
    if not isinstance(data, dict):
        data = {}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    data["sessions"] = sessions
    known_users = data.get("known_users")
    if not isinstance(known_users, dict):
        known_users = {}
    data["known_users"] = known_users
    return data


def _should_touch_sid(sid: str, now: float, interval_seconds: int) -> bool:
    if interval_seconds <= 0:
        return True
    with _touch_lock:
        last = float(_last_touch_by_sid.get(sid, 0.0))
        if (now - last) < float(interval_seconds):
            return False
        _last_touch_by_sid[sid] = now
    return True


def touch_active_session(
    *,
    session_dir: str,
    sid: str | None,
    user: str | None,
    auth_method: str | None = None,
    login_ts: int | float | None = None,
    force: bool = False,
) -> dict[str, Any]:
    sid_value = (sid or "").strip()
    user_value = (user or "").strip()
    if not sid_value:
        return {"ok": False, "skipped": "missing_sid"}
    if not user_value:
        return {"ok": False, "skipped": "missing_user"}

    now = time.time()
    interval = _env_int("PS_ACTIVE_SESSION_TOUCH_INTERVAL_SECONDS", 30)
    if not force and not _should_touch_sid(sid_value, now, interval):
        return {"ok": True, "skipped": "throttled"}

    path = _registry_path(session_dir)
    lock_path = f"{path}.lock"
    lock_fh = acquire_lock(lock_path, blocking=True)

    try:
        data = _read_registry(path)
        sessions = data["sessions"]
        entry = sessions.get(sid_value)
        if not isinstance(entry, dict):
            entry = {}

        if login_ts is not None:
            try:
                entry["login_ts"] = float(login_ts)
            except Exception:
                entry["login_ts"] = now
        elif not isinstance(entry.get("login_ts"), (int, float)):
            entry["login_ts"] = now

        entry["sid"] = sid_value
        entry["user"] = user_value
        entry["auth_method"] = (auth_method or entry.get("auth_method") or "unknown").strip() or "unknown"
        entry["last_seen_ts"] = now
        entry["pod"] = os.getenv("HOSTNAME") or ""

        sessions[sid_value] = entry
        known_users = data["known_users"]
        if force or user_value not in known_users:
            login_epoch = float(entry.get("login_ts") or now)
            known_users[user_value] = {
                "user": user_value,
                "last_login_ts": login_epoch,
                "last_login_iso": _iso_utc(login_epoch),
            }
        data["updated_at"] = now
        data["updated_at_iso"] = _iso_utc(now)

        write_json_atomic(path, data)
        return {"ok": True, "registry_path": path}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "registry_path": path}
    finally:
        release_lock(lock_fh)


def list_known_users(*, session_dir: str) -> list[str]:
    """Return identities active in Praxis during the configured retention window."""
    path = _registry_path(session_dir)
    lock_fh = acquire_lock(f"{path}.lock", blocking=True)
    try:
        data = _read_registry(path)
        now = time.time()
        retention = _env_int("PS_KNOWN_USERS_RETENTION_SECONDS", 30 * 24 * 60 * 60)
        known_users = data["known_users"]
        expired = [
            user
            for user, entry in known_users.items()
            if not isinstance(entry, dict)
            or not isinstance(entry.get("last_login_ts"), (int, float))
            or (now - float(entry["last_login_ts"])) > retention
        ]
        for user in expired:
            known_users.pop(user, None)
        users = {str(user).strip() for user in known_users if str(user).strip()}
        # Include sessions written by versions predating the known-users index.
        users.update(
            str(entry.get("user") or "").strip()
            for entry in data["sessions"].values()
            if isinstance(entry, dict)
            and str(entry.get("user") or "").strip()
            and isinstance(entry.get("last_seen_ts"), (int, float))
            and (now - float(entry["last_seen_ts"])) <= retention
        )
        if expired:
            data["updated_at"] = now
            data["updated_at_iso"] = _iso_utc(now)
            write_json_atomic(path, data)
        return sorted(users, key=str.casefold)
    except Exception:
        return []
    finally:
        release_lock(lock_fh)


def remove_active_session(
    *,
    session_dir: str,
    sid: str | None = None,
    user: str | None = None,
) -> dict[str, Any]:
    sid_value = (sid or "").strip()
    user_value = (user or "").strip()
    if not sid_value and not user_value:
        return {"ok": False, "skipped": "missing_sid_and_user"}

    path = _registry_path(session_dir)
    lock_path = f"{path}.lock"
    lock_fh = acquire_lock(lock_path, blocking=True)

    try:
        data = _read_registry(path)
        sessions = data["sessions"]
        removed = 0

        if sid_value and sid_value in sessions:
            sessions.pop(sid_value, None)
            removed += 1
        elif user_value:
            stale_keys = []
            for k, v in sessions.items():
                if isinstance(v, dict) and str(v.get("user") or "") == user_value:
                    stale_keys.append(k)
            for k in stale_keys:
                sessions.pop(k, None)
            removed += len(stale_keys)

        data["updated_at"] = time.time()
        data["updated_at_iso"] = _iso_utc(data["updated_at"])
        write_json_atomic(path, data)
        return {"ok": True, "removed": removed, "registry_path": path}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "registry_path": path}
    finally:
        release_lock(lock_fh)


def list_active_sessions(
    *,
    session_dir: str,
    window_seconds: int | None = None,
) -> dict[str, Any]:
    now = time.time()
    window = int(window_seconds or _env_int("PS_ACTIVE_SESSIONS_WINDOW_SECONDS", 12 * 60 * 60))
    max_rows = _env_int("PS_ACTIVE_SESSIONS_MAX_ROWS", 50)
    path = _registry_path(session_dir)
    lock_path = f"{path}.lock"
    lock_fh = acquire_lock(lock_path, blocking=True)

    try:
        data = _read_registry(path)
        sessions = data["sessions"]
        entries: list[dict[str, Any]] = []
        stale_sids: list[str] = []

        for sid, raw in sessions.items():
            if not isinstance(raw, dict):
                stale_sids.append(sid)
                continue

            last_seen = raw.get("last_seen_ts")
            login_ts = raw.get("login_ts")
            if not isinstance(last_seen, (int, float)):
                last_seen = login_ts if isinstance(login_ts, (int, float)) else None
            if not isinstance(last_seen, (int, float)):
                stale_sids.append(sid)
                continue

            if (now - float(last_seen)) > float(window):
                stale_sids.append(sid)
                continue

            login_epoch = float(login_ts) if isinstance(login_ts, (int, float)) else None
            last_seen_epoch = float(last_seen)
            entries.append(
                {
                    "sid_short": str(sid)[:8],
                    "user": str(raw.get("user") or ""),
                    "auth_method": str(raw.get("auth_method") or "unknown"),
                    "pod": str(raw.get("pod") or ""),
                    "login_ts": login_epoch,
                    "login_ts_iso": _iso_utc(login_epoch),
                    "last_seen_ts": last_seen_epoch,
                    "last_seen_iso": _iso_utc(last_seen_epoch),
                    "last_seen_ago_seconds": max(0, int(now - last_seen_epoch)),
                }
            )

        if stale_sids:
            for sid in stale_sids:
                sessions.pop(sid, None)
            data["updated_at"] = now
            data["updated_at_iso"] = _iso_utc(now)
            write_json_atomic(path, data)

        entries.sort(key=lambda e: float(e.get("last_seen_ts") or 0.0), reverse=True)
        users = sorted({e.get("user") for e in entries if e.get("user")})
        truncated = len(entries) > max_rows
        visible_entries = entries[:max_rows]

        return {
            "ok": True,
            "registry_path": path,
            "window_seconds": window,
            "window_minutes": int(window / 60),
            "active_count": len(entries),
            "active_users_count": len(users),
            "users": users,
            "entries": visible_entries,
            "truncated": truncated,
            "scope_note": f"File-backed registry: {path}. Shared when this path is on shared storage.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "registry_path": path,
            "window_seconds": window,
            "window_minutes": int(window / 60),
            "active_count": 0,
            "active_users_count": 0,
            "users": [],
            "entries": [],
            "truncated": False,
            "scope_note": f"File-backed registry: {path}. Shared when this path is on shared storage.",
        }
    finally:
        release_lock(lock_fh)
