from __future__ import annotations

import json
import os
import posixpath
import sqlite3
import time
import uuid
from typing import Any, Mapping


DEFAULT_DB_PATH = "/tmp/praxis-cache/wizard-workspaces.sqlite3"
_TIMEOUT_SECONDS = 30.0
_BUSY_TIMEOUT_MS = 30_000
VALID_PERMISSIONS = {"viewer", "editor"}


def current_owner(session_obj: Mapping[str, Any]) -> str:
    return str(session_obj.get("user") or "local-user").strip() or "local-user"


def _db_path() -> str:
    return os.path.abspath(
        os.path.normpath(
            (os.getenv("PS_WIZARD_WORKSPACES_DB_PATH") or DEFAULT_DB_PATH).strip()
        )
    )


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wizard_workspaces (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                env_slug TEXT NOT NULL,
                title TEXT NOT NULL,
                current_step TEXT NOT NULL DEFAULT 'project_settings',
                state_json TEXT NOT NULL DEFAULT '{}',
                spec_yaml TEXT NOT NULL DEFAULT '',
                latest_job_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_wizard_workspaces_owner_updated
                ON wizard_workspaces(owner, updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_wizard_workspaces_latest_job
                ON wizard_workspaces(latest_job_id);

            CREATE TABLE IF NOT EXISTS wizard_workspace_validations (
                workspace_id TEXT NOT NULL,
                job_id TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                PRIMARY KEY (workspace_id, job_id),
                FOREIGN KEY (workspace_id) REFERENCES wizard_workspaces(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_wizard_workspace_validations_workspace
                ON wizard_workspace_validations(workspace_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS wizard_workspace_shares (
                workspace_id TEXT NOT NULL,
                shared_with TEXT NOT NULL,
                permission TEXT NOT NULL CHECK(permission IN ('viewer', 'editor')),
                shared_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (workspace_id, shared_with),
                FOREIGN KEY (workspace_id) REFERENCES wizard_workspaces(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_wizard_workspace_shares_user
                ON wizard_workspace_shares(shared_with, workspace_id);
            """
        )

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(wizard_workspaces)")}
        if "workspace_origin" not in columns:
            conn.execute("ALTER TABLE wizard_workspaces ADD COLUMN workspace_origin TEXT NOT NULL DEFAULT ''")
        if "readiness_setup_id" not in columns:
            conn.execute("ALTER TABLE wizard_workspaces ADD COLUMN readiness_setup_id TEXT NOT NULL DEFAULT ''")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _session_snapshot(session_obj: Mapping[str, Any], env_slug: str) -> dict[str, Any]:
    prefix = f"{env_slug}:"
    state = {
        key: value
        for key, value in session_obj.items()
        if key.startswith(prefix)
    }
    if session_obj.get("release_selection"):
        state["release_selection"] = session_obj.get("release_selection")
    return state


def _derive_title(env_slug: str, state: Mapping[str, Any]) -> str:
    common = state.get(f"{env_slug}:common") or {}
    repo = state.get(f"{env_slug}:repository_settings") or {}
    release = state.get("release_selection") or {}
    project = state.get(f"{env_slug}:project_settings") or {}
    for candidate in (
        project.get("environment_name") if isinstance(project, dict) else "",
        common.get("environment") if isinstance(common, dict) else "",
        repo.get("new_prefix") if isinstance(repo, dict) else "",
        release.get("customer") if isinstance(release, dict) else "",
    ):
        value = str(candidate or "").strip()
        if value:
            return value[:180]
    return env_slug.replace("-", " ").title()


def workspace_environment_names(workspace: Mapping[str, Any]) -> list[str]:
    """Return selected environment names, including configured suffixes."""
    env_slug = str(workspace.get("env_slug") or "").strip()
    try:
        state = json.loads(str(workspace.get("state_json") or "{}"))
    except (TypeError, ValueError):
        return []
    project = state.get(f"{env_slug}:project_settings") if isinstance(state, dict) else None
    if not isinstance(project, dict):
        return []

    selected = project.get("environment_type") or []
    if isinstance(selected, str):
        selected = [selected]
    suffixes_raw = project.get("environment_suffixes") or {}
    if isinstance(suffixes_raw, str):
        try:
            suffixes = json.loads(suffixes_raw)
        except (TypeError, ValueError):
            suffixes = {}
    else:
        suffixes = suffixes_raw
    if not isinstance(suffixes, dict):
        suffixes = {}

    names: list[str] = []
    for raw_env in selected:
        env = str(raw_env or "").strip().lower()
        if not env:
            continue
        base = env.split("-", 1)[0]
        suffix = str(suffixes.get(base) or "").strip().lower()
        name = env if "-" in env else (f"{base}-{suffix}" if suffix else base)
        if name not in names:
            names.append(name)
    return names


def workspace_release_details(workspace: Mapping[str, Any]) -> dict[str, str]:
    """Return display-safe release metadata from a workspace snapshot."""
    try:
        state = json.loads(str(workspace.get("state_json") or "{}"))
    except (TypeError, ValueError):
        state = {}
    release = state.get("release_selection") if isinstance(state, dict) else None
    if not isinstance(release, dict):
        return {"customer": "", "key": "", "name": ""}

    customer = str(release.get("customer") or "").strip()
    key = str(release.get("release_key") or "").strip()
    return {
        "customer": customer,
        "key": key,
        "name": posixpath.basename(key.rstrip("/")) if key else "",
    }


def create_workspace(
    *, owner: str, env_slug: str, state: Mapping[str, Any] | None = None,
    current_step: str = "project_settings", readiness_setup_id: str = "",
) -> dict[str, Any]:
    init_db()
    now = time.time()
    workspace_id = uuid.uuid4().hex
    state = dict(state or {})
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO wizard_workspaces (
                id, owner, env_slug, title, current_step, state_json,
                spec_yaml, latest_job_id, created_at, updated_at, readiness_setup_id, workspace_origin
            ) VALUES (?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)
            """,
            (
                workspace_id, owner, env_slug, _derive_title(env_slug, state),
                current_step, json.dumps(state), now, now, readiness_setup_id, "readiness" if readiness_setup_id else "direct",
            ),
        )
        row = conn.execute(
            "SELECT * FROM wizard_workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
    result = _row(row)
    assert result is not None
    return result


def get_workspace_for_user(user: str, workspace_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT w.*,
                   CASE WHEN w.owner = ? THEN 'owner' ELSE s.permission END AS access
            FROM wizard_workspaces w
            LEFT JOIN wizard_workspace_shares s
              ON s.workspace_id = w.id AND s.shared_with = ?
            WHERE w.id = ? AND (w.owner = ? OR s.shared_with = ?)
            """,
            (user, user, workspace_id, user, user),
        ).fetchone()
    return _row(row)


def get_workspace_by_job(user: str, job_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT w.*,
                   CASE WHEN w.owner = ? THEN 'owner' ELSE s.permission END AS access
            FROM wizard_workspaces w
            LEFT JOIN wizard_workspace_shares s
              ON s.workspace_id = w.id AND s.shared_with = ?
            WHERE (
                    w.latest_job_id = ?
                    OR EXISTS (
                        SELECT 1 FROM wizard_workspace_validations v
                        WHERE v.workspace_id = w.id AND v.job_id = ?
                    )
                  )
              AND (w.owner = ? OR s.shared_with = ?)
            ORDER BY w.updated_at DESC LIMIT 1
            """,
            (user, user, job_id, job_id, user, user),
        ).fetchone()
    return _row(row)


def list_workspaces(user: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    init_db()
    with _connect() as conn:
        owned = conn.execute(
            """SELECT w.*, 'owner' AS access FROM wizard_workspaces w
               WHERE w.owner = ? ORDER BY w.updated_at DESC""",
            (user,),
        ).fetchall()
        shared = conn.execute(
            """
            SELECT w.*, s.permission AS access
            FROM wizard_workspaces w
            JOIN wizard_workspace_shares s ON s.workspace_id = w.id
            WHERE s.shared_with = ? AND w.owner <> ?
            ORDER BY w.updated_at DESC
            """,
            (user, user),
        ).fetchall()
    return [dict(item) for item in owned], [dict(item) for item in shared]


def delete_workspace(*, owner: str, workspace_id: str) -> bool:
    """Delete an owned workspace and its access/history associations."""
    init_db()
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM wizard_workspaces WHERE id = ? AND owner = ?",
            (workspace_id, owner),
        )
    return cursor.rowcount > 0


def sync_workspace(
    *, workspace_id: str, owner: str, env_slug: str,
    session_obj: Mapping[str, Any], current_step: str | None = None,
    spec_yaml: str | None = None, latest_job_id: str | None = None,
) -> dict[str, Any] | None:
    init_db()
    state = _session_snapshot(session_obj, env_slug)
    updates = ["title = ?", "state_json = ?", "updated_at = ?"]
    params: list[Any] = [
        _derive_title(env_slug, state), json.dumps(state), time.time()
    ]
    if current_step is not None:
        updates.append("current_step = ?")
        params.append(current_step)
    if spec_yaml is not None:
        updates.append("spec_yaml = ?")
        params.append(spec_yaml)
    if latest_job_id is not None:
        updates.append("latest_job_id = ?")
        params.append(latest_job_id)
    params.extend([workspace_id, owner])
    with _connect() as conn:
        conn.execute(
            f"UPDATE wizard_workspaces SET {', '.join(updates)} WHERE id = ? AND owner = ?",
            params,
        )
        if latest_job_id:
            conn.execute(
                """
                INSERT INTO wizard_workspace_validations (workspace_id, job_id, created_at)
                SELECT id, ?, ? FROM wizard_workspaces
                WHERE id = ? AND owner = ?
                ON CONFLICT(job_id) DO NOTHING
                """,
                (latest_job_id, time.time(), workspace_id, owner),
            )
        row = conn.execute(
            "SELECT * FROM wizard_workspaces WHERE id = ? AND owner = ?",
            (workspace_id, owner),
        ).fetchone()
    return _row(row)


def load_workspace_state(workspace: Mapping[str, Any], session_obj: Any) -> None:
    env_slug = str(workspace.get("env_slug") or "")
    prefix = f"{env_slug}:"
    for key in list(session_obj.keys()):
        if key.startswith(prefix):
            session_obj.pop(key, None)
    try:
        state = json.loads(str(workspace.get("state_json") or "{}"))
    except (TypeError, ValueError):
        state = {}
    if isinstance(state, dict):
        for key, value in state.items():
            if key.startswith(prefix) or key == "release_selection":
                session_obj[key] = value
    if workspace.get("spec_yaml"):
        session_obj[f"{env_slug}:spec_yaml"] = workspace["spec_yaml"]
    if workspace.get("latest_job_id"):
        session_obj[f"{env_slug}:core_validate_latest_job_id"] = workspace["latest_job_id"]
    session_obj[f"{env_slug}:workspace_id"] = workspace["id"]
    session_obj.modified = True


def ensure_active_workspace(session_obj: Any, env_slug: str) -> dict[str, Any]:
    owner = current_owner(session_obj)
    key = f"{env_slug}:workspace_id"
    workspace_id = str(session_obj.get(key) or "").strip()
    if workspace_id:
        workspace = get_workspace_for_user(owner, workspace_id)
        if workspace:
            return workspace
        # The workspace was deleted or this user's access was revoked. Do not
        # clone its stale session snapshot into a new user-owned workspace.
        prefix = f"{env_slug}:"
        for session_key in list(session_obj.keys()):
            if session_key.startswith(prefix):
                session_obj.pop(session_key, None)
        session_obj.modified = True
    workspace = create_workspace(
        owner=owner,
        env_slug=env_slug,
        state=_session_snapshot(session_obj, env_slug),
    )
    session_obj[key] = workspace["id"]
    session_obj.modified = True
    return workspace


def start_new_workspace(session_obj: Any, env_slug: str) -> dict[str, Any]:
    """Start a separate private setup while retaining older workspaces."""
    owner = current_owner(session_obj)
    prefix = f"{env_slug}:"
    for key in list(session_obj.keys()):
        if key.startswith(prefix):
            session_obj.pop(key, None)
    from engine.wizards.components import new_component_profile

    profile = new_component_profile(env_slug)
    if profile:
        session_obj[f"{env_slug}:component_profile"] = profile
    workspace = create_workspace(
        owner=owner,
        env_slug=env_slug,
        state=_session_snapshot(session_obj, env_slug),
    )
    session_obj[f"{env_slug}:workspace_id"] = workspace["id"]
    session_obj.modified = True
    return workspace


def change_workspace_release(
    session_obj: Any,
    env_slug: str,
    release_selection: Mapping[str, Any],
    *,
    return_step: str,
    valid_steps: list[str],
) -> tuple[dict[str, Any], str]:
    """Change an active workspace's release without discarding its form data."""
    workspace = ensure_active_workspace(session_obj, env_slug)
    if workspace.get("access") == "viewer":
        raise PermissionError("View-only workspaces cannot change releases")

    target_step = return_step if return_step in valid_steps else ""
    if not target_step:
        saved_step = str(workspace.get("current_step") or "")
        target_step = saved_step if saved_step in valid_steps else valid_steps[0]

    session_obj["release_selection"] = dict(release_selection)
    # A release change invalidates generated output, but not the user's inputs.
    for suffix in (
        "spec_yaml",
        "core_validate_ok_spec_hash",
        "core_validate_latest_job_id",
    ):
        session_obj.pop(f"{env_slug}:{suffix}", None)
    session_obj.modified = True

    updated = sync_workspace(
        workspace_id=str(workspace["id"]),
        owner=str(workspace["owner"]),
        env_slug=env_slug,
        session_obj=session_obj,
        current_step=target_step,
        spec_yaml="",
        latest_job_id="",
    )
    return updated or workspace, target_step


def set_share(
    *, owner: str, workspace_id: str, shared_with: str, permission: str,
) -> bool:
    shared_with = (shared_with or "").strip()
    permission = (permission or "").strip().lower()
    if not shared_with or shared_with == owner or permission not in VALID_PERMISSIONS:
        return False
    init_db()
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM wizard_workspaces WHERE id = ? AND owner = ?",
            (workspace_id, owner),
        ).fetchone()
        if not exists:
            return False
        conn.execute(
            """
            INSERT INTO wizard_workspace_shares (
                workspace_id, shared_with, permission, shared_by, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, shared_with)
            DO UPDATE SET permission = excluded.permission,
                          shared_by = excluded.shared_by,
                          created_at = excluded.created_at
            """,
            (workspace_id, shared_with, permission, owner, time.time()),
        )
    return True


def handoff_workspace_access(
    *, workspace_id: str, current_assignee: str, new_assignee: str,
) -> dict[str, str] | None:
    """Move an assigned user's editor access to their replacement.

    When the current assignee owns the workspace, ownership moves to the
    replacement. Otherwise, their share is removed and the replacement gets
    editor access while the original workspace owner remains unchanged.
    """
    current_assignee = (current_assignee or "").strip()
    new_assignee = (new_assignee or "").strip()
    if not workspace_id or not current_assignee or not new_assignee:
        return None
    if current_assignee.casefold() == new_assignee.casefold():
        return None
    init_db()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        workspace = conn.execute(
            "SELECT owner FROM wizard_workspaces WHERE id = ?",
            (workspace_id,),
        ).fetchone()
        if workspace is None:
            return None
        workspace_owner = str(workspace["owner"] or "").strip()
        conn.execute(
            "DELETE FROM wizard_workspace_shares WHERE workspace_id = ? AND lower(shared_with) = lower(?)",
            (workspace_id, new_assignee),
        )
        if workspace_owner.casefold() == current_assignee.casefold():
            updated = conn.execute(
                """UPDATE wizard_workspaces SET owner = ?, updated_at = ?
                    WHERE id = ? AND lower(owner) = lower(?)""",
                (new_assignee, time.time(), workspace_id, current_assignee),
            )
            if updated.rowcount != 1:
                return None
            conn.execute(
                "DELETE FROM wizard_workspace_shares WHERE workspace_id = ? AND lower(shared_with) = lower(?)",
                (workspace_id, current_assignee),
            )
            return {"mode": "owner", "owner": new_assignee}

        conn.execute(
            "DELETE FROM wizard_workspace_shares WHERE workspace_id = ? AND lower(shared_with) = lower(?)",
            (workspace_id, current_assignee),
        )
        if workspace_owner.casefold() != new_assignee.casefold():
            conn.execute(
                """INSERT INTO wizard_workspace_shares (
                       workspace_id, shared_with, permission, shared_by, created_at
                   ) VALUES (?, ?, 'editor', ?, ?)""",
                (workspace_id, new_assignee, workspace_owner, time.time()),
            )
        return {"mode": "editor", "owner": workspace_owner}


def remove_share(*, owner: str, workspace_id: str, shared_with: str) -> bool:
    init_db()
    with _connect() as conn:
        cursor = conn.execute(
            """
            DELETE FROM wizard_workspace_shares
            WHERE workspace_id = ? AND shared_with = ?
              AND EXISTS (
                SELECT 1 FROM wizard_workspaces
                WHERE id = ? AND owner = ?
              )
            """,
            (workspace_id, shared_with, workspace_id, owner),
        )
    return cursor.rowcount > 0


def list_shares(owner: str, workspace_id: str) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM wizard_workspace_shares s
            JOIN wizard_workspaces w ON w.id = s.workspace_id
            WHERE s.workspace_id = ? AND w.owner = ?
            ORDER BY s.shared_with
            """,
            (workspace_id, owner),
        ).fetchall()
    return [dict(item) for item in rows]


def annotate_workspace_origins(
    workspaces: list[dict[str, Any]], setups: dict[str, dict[str, Any]]
) -> None:
    """Retain gate provenance separately from editable wizard state and ownership."""
    backfill = []
    for workspace in workspaces:
        setup = setups.get(str(workspace["id"]))
        source = str(workspace.get("readiness_setup_id") or "")
        if setup and not source:
            source = str(setup["id"])
            backfill.append((source, workspace["id"]))
            workspace["readiness_setup_id"] = source
        workspace["origin_label"] = "Readiness Gate" if source else ("Created directly" if workspace.get("workspace_origin") == "direct" else "Origin unknown")
        workspace["origin_setup"] = setup
    if backfill:
        init_db()
        with _connect() as conn:
            conn.executemany(
                "UPDATE wizard_workspaces SET readiness_setup_id = ?, workspace_origin = 'readiness' WHERE id = ? AND readiness_setup_id = ''",
                backfill,
            )
