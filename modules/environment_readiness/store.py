from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any
from uuid import uuid4

from flask import current_app

WIZARD_SETUP_TYPES = {"catalyst-single-vpc", "catalyst-multi-vpc", "rgs", "loyalty"}


class DraftWriteConflict(ValueError):
    """A stale browser attempted to overwrite a newer draft version."""


def _database_path() -> Path:
    configured = current_app.config.get("ENVIRONMENT_READINESS_DB") or os.getenv(
        "PS_ENVIRONMENT_READINESS_DB"
    )
    return Path(configured or "/var/lib/praxis-environment-readiness/environment-readiness.sqlite3")


def _connect() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS environment_setups (
            id TEXT PRIMARY KEY,
            environment_name TEXT NOT NULL,
            customer TEXT NOT NULL,
            setup_type TEXT NOT NULL,
            devops_owner TEXT NOT NULL,
            target_date TEXT NOT NULL,
            status TEXT NOT NULL,
            published_by TEXT NOT NULL,
            setup_owner TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL,
            acknowledged_by TEXT NOT NULL DEFAULT '',
            acknowledged_at TEXT NOT NULL DEFAULT '',
            started_by TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            completed_by TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL
        )
        """
    )
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(environment_setups)").fetchall()
    }
    for name, definition in (
        ("logical_id", "TEXT NOT NULL DEFAULT ''"),
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ("is_current", "INTEGER NOT NULL DEFAULT 1"),
        ("aws_account_id", "TEXT NOT NULL DEFAULT ''"),
        ("jira_epic", "TEXT NOT NULL DEFAULT ''"),
        ("ncr_ticket", "TEXT NOT NULL DEFAULT ''"),
        ("architect", "TEXT NOT NULL DEFAULT ''"),
        ("requester", "TEXT NOT NULL DEFAULT ''"),
        ("setup_owner", "TEXT NOT NULL DEFAULT ''"),
        ("source_draft_id", "TEXT NOT NULL DEFAULT ''"),
        ("draft_created_at", "TEXT NOT NULL DEFAULT ''"),
        ("completed_at", "TEXT NOT NULL DEFAULT ''"),
        ("completed_by", "TEXT NOT NULL DEFAULT ''"),
        ("acknowledged_by", "TEXT NOT NULL DEFAULT ''"),
        ("acknowledged_at", "TEXT NOT NULL DEFAULT ''"),
        ("wizard_workspace_id", "TEXT NOT NULL DEFAULT ''"),
        ("wizard_slug", "TEXT NOT NULL DEFAULT ''"),
        ("wizard_owner", "TEXT NOT NULL DEFAULT ''"),
        ("bootstrap_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE environment_setups ADD COLUMN {name} {definition}")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS environment_setup_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT NOT NULL,
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            changed_by TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            previous_target_date TEXT NOT NULL DEFAULT '',
            new_target_date TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (setup_id) REFERENCES environment_setups(id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS environment_drafts (
            id TEXT NOT NULL,
            owner TEXT NOT NULL,
            environment_name TEXT NOT NULL DEFAULT '',
            customer TEXT NOT NULL DEFAULT '',
            current_step INTEGER NOT NULL DEFAULT 1,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (id, owner)
        )
        """
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS environment_setup_clarifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logical_id TEXT NOT NULL, setup_id TEXT NOT NULL,
            requested_by TEXT NOT NULL, requested_at TEXT NOT NULL, reason TEXT NOT NULL,
            resolved_by TEXT NOT NULL DEFAULT '', resolved_at TEXT NOT NULL DEFAULT '',
            response TEXT NOT NULL DEFAULT ''
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS environment_drafts_owner_updated ON environment_drafts(owner, updated_at DESC)"
    )
    draft_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(environment_drafts)").fetchall()
    }
    if "version" not in draft_columns:
        connection.execute(
            "ALTER TABLE environment_drafts ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS environment_servicenow_submissions (
               draft_id TEXT NOT NULL,
               owner TEXT NOT NULL,
               request_type TEXT NOT NULL CHECK(request_type IN ('aws', 'ncr')),
               status TEXT NOT NULL CHECK(status IN ('pending', 'completed')),
               request_number TEXT NOT NULL DEFAULT '',
               request_url TEXT NOT NULL DEFAULT '',
               updated_at TEXT NOT NULL,
               PRIMARY KEY (draft_id, owner, request_type)
           )"""
    )
    submission_columns = {row['name'] for row in connection.execute('PRAGMA table_info(environment_servicenow_submissions)')}
    if 'snapshot_json' not in submission_columns:
        connection.execute("ALTER TABLE environment_servicenow_submissions ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '{}'")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS environment_setup_ownership_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               logical_id TEXT NOT NULL,
               from_owner TEXT NOT NULL,
               to_owner TEXT NOT NULL,
               changed_by TEXT NOT NULL,
               changed_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS environment_setup_devops_owner_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               logical_id TEXT NOT NULL,
               from_owner TEXT NOT NULL,
               to_owner TEXT NOT NULL,
               changed_by TEXT NOT NULL,
               changed_at TEXT NOT NULL,
               reason TEXT NOT NULL DEFAULT ''
           )"""
    )
    event_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(environment_setup_events)").fetchall()
    }
    for name in ("previous_target_date", "new_target_date"):
        if name not in event_columns:
            connection.execute(
                f"ALTER TABLE environment_setup_events ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )
    _migrate_legacy_revisions(connection)
    _backfill_search_columns(connection)
    _backfill_completed_at(connection)
    _backfill_completed_by(connection)
    _backfill_acknowledgement(connection)
    _backfill_source_draft_ids(connection)
    _backfill_setup_owners(connection)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS environment_setups_published_at ON environment_setups(published_at DESC)"
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS environment_setup_ownership_events_logical
           ON environment_setup_ownership_events(logical_id, id DESC)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS environment_setup_devops_owner_events_logical
           ON environment_setup_devops_owner_events(logical_id, id DESC)"""
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS environment_setups_logical_revision ON environment_setups(logical_id, revision)"
    )
    _merge_duplicate_current_environments(connection)
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS environment_setups_current_environment
           ON environment_setups(lower(trim(environment_name))) WHERE is_current = 1"""
    )
    connection.commit()
    return connection


def _backfill_setup_owners(connection: sqlite3.Connection) -> None:
    """Give legacy logical setups the owner of their current source draft.

    Older rows only recorded an editable requester and the publishing identity.
    The durable draft owner is the strongest available ownership signal, with
    the publisher and requester retained as safe migration fallbacks.
    """
    logical_ids = connection.execute(
        """SELECT DISTINCT logical_id FROM environment_setups
             WHERE setup_owner = '' AND logical_id <> ''"""
    ).fetchall()
    for logical in logical_ids:
        current = connection.execute(
            """SELECT source_draft_id, published_by, requester
                 FROM environment_setups
                WHERE logical_id = ?
                ORDER BY is_current DESC, revision DESC
                LIMIT 1""",
            (logical["logical_id"],),
        ).fetchone()
        if current is None:
            continue
        draft_owner = None
        source_draft_id = str(current["source_draft_id"] or "")
        if source_draft_id:
            draft_owner = connection.execute(
                """SELECT owner FROM environment_drafts
                    WHERE id = ?
                    ORDER BY CASE WHEN lower(owner) = lower(?) THEN 0 ELSE 1 END,
                             updated_at DESC
                    LIMIT 1""",
                (source_draft_id, str(current["published_by"] or "")),
            ).fetchone()
        owner = (
            str(draft_owner["owner"] or "") if draft_owner is not None else ""
        ) or str(current["published_by"] or "") or str(current["requester"] or "") or "Unknown user"
        connection.execute(
            "UPDATE environment_setups SET setup_owner = ? WHERE logical_id = ? AND setup_owner = ''",
            (owner, logical["logical_id"]),
        )


def _backfill_source_draft_ids(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT id, payload_json FROM environment_setups WHERE source_draft_id = ''"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        source_draft_id = str(payload.get("_draft_id") or "") if isinstance(payload, dict) else ""
        if source_draft_id:
            connection.execute(
                "UPDATE environment_setups SET source_draft_id = ? WHERE id = ?",
                (source_draft_id, row["id"]),
            )


def save_draft(
    draft_id: str, owner: str, payload: dict[str, Any], *, expected_version: int | None = None
) -> dict[str, Any]:
    """Create or replace a durable draft owned by one authenticated user."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    data = dict(payload)
    data["_draft_id"] = draft_id
    data["_saved_at"] = str(data.get("_saved_at") or now)
    try:
        current_step = max(1, min(7, int(data.get("_step") or 1)))
    except (TypeError, ValueError):
        current_step = 1
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT created_at, version, payload_json FROM environment_drafts WHERE id = ? AND owner = ?",
            (draft_id, owner),
        ).fetchone()
        completed_source = connection.execute(
            """SELECT id FROM environment_setups
                WHERE source_draft_id = ? AND status = 'completed'
                ORDER BY is_current DESC, revision DESC LIMIT 1""",
            (draft_id,),
        ).fetchone()
        if completed_source is not None:
            raise DraftWriteConflict(
                "Completed setup source drafts are read-only. Reopen the setup to create a new revision draft."
            )
        environment_name = str(data.get("environment_name") or "").strip()
        current_setup = None
        if environment_name:
            current_setup = connection.execute(
                """SELECT id, source_draft_id, status
                     FROM environment_setups
                    WHERE is_current = 1
                      AND lower(trim(environment_name)) = lower(trim(?))
                    ORDER BY published_at DESC, id DESC
                    LIMIT 1""",
                (environment_name,),
            ).fetchone()
        if current_setup is not None and str(current_setup["source_draft_id"] or "") != draft_id:
            reopened_from_setup_id = ""
            if existing is not None:
                try:
                    existing_payload = json.loads(str(existing["payload_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_payload = {}
                if isinstance(existing_payload, dict):
                    reopened_from_setup_id = str(
                        existing_payload.get("_reopened_from_setup_id") or ""
                    )
            authorized_reopen = (
                str(current_setup["status"] or "") == "completed"
                and reopened_from_setup_id == str(current_setup["id"])
            )
            if not authorized_reopen:
                raise DraftWriteConflict(
                    f"A current setup already exists for {environment_name}. "
                    "Open the existing setup instead of creating another environment draft."
                )
        from .workflow import invalidate_dependencies
        previous_payload = json.loads(existing["payload_json"]) if existing else {}
        data = invalidate_dependencies(data, previous_payload)
        current_version = int(existing["version"]) if existing is not None else 0
        if expected_version is not None and expected_version != current_version:
            raise DraftWriteConflict(
                "This draft changed in another tab or verification session. Reload it before saving again."
            )
        next_version = current_version + 1
        data["_version"] = next_version
        data["_created_at"] = str(
            data.get("_created_at") or (existing["created_at"] if existing else now)
        )
        connection.execute(
            """
            INSERT INTO environment_drafts
                (id, owner, environment_name, customer, current_step, version, created_at, updated_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id, owner) DO UPDATE SET
                environment_name = excluded.environment_name,
                customer = excluded.customer,
                current_step = excluded.current_step,
                version = excluded.version,
                updated_at = excluded.updated_at,
                payload_json = excluded.payload_json
            """,
            (
                draft_id,
                owner,
                str(data.get("environment_name") or ""),
                str(data.get("customer") or ""),
                current_step,
                next_version,
                now,
                now,
                json.dumps(data),
            ),
        )
    return data


def list_drafts(owner: str) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT id, version, payload_json FROM environment_drafts WHERE owner = ? ORDER BY updated_at DESC, id",
            (owner,),
        ).fetchall()
    drafts: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["_draft_id"] = str(row["id"])
            payload["_version"] = int(row["version"])
            drafts.append(payload)
    return drafts


def list_all_drafts() -> list[dict[str, Any]]:
    """Return team draft metadata while retaining each draft's owning identity."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT id, owner, version, payload_json FROM environment_drafts ORDER BY updated_at DESC, id"
        ).fetchall()
    drafts: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["_draft_id"] = str(row["id"])
            payload["_owner"] = str(row["owner"])
            payload["_version"] = int(row["version"])
            drafts.append(payload)
    return drafts


def find_active_draft_by_environment(
    environment_name: str, *, exclude_draft_id: str = "", exclude_owner: str = ""
) -> dict[str, str] | None:
    """Find a team draft for an environment that has not already become a setup."""
    normalized = environment_name.strip()
    if not normalized:
        return None
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT d.id, d.owner, d.environment_name
              FROM environment_drafts d
             WHERE lower(trim(d.environment_name)) = lower(trim(?))
               AND NOT (d.id = ? AND d.owner = ?)
               AND NOT EXISTS (
                    SELECT 1 FROM environment_setups s
                     WHERE s.source_draft_id = d.id AND s.is_current = 1
               )
             ORDER BY d.updated_at DESC
             LIMIT 1
            """,
            (normalized, exclude_draft_id, exclude_owner),
        ).fetchone()
    return dict(row) if row is not None else None


def get_draft(draft_id: str, owner: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT version, payload_json FROM environment_drafts WHERE id = ? AND owner = ?",
            (draft_id, owner),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["_version"] = int(row["version"])
    return payload


def find_completed_setup_by_source_draft(draft_id: str) -> dict[str, Any] | None:
    """Return the completed snapshot that permanently locks a source draft."""
    normalized = draft_id.strip()
    if not normalized:
        return None
    with _connect() as connection:
        row = connection.execute(
            """SELECT * FROM environment_setups
                WHERE source_draft_id = ? AND status = 'completed'
                ORDER BY is_current DESC, revision DESC LIMIT 1""",
            (normalized,),
        ).fetchone()
    return _decode(row)


def delete_draft(draft_id: str, owner: str) -> bool:
    with _connect() as connection:
        completed_source = connection.execute(
            """SELECT id FROM environment_setups
                WHERE source_draft_id = ? AND status = 'completed'
                ORDER BY is_current DESC, revision DESC LIMIT 1""",
            (draft_id,),
        ).fetchone()
        if completed_source is not None:
            raise ValueError(
                "Completed setup source drafts are read-only and cannot be deleted."
            )
        connection.execute(
            "DELETE FROM environment_servicenow_submissions WHERE draft_id = ? AND owner = ?",
            (draft_id, owner),
        )
        cursor = connection.execute(
            "DELETE FROM environment_drafts WHERE id = ? AND owner = ?",
            (draft_id, owner),
        )
    return cursor.rowcount > 0


def reopen_completed_setup(
    setup_id: str, *, owner: str, reason: str, target_date: str
) -> dict[str, Any] | None:
    """Clone a completed snapshot into a new owner-controlled revision draft."""
    actor = owner.strip()
    explanation = reason.strip()[:1000]
    revised_target = target_date.strip()
    if not actor:
        raise ValueError("The setup owner identity is required.")
    if not explanation:
        raise ValueError("Explain why the completed setup is being reopened.")
    try:
        parsed_target = date.fromisoformat(revised_target)
    except ValueError as exc:
        raise ValueError("Select a valid new target date.") from exc
    if parsed_target < datetime.now(timezone.utc).date():
        raise ValueError("The new target date cannot be in the past.")

    reopened_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT id, logical_id, revision, is_current, status, setup_owner,
                      environment_name, customer, payload_json
                 FROM environment_setups WHERE id = ?""",
            (setup_id,),
        ).fetchone()
        if row is None:
            return None
        if not int(row["is_current"]):
            raise ValueError("Only the current completed revision can be reopened.")
        if str(row["status"] or "") != "completed":
            raise ValueError("Only a completed setup can be reopened.")
        setup_owner = str(row["setup_owner"] or "").strip()
        if setup_owner.casefold() != actor.casefold():
            raise ValueError("Only the setup owner can reopen a completed setup.")
        active_draft = connection.execute(
            """SELECT d.id
                  FROM environment_drafts d
                 WHERE lower(trim(d.environment_name)) = lower(trim(?))
                   AND NOT EXISTS (
                        SELECT 1 FROM environment_setups s
                         WHERE s.source_draft_id = d.id AND s.is_current = 1
                   )
                 ORDER BY d.updated_at DESC LIMIT 1""",
            (str(row["environment_name"] or ""),),
        ).fetchone()
        if active_draft is not None:
            raise ValueError(
                "An active revision draft already exists for this environment. Open that draft instead."
            )
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("The completed snapshot cannot be reopened because its data is invalid.") from exc
        if not isinstance(payload, dict):
            raise ValueError("The completed snapshot cannot be reopened because its data is invalid.")

        for key in tuple(payload):
            if str(key).startswith("_"):
                payload.pop(key, None)
        draft_id = uuid4().hex[:12]
        payload.update(
            {
                "requester": setup_owner,
                "target_date": revised_target,
                "_draft_id": draft_id,
                "_version": 1,
                "_step": 1,
                "_created_at": reopened_at,
                "_saved_at": reopened_at,
                "_reopened_from_setup_id": str(row["id"]),
                "_reopened_from_logical_id": str(row["logical_id"]),
                "_reopened_from_revision": int(row["revision"]),
                "_reopen_reason": explanation,
                "_reopened_at": reopened_at,
            }
        )
        connection.execute(
            """INSERT INTO environment_drafts
                   (id, owner, environment_name, customer, current_step, version,
                    created_at, updated_at, payload_json)
               VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?)""",
            (
                draft_id,
                setup_owner,
                str(row["environment_name"] or ""),
                str(row["customer"] or ""),
                reopened_at,
                reopened_at,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
    return payload


def transfer_setup_ownership(
    setup_id: str, *, new_owner: str, changed_by: str, expected_owner: str | None = None
) -> dict[str, Any] | None:
    """Transfer revision authority and the source draft to another identity."""
    destination = new_owner.strip()
    actor = changed_by.strip() or "Unknown user"
    if not destination:
        raise ValueError("Select the new setup owner.")
    changed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT logical_id, is_current, status, setup_owner, source_draft_id
                 FROM environment_setups WHERE id = ?""",
            (setup_id,),
        ).fetchone()
        if row is None:
            return None
        if not int(row["is_current"]):
            raise ValueError("Superseded revisions are read-only.")
        if str(row["status"] or "") == "completed":
            raise ValueError("Completed setups are read-only. Reopen the setup before changing ownership.")
        current_owner = str(row["setup_owner"] or "").strip()
        if expected_owner is not None and current_owner.casefold() != expected_owner.strip().casefold():
            raise ValueError("Setup ownership changed while this request was open. Reload the setup.")
        if current_owner.casefold() == destination.casefold():
            raise ValueError("The selected user already owns this setup.")

        source_draft_id = str(row["source_draft_id"] or "").strip()
        draft = None
        if source_draft_id:
            draft_rows = connection.execute(
                """SELECT owner, version, payload_json FROM environment_drafts
                    WHERE id = ? ORDER BY updated_at DESC""",
                (source_draft_id,),
            ).fetchall()
            draft = next(
                (
                    candidate for candidate in draft_rows
                    if str(candidate["owner"] or "").casefold() == current_owner.casefold()
                ),
                draft_rows[0] if len(draft_rows) == 1 else None,
            )
            if draft_rows and draft is None:
                raise ValueError("The source draft owner is ambiguous; an administrator must repair it.")
            if draft is not None and connection.execute(
                "SELECT 1 FROM environment_drafts WHERE id = ? AND lower(owner) = lower(?)",
                (source_draft_id, destination),
            ).fetchone() is not None:
                raise ValueError("The new owner already has a draft with this identifier.")
            if draft is not None and connection.execute(
                """SELECT 1 FROM environment_servicenow_submissions
                    WHERE draft_id = ? AND lower(owner) = lower(?) LIMIT 1""",
                (source_draft_id, destination),
            ).fetchone() is not None:
                raise ValueError("The new owner already has ServiceNow state for this draft.")

        connection.execute(
            "UPDATE environment_setups SET setup_owner = ? WHERE id = ? AND is_current = 1",
            (destination, setup_id),
        )
        if draft is not None:
            draft_owner = str(draft["owner"] or "")
            try:
                payload = json.loads(str(draft["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            next_version = int(draft["version"] or 0) + 1
            payload["requester"] = destination
            payload["_version"] = next_version
            payload["_saved_at"] = changed_at
            connection.execute(
                """UPDATE environment_servicenow_submissions SET owner = ?
                    WHERE draft_id = ? AND owner = ?""",
                (destination, source_draft_id, draft_owner),
            )
            connection.execute(
                """UPDATE environment_drafts
                      SET owner = ?, version = ?, updated_at = ?, payload_json = ?
                    WHERE id = ? AND owner = ?""",
                (
                    destination, next_version, changed_at,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    source_draft_id, draft_owner,
                ),
            )
        connection.execute(
            """INSERT INTO environment_setup_ownership_events
                   (logical_id, from_owner, to_owner, changed_by, changed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (row["logical_id"], current_owner, destination, actor, changed_at),
        )
    return get_setup(setup_id)


def reassign_setup_devops_owner(
    setup_id: str, *, new_owner: str, changed_by: str, reason: str,
    expected_devops_owner: str | None = None, wizard_owner: str | None = None,
) -> dict[str, Any] | None:
    """Reassign operational delivery without changing authoritative setup ownership."""
    destination = new_owner.strip()
    actor = changed_by.strip() or "Unknown user"
    explanation = reason.strip()[:1000]
    if not destination:
        raise ValueError("Select the new Receiving DevOps owner.")
    if not explanation:
        raise ValueError("Explain why the Receiving DevOps owner is changing.")
    changed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT logical_id, is_current, status, devops_owner, setup_owner,
                      source_draft_id, wizard_owner, payload_json
                 FROM environment_setups WHERE id = ?""",
            (setup_id,),
        ).fetchone()
        if row is None:
            return None
        if not int(row["is_current"]):
            raise ValueError("Superseded revisions are read-only.")
        if str(row["status"] or "") == "completed":
            raise ValueError(
                "Completed setups are read-only. Reopen the setup before changing the Receiving DevOps owner."
            )
        current_owner = str(row["devops_owner"] or "").strip()
        if (
            expected_devops_owner is not None
            and current_owner.casefold() != expected_devops_owner.strip().casefold()
        ):
            raise ValueError(
                "The Receiving DevOps owner changed while this request was open. Reload the setup."
            )
        if current_owner.casefold() == destination.casefold():
            raise ValueError("The selected user is already the Receiving DevOps owner.")

        try:
            setup_payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            setup_payload = {}
        if not isinstance(setup_payload, dict):
            setup_payload = {}
        setup_payload["devops_receiver"] = destination

        source_draft_id = str(row["source_draft_id"] or "").strip()
        draft = None
        if source_draft_id:
            draft_rows = connection.execute(
                """SELECT owner, version, payload_json FROM environment_drafts
                    WHERE id = ? ORDER BY updated_at DESC""",
                (source_draft_id,),
            ).fetchall()
            setup_owner = str(row["setup_owner"] or "").strip()
            draft = next(
                (
                    candidate for candidate in draft_rows
                    if str(candidate["owner"] or "").casefold() == setup_owner.casefold()
                ),
                draft_rows[0] if len(draft_rows) == 1 else None,
            )
            if draft_rows and draft is None:
                raise ValueError(
                    "The source draft owner is ambiguous; an administrator must repair it."
                )

        next_wizard_owner = (
            str(row["wizard_owner"] or "") if wizard_owner is None else wizard_owner.strip()
        )
        updated = connection.execute(
            """UPDATE environment_setups
                  SET devops_owner = ?, payload_json = ?, wizard_owner = ?
                WHERE id = ? AND is_current = 1 AND lower(devops_owner) = lower(?)""",
            (
                destination,
                json.dumps(setup_payload, ensure_ascii=False, sort_keys=True),
                next_wizard_owner,
                setup_id,
                current_owner,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError(
                "The Receiving DevOps owner changed while this request was processed. Reload the setup."
            )

        if draft is not None:
            try:
                draft_payload = json.loads(str(draft["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                draft_payload = {}
            if not isinstance(draft_payload, dict):
                draft_payload = {}
            next_version = int(draft["version"] or 0) + 1
            draft_payload["devops_receiver"] = destination
            draft_payload["_version"] = next_version
            draft_payload["_saved_at"] = changed_at
            connection.execute(
                """UPDATE environment_drafts
                      SET version = ?, updated_at = ?, payload_json = ?
                    WHERE id = ? AND owner = ?""",
                (
                    next_version,
                    changed_at,
                    json.dumps(draft_payload, ensure_ascii=False, sort_keys=True),
                    source_draft_id,
                    str(draft["owner"] or ""),
                ),
            )

        connection.execute(
            """INSERT INTO environment_setup_devops_owner_events
                   (logical_id, from_owner, to_owner, changed_by, changed_at, reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                row["logical_id"], current_owner, destination, actor, changed_at,
                explanation,
            ),
        )
    return get_setup(setup_id)


def claim_servicenow_submission(draft_id: str, owner: str, request_type: str, *, snapshot: dict | None = None) -> bool:
    """Atomically reserve one external ServiceNow submission for a draft/type."""
    if request_type not in {"aws", "ncr"}:
        raise ValueError("Unsupported ServiceNow request type.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT status FROM environment_servicenow_submissions
                WHERE draft_id = ? AND owner = ? AND request_type = ?""",
            (draft_id, owner, request_type),
        ).fetchone()
        if existing is not None:
            return False
        connection.execute(
            """INSERT INTO environment_servicenow_submissions
                   (draft_id, owner, request_type, status, updated_at, snapshot_json)
               VALUES (?, ?, ?, 'pending', ?, ?)""",
            (draft_id, owner, request_type, now, json.dumps(snapshot or {})),
        )
    return True


def get_servicenow_submission(draft_id: str, owner: str, request_type: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM environment_servicenow_submissions WHERE draft_id = ? AND owner = ? AND request_type = ?",
            (draft_id, owner, request_type),
        ).fetchone()
    return dict(row) if row else None


def complete_servicenow_submission(
    draft_id: str, owner: str, request_type: str, *, request_number: str, request_url: str,
    request_status: str = "submitted",
) -> None:
    """Record the external result and merge only its fields into the latest mutable draft."""
    if request_type not in {"aws", "ncr"}:
        raise ValueError("Unsupported ServiceNow request type.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        # Manual attachment also gets an immutable reviewed snapshot. Existing
        # pending/completed snapshots are never replaced by later refreshes.
        draft_row = connection.execute(
            'SELECT payload_json FROM environment_drafts WHERE id = ? AND owner = ?',
            (draft_id, owner),
        ).fetchone()
        snapshot = (json.loads(draft_row['payload_json']).get('servicenow_templates', {}).get(request_type, {})
                    if draft_row else {})
        connection.execute(
            """INSERT OR IGNORE INTO environment_servicenow_submissions
                   (draft_id, owner, request_type, status, updated_at, snapshot_json)
               VALUES (?, ?, ?, 'completed', ?, ?)""",
            (draft_id, owner, request_type, now, json.dumps(snapshot)),
        )
        connection.execute(
            """UPDATE environment_servicenow_submissions
                  SET status = 'completed', request_number = ?, request_url = ?, updated_at = ?
                WHERE draft_id = ? AND owner = ? AND request_type = ?""",
            (request_number, request_url, now, draft_id, owner, request_type),
        )
        row = connection.execute(
            "SELECT payload_json, version FROM environment_drafts WHERE id = ? AND owner = ?",
            (draft_id, owner),
        ).fetchone()
        completed = connection.execute(
            "SELECT 1 FROM environment_setups WHERE source_draft_id = ? AND status = 'completed'",
            (draft_id,),
        ).fetchone()
        if row is None or completed:
            return
        data = json.loads(row["payload_json"])
        field = "aws_service_request" if request_type == "aws" else "ncr_ticket"
        # Preserve a reference attached by another operator while the order was running.
        if data.get(field) and data[field] != request_number:
            return
        data.update({field: request_number, f"{field}_url": request_url,
                     f"{field}_status": request_status, "_version": row["version"] + 1,
                     "_saved_at": now})
        connection.execute(
            "UPDATE environment_drafts SET payload_json = ?, version = ?, updated_at = ? WHERE id = ? AND owner = ?",
            (json.dumps(data), data["_version"], now, draft_id, owner),
        )


def release_servicenow_submission(draft_id: str, owner: str, request_type: str) -> None:
    """Release a failed reservation so a deliberate retry can be attempted."""
    with _connect() as connection:
        connection.execute(
            """DELETE FROM environment_servicenow_submissions
                WHERE draft_id = ? AND owner = ? AND request_type = ? AND status = 'pending'""",
            (draft_id, owner, request_type),
        )


def _migrate_legacy_revisions(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """SELECT id, environment_name, published_at FROM environment_setups
            WHERE logical_id = '' ORDER BY lower(trim(environment_name)), published_at, id"""
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        key = str(row["environment_name"] or "").strip().casefold() or str(row["id"])
        grouped.setdefault(key, []).append(row)
    for revisions in grouped.values():
        logical_id = uuid4().hex
        for number, row in enumerate(revisions, start=1):
            connection.execute(
                "UPDATE environment_setups SET logical_id = ?, revision = ?, is_current = ? WHERE id = ?",
                (logical_id, number, 1 if number == len(revisions) else 0, row["id"]),
            )


def _merge_duplicate_current_environments(connection: sqlite3.Connection) -> None:
    """Repair duplicate logical setups created before DB-level uniqueness existed."""
    duplicates = connection.execute(
        """SELECT lower(trim(environment_name)) AS environment_key
             FROM environment_setups
            WHERE is_current = 1
            GROUP BY lower(trim(environment_name))
           HAVING count(*) > 1"""
    ).fetchall()
    for duplicate in duplicates:
        rows = connection.execute(
            """SELECT id FROM environment_setups
                WHERE lower(trim(environment_name)) = ?
                ORDER BY published_at, id""",
            (duplicate["environment_key"],),
        ).fetchall()
        logical_id = uuid4().hex
        for revision, row in enumerate(rows, start=1):
            connection.execute(
                "UPDATE environment_setups SET logical_id = ?, revision = ?, is_current = ? WHERE id = ?",
                (logical_id, revision, 1 if revision == len(rows) else 0, row["id"]),
            )


def _backfill_search_columns(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """SELECT id, payload_json FROM environment_setups
            WHERE aws_account_id = '' AND jira_epic = '' AND ncr_ticket = ''
              AND architect = '' AND requester = ''"""
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        connection.execute(
            """UPDATE environment_setups
                  SET aws_account_id = ?, jira_epic = ?, ncr_ticket = ?, architect = ?, requester = ?
                WHERE id = ?""",
            tuple(str(payload.get(key) or "") for key in ("aws_account_id", "jira_epic", "ncr_ticket", "architect", "requester")) + (row["id"],),
        )


def _backfill_completed_at(connection: sqlite3.Connection) -> None:
    connection.execute(
        """UPDATE environment_setups
              SET completed_at = COALESCE((
                    SELECT changed_at FROM environment_setup_events
                     WHERE setup_id = environment_setups.id AND to_status = 'completed'
                     ORDER BY id DESC LIMIT 1
                  ), '')
            WHERE status = 'completed' AND completed_at = ''
              AND EXISTS (SELECT 1 FROM environment_setup_events
                           WHERE setup_id = environment_setups.id AND to_status = 'completed')"""
    )


def _backfill_completed_by(connection: sqlite3.Connection) -> None:
    connection.execute(
        """UPDATE environment_setups
              SET completed_by = COALESCE((
                    SELECT changed_by FROM environment_setup_events
                     WHERE setup_id = environment_setups.id AND to_status = 'completed'
                     ORDER BY id DESC LIMIT 1
                  ), '')
            WHERE status = 'completed' AND completed_by = ''
              AND EXISTS (SELECT 1 FROM environment_setup_events
                           WHERE setup_id = environment_setups.id AND to_status = 'completed')"""
    )


def _backfill_acknowledgement(connection: sqlite3.Connection) -> None:
    connection.execute(
        """UPDATE environment_setups
              SET acknowledged_by = CASE WHEN acknowledged_by = '' THEN started_by ELSE acknowledged_by END,
                  acknowledged_at = CASE WHEN acknowledged_at = '' THEN started_at ELSE acknowledged_at END
            WHERE status IN ('started', 'paused', 'completed') AND started_at <> ''"""
    )


def _with_schedule(record: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date()
    try:
        target = date.fromisoformat(str(record.get("target_date") or ""))
    except ValueError:
        record["schedule"] = {"key": "unknown", "label": "No valid target date", "days": None}
        return record
    status = str(record.get("status") or "")
    if status == "completed":
        try:
            completed = date.fromisoformat(str(record.get("completed_at") or "")[:10])
        except ValueError:
            completed = target
        late_days = (completed - target).days
        record["schedule"] = {
            "key": "completed_late" if late_days > 0 else "completed_on_time",
            "label": f"Completed {late_days} day{'s' if late_days != 1 else ''} late" if late_days > 0 else "Completed on time",
            "days": late_days,
        }
        return record
    days = (target - today).days
    if days < 0:
        overdue = abs(days)
        record["schedule"] = {
            "key": "paused_overdue" if status == "paused" else "overdue",
            "label": f"{'On hold · ' if status == 'paused' else ''}{overdue} day{'s' if overdue != 1 else ''} overdue",
            "days": overdue,
        }
    elif days == 0:
        record["schedule"] = {"key": "due_today", "label": "Due today", "days": 0}
    elif days <= 5:
        record["schedule"] = {"key": "due_soon", "label": f"Due in {days} day{'s' if days != 1 else ''}", "days": days}
    else:
        record["schedule"] = {"key": "scheduled", "label": f"Due in {days} days", "days": days}
    return record


def _with_setup_duration(record: dict[str, Any]) -> dict[str, Any]:
    """Add readable wall-clock time from the first start through completion."""
    record["setup_duration"] = ""
    started_at = str(record.get("started_at") or "")
    completed_at = str(record.get("completed_at") or "")
    if not started_at or not completed_at:
        return record
    try:
        started = datetime.strptime(started_at, "%Y-%m-%d %H:%M UTC")
        completed = datetime.strptime(completed_at, "%Y-%m-%d %H:%M UTC")
    except ValueError:
        return record
    total_minutes = max(0, int((completed - started).total_seconds() // 60))
    if total_minutes == 0:
        record["setup_duration"] = "Less than 1 minute"
        return record
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    record["setup_duration"] = " ".join(parts)
    return record


def publish_setup(
    payload: dict[str, Any], *, published_by: str, revision_of: str = "",
    expected_revision: int | None = None, setup_owner: str = "",
    expected_setup_owner: str | None = None,
) -> dict[str, Any]:
    setup_id = uuid4().hex
    logical_id = revision_of or uuid4().hex
    published_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    record = {
        "id": setup_id,
        "logical_id": logical_id,
        "revision": 1,
        "is_current": 1,
        "aws_account_id": str(payload.get("aws_account_id") or ""),
        "jira_epic": str(payload.get("jira_epic") or ""),
        "ncr_ticket": str(payload.get("ncr_ticket") or ""),
        "architect": str(payload.get("architect") or ""),
        "requester": str(payload.get("requester") or ""),
        "setup_owner": setup_owner.strip() or published_by.strip() or "Unknown user",
        "source_draft_id": str(payload.get("_draft_id") or ""),
        "draft_created_at": str(payload.get("_created_at") or ""),
        "environment_name": str(payload.get("environment_name") or ""),
        "customer": str(payload.get("customer") or ""),
        "setup_type": str(payload.get("setup_type") or ""),
        "devops_owner": str(payload.get("devops_receiver") or ""),
        "target_date": str(payload.get("target_date") or ""),
        "status": "ready",
        "published_by": published_by or "Unknown user",
        "published_at": published_at,
        "acknowledged_by": "",
        "acknowledged_at": "",
        "started_by": "",
        "started_at": "",
        "completed_by": "",
        "completed_at": "",
        "wizard_workspace_id": "",
        "wizard_slug": "",
        "wizard_owner": "",
        "bootstrap_json": "{}",
        "payload": payload,
    }
    try:
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = None
            if revision_of:
                previous = connection.execute(
                    """SELECT id, revision, setup_type, status, acknowledged_by, acknowledged_at,
                          started_by, started_at, completed_by, completed_at,
                          wizard_workspace_id, wizard_slug, wizard_owner, bootstrap_json,
                          setup_owner
                     FROM environment_setups
                    WHERE logical_id = ?
                    ORDER BY revision DESC
                    LIMIT 1""",
                    (revision_of,),
                ).fetchone()
                if previous is None:
                    raise ValueError("The setup selected for revision no longer exists.")
                if expected_revision is not None and int(previous["revision"]) != expected_revision:
                    raise ValueError("A newer revision was published while this request was open.")
                if (
                    expected_setup_owner is not None
                    and str(previous["setup_owner"] or "").casefold()
                    != expected_setup_owner.strip().casefold()
                ):
                    raise ValueError("Setup ownership changed while this revision was open.")
                record["revision"] = int(previous["revision"]) + 1
                record["setup_owner"] = str(previous["setup_owner"] or record["setup_owner"])
                previous_completed = str(previous["status"] or "") == "completed"
                if previous_completed and str(payload.get("_reopened_from_setup_id") or "") != str(previous["id"]):
                    raise ValueError(
                        "Completed setups can only be revised from the setup owner's reopen draft."
                    )
                # A same-type revision is a new immutable snapshot of the same
                # provisioning job, so its operational state must remain intact.
                # Completion is the boundary: a reopened revision starts a new
                # handoff and must never inherit completed lifecycle or wizard state.
                if not previous_completed and str(previous["setup_type"] or "") == record["setup_type"]:
                    for field in (
                        "status", "acknowledged_by", "acknowledged_at", "started_by",
                        "started_at", "completed_by", "completed_at",
                        "wizard_workspace_id", "wizard_slug", "wizard_owner",
                    ):
                        record[field] = str(previous[field] or "")
                    record["bootstrap_json"] = str(previous["bootstrap_json"] or "{}")
                connection.execute(
                    "UPDATE environment_setups SET is_current = 0 WHERE logical_id = ?",
                    (revision_of,),
                )
            connection.execute(
                """
                INSERT INTO environment_setups (
                    id, environment_name, customer, setup_type, devops_owner, target_date,
                    status, published_by, published_at, payload_json, logical_id, revision, is_current,
                    aws_account_id, jira_epic, ncr_ticket, architect, requester, source_draft_id,
                    draft_created_at, acknowledged_by, acknowledged_at, started_by, started_at,
                    completed_by, completed_at, wizard_workspace_id, wizard_slug, wizard_owner,
                    bootstrap_json, setup_owner
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"], record["environment_name"], record["customer"],
                    record["setup_type"], record["devops_owner"], record["target_date"],
                    record["status"], record["published_by"], record["published_at"],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    record["logical_id"], record["revision"], record["is_current"],
                    record["aws_account_id"], record["jira_epic"], record["ncr_ticket"],
                    record["architect"], record["requester"], record["source_draft_id"],
                    record["draft_created_at"], record["acknowledged_by"],
                    record["acknowledged_at"], record["started_by"], record["started_at"],
                    record["completed_by"], record["completed_at"],
                    record["wizard_workspace_id"], record["wizard_slug"],
                    record["wizard_owner"], record["bootstrap_json"], record["setup_owner"],
                ),
            )
            if previous is not None and record["status"] != "ready":
                connection.execute(
                    """INSERT INTO environment_setup_events
                       (setup_id, from_status, to_status, changed_by, changed_at, note,
                        previous_target_date, new_target_date)
                       SELECT ?, from_status, to_status, changed_by, changed_at, note,
                              previous_target_date, new_target_date
                         FROM environment_setup_events WHERE setup_id = ? ORDER BY id""",
                    (record["id"], previous["id"]),
                )
                connection.execute(
                    """INSERT INTO environment_setup_events
                       (setup_id, from_status, to_status, changed_by, changed_at, note)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        record["id"], record["status"], record["status"],
                        record["published_by"], published_at,
                        f"Revision {record['revision']} published; lifecycle retained.",
                    ),
                )
            else:
                publication_note = "Published for DevOps"
                if previous is not None and str(previous["status"] or "") == "completed":
                    reopen_reason = str(payload.get("_reopen_reason") or "").strip()
                    publication_note = (
                        f"Reopened from completed revision {int(previous['revision'])}: {reopen_reason}"
                    )[:1000]
                connection.execute(
                    """INSERT INTO environment_setup_events
                       (setup_id, from_status, to_status, changed_by, changed_at, note)
                       VALUES (?, '', 'ready', ?, ?, ?)""",
                    (record["id"], record["published_by"], published_at, publication_note),
                )
    except sqlite3.IntegrityError as exc:
        raise ValueError("A current setup already exists for this environment.") from exc
    return record


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    record["payload"] = json.loads(record.pop("payload_json"))
    try:
        record["bootstrap"] = json.loads(record.pop("bootstrap_json", "{}") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        record["bootstrap"] = {}
    return record


def _revision_values(payload: dict[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Flatten published values into readable paths for revision comparisons."""
    values: dict[tuple[str, ...], Any] = {}
    for key, value in payload.items():
        if str(key).startswith("_"):
            continue
        path = prefix + (str(key),)
        if isinstance(value, dict):
            values.update(_revision_values(value, path))
        else:
            values[path] = value
    return values


def _revision_changes(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, str]]:
    before = _revision_values(previous)
    after = _revision_values(current)
    changes = []
    for path in sorted(set(before) | set(after)):
        old_value = before.get(path)
        new_value = after.get(path)
        if old_value == new_value:
            continue
        changes.append({
            "field": " › ".join(part.replace("_", " ").title() for part in path),
            "before": _display_revision_value(old_value),
            "after": _display_revision_value(new_value),
        })
    return changes


def revision_changes(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, str]]:
    """Return the complete human-readable diff used before and after publication."""
    return _revision_changes(previous, current)


def _display_revision_value(value: Any) -> str:
    if value in (None, "", []):
        return "—"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "—"
    return str(value)


def list_setups(*, current_only: bool = True) -> list[dict[str, Any]]:
    with _connect() as connection:
        current_filter = "WHERE is_current = 1" if current_only else ""
        rows = connection.execute(
            f"""SELECT * FROM environment_setups {current_filter}
                ORDER BY is_current DESC, revision DESC, published_at DESC, id DESC"""
        ).fetchall()
    return [record for row in rows if (record := _decode(row)) is not None]


def _parse_lifecycle_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _duration_hours(start: Any, end: Any) -> float | None:
    started = _parse_lifecycle_time(start)
    ended = _parse_lifecycle_time(end)
    if started is None or ended is None:
        return None
    return max(0.0, (ended - started).total_seconds() / 3600)


def _format_duration_hours(value: float | None) -> str:
    if value is None:
        return "Not enough data"
    minutes = max(0, round(value * 60))
    days, remaining = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remaining, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _paused_hours(events: list[dict[str, Any]], completed_at: str) -> float:
    paused_at: datetime | None = None
    total = 0.0
    completed = _parse_lifecycle_time(completed_at)
    for event in events:
        changed = _parse_lifecycle_time(event.get("changed_at"))
        if changed is None:
            continue
        if event.get("to_status") == "paused":
            paused_at = changed
        elif paused_at is not None and event.get("from_status") == "paused":
            total += max(0.0, (changed - paused_at).total_seconds() / 3600)
            paused_at = None
    if paused_at is not None and completed is not None:
        total += max(0.0, (completed - paused_at).total_seconds() / 3600)
    return total


def _complexity_profile(record: dict[str, Any]) -> dict[str, Any]:
    """Return a transparent scope profile; this is not an engineer performance score."""
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    setup_type = str(record.get("setup_type") or payload.get("setup_type") or "")
    environment_type = str(payload.get("environment_type") or "").casefold()
    classification = str(payload.get("production_classification") or "").casefold()
    points = 1
    factors = ["Base foundation setup"]
    if setup_type == "catalyst-multi-vpc":
        points += 3
        factors.append("Multi-VPC topology (+3)")
    elif setup_type == "custom":
        points += 1
        factors.append("Custom setup definition (+1)")
    if classification == "prod" or environment_type in {"prod", "production"}:
        points += 2
        factors.append("Production environment (+2)")
    for flag, label in (
        ("grafana_required", "Grafana integration"),
        ("jumphost_required", "Jump host"),
    ):
        if str(payload.get(flag) or "").casefold() == "yes":
            points += 1
            factors.append(f"{label} (+1)")
    if points >= 4:
        key, label = "high", "High complexity"
    elif points >= 2:
        key, label = "standard", "Standard complexity"
    else:
        key, label = "low", "Low complexity"
    return {"key": key, "label": label, "points": points, "factors": factors}


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def setup_insights(
    *, period: str = "90", customer: str = "", setup_type: str = "",
    environment_type: str = "",
) -> dict[str, Any]:
    """Aggregate fair lifecycle statistics from current environment setup revisions."""
    valid_periods = {"30": 30, "90": 90, "365": 365, "all": None}
    if period not in valid_periods:
        period = "90"
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=valid_periods[period])
        if valid_periods[period] is not None
        else None
    )
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM environment_setups WHERE is_current = 1 ORDER BY published_at DESC"
        ).fetchall()
        revision_rows = connection.execute(
            "SELECT * FROM environment_setups ORDER BY logical_id, revision, published_at"
        ).fetchall()
        event_rows = connection.execute(
            "SELECT * FROM environment_setup_events ORDER BY changed_at, id"
        ).fetchall()
        draft_rows = connection.execute(
            "SELECT id, owner, customer, environment_name, current_step, updated_at, payload_json FROM environment_drafts ORDER BY updated_at"
        ).fetchall()
    events_by_setup: dict[str, list[dict[str, Any]]] = {}
    for event in event_rows:
        events_by_setup.setdefault(str(event["setup_id"]), []).append(dict(event))

    decoded_records = [record for row in rows if (record := _decode(row)) is not None]
    revision_history: dict[str, list[dict[str, Any]]] = {}
    for row in revision_rows:
        revision = _decode(row)
        if revision is not None:
            revision_history.setdefault(str(revision.get("logical_id") or ""), []).append(revision)
    draft_entries: list[dict[str, Any]] = []
    for row in draft_rows:
        try:
            draft_payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            draft_payload = {}
        if not isinstance(draft_payload, dict):
            draft_payload = {}
        draft_entries.append({**dict(row), "payload": draft_payload})
    setup_types = sorted(
        {str(record.get("setup_type") or "") for record in decoded_records if record.get("setup_type")},
        key=str.casefold,
    )
    environment_types = sorted(
        {
            str(record.get("payload", {}).get("environment_type") or "")
            for record in decoded_records
            if isinstance(record.get("payload"), dict) and record.get("payload", {}).get("environment_type")
        },
        key=str.casefold,
    )
    draft_customers = {str(row["customer"] or "") for row in draft_entries if row["customer"]}
    customers = sorted(
        {str(record.get("customer") or "") for record in decoded_records if record.get("customer")} | draft_customers,
        key=str.casefold,
    )
    selected_customer = customer.strip()
    selected_setup_type = setup_type.strip()
    selected_environment_type = environment_type.strip()

    def record_matches(record: dict[str, Any]) -> bool:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        return (
            (not selected_customer or str(record.get("customer") or "").casefold() == selected_customer.casefold())
            and (not selected_setup_type or str(record.get("setup_type") or "").casefold() == selected_setup_type.casefold())
            and (not selected_environment_type or str(payload.get("environment_type") or "").casefold() == selected_environment_type.casefold())
        )

    def draft_matches(draft: dict[str, Any]) -> bool:
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        return (
            (not selected_customer or str(draft.get("customer") or "").casefold() == selected_customer.casefold())
            and (not selected_setup_type or str(payload.get("setup_type") or "").casefold() == selected_setup_type.casefold())
            and (not selected_environment_type or str(payload.get("environment_type") or "").casefold() == selected_environment_type.casefold())
        )

    now = datetime.now(timezone.utc)
    attention: dict[str, list[dict[str, Any]]] = {
        "stale_drafts": [], "awaiting_acknowledgement": [], "awaiting_start": [],
        "inactive_setups": [], "on_hold": [], "overdue": [], "unassigned": [],
    }

    def age_days(value: Any) -> int | None:
        changed = _parse_lifecycle_time(value)
        return max(0, int((now - changed).total_seconds() // 86400)) if changed else None

    def setup_attention(record: dict[str, Any], category: str, reason: str, since: Any, tone: str) -> None:
        age = age_days(since)
        attention[category].append({
            "kind": "setup", "id": str(record.get("id") or ""),
            "environment": str(record.get("environment_name") or "Unnamed environment"),
            "customer": str(record.get("customer") or ""),
            "owner": str(record.get("devops_owner") or "Unassigned"),
            "reason": reason, "age_days": age, "tone": tone,
        })

    active_records = [
        record for record in decoded_records
        if record.get("status") != "completed"
        and record_matches(record)
    ]
    for record in active_records:
        status = str(record.get("status") or "")
        record_events = events_by_setup.get(str(record.get("id") or ""), [])
        last_activity = record_events[-1].get("changed_at") if record_events else record.get("published_at")
        if status == "ready" and (age_days(record.get("published_at")) or 0) >= 1:
            setup_attention(record, "awaiting_acknowledgement", "Awaiting acknowledgement", record.get("published_at"), "warning")
        if status == "acknowledged" and (age_days(record.get("acknowledged_at")) or 0) >= 1:
            setup_attention(record, "awaiting_start", "Acknowledged but not started", record.get("acknowledged_at"), "warning")
        if status == "started" and (age_days(last_activity) or 0) >= 3:
            setup_attention(record, "inactive_setups", "No recorded activity", last_activity, "warning")
        if status == "paused":
            setup_attention(record, "on_hold", "Setup is on hold", last_activity, "danger")
        try:
            target = date.fromisoformat(str(record.get("target_date") or ""))
        except ValueError:
            target = None
        if target is not None and target < now.date():
            setup_attention(record, "overdue", f"Target date passed on {target.isoformat()}", f"{target.isoformat()} 00:00 UTC", "danger")
        if not str(record.get("devops_owner") or "").strip():
            setup_attention(record, "unassigned", "Receiving DevOps owner is missing", record.get("published_at"), "danger")

    published_draft_ids = {
        str(record.get("source_draft_id") or "") for record in decoded_records if record.get("source_draft_id")
    }
    unpublished_drafts = [
        draft for draft in draft_entries
        if str(draft["id"]) not in published_draft_ids and draft_matches(draft)
    ]
    for row in unpublished_drafts:
        if str(row["id"]) in published_draft_ids:
            continue
        age = age_days(row["updated_at"])
        if age is None or age < 7:
            continue
        attention["stale_drafts"].append({
            "kind": "draft", "id": str(row["id"]),
            "environment": str(row["environment_name"] or "Unnamed environment"),
            "customer": str(row["customer"] or ""), "owner": str(row["owner"] or ""),
            "reason": "Draft has not been updated", "age_days": age, "tone": "warning",
        })
    for items in attention.values():
        items.sort(key=lambda item: (-(item.get("age_days") or 0), item["environment"].casefold()))
    attention_counts = {key: len(items) for key, items in attention.items()}

    try:
        elevated_threshold = max(1, int(os.getenv("PS_INSIGHTS_ELEVATED_CONCURRENCY", "3")))
        high_threshold = max(elevated_threshold + 1, int(os.getenv("PS_INSIGHTS_HIGH_CONCURRENCY", "5")))
    except ValueError:
        elevated_threshold, high_threshold = 3, 5
    workload_owners: dict[str, dict[str, Any]] = {}

    def workload_owner(name: Any) -> dict[str, Any]:
        owner_name = str(name or "").strip() or "Unassigned DevOps owner"
        return workload_owners.setdefault(owner_name, {
            "owner": owner_name, "ready": 0, "acknowledged": 0, "started": 0,
            "paused": 0, "incoming": 0, "completed_30_days": 0,
            "active_ages": [], "queue_ages": [], "setups": [],
        })

    for record in active_records:
        entry = workload_owner(record.get("devops_owner"))
        status = str(record.get("status") or "")
        if status in {"ready", "acknowledged", "started", "paused"}:
            entry[status] += 1
        age = age_days(record.get("published_at"))
        if age is not None:
            entry["active_ages"].append(age)
            if status in {"ready", "acknowledged"}:
                entry["queue_ages"].append(age)
        entry["setups"].append({
            "id": str(record.get("id") or ""),
            "environment": str(record.get("environment_name") or "Unnamed environment"),
            "status": status,
        })
    for draft in unpublished_drafts:
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        receiver = str(payload.get("devops_receiver") or "").strip()
        if receiver:
            workload_owner(receiver)["incoming"] += 1
    recent_cutoff = now - timedelta(days=30)
    for record in decoded_records:
        if not record_matches(record):
            continue
        completed_time = _parse_lifecycle_time(record.get("completed_at"))
        if completed_time is not None and completed_time >= recent_cutoff:
            workload_owner(record.get("devops_owner"))["completed_30_days"] += 1
    workload_rows = []
    all_queue_age_values: list[int] = []
    for entry in workload_owners.values():
        ages = entry.pop("active_ages")
        queue_ages = entry.pop("queue_ages")
        all_queue_age_values.extend(queue_ages)
        entry["active"] = entry["ready"] + entry["acknowledged"] + entry["started"] + entry["paused"]
        entry["queue"] = entry["ready"] + entry["acknowledged"]
        entry["median_active_age"] = f"{median(ages):g}d" if ages else "—"
        entry["median_queue_age"] = f"{median(queue_ages):g}d" if queue_ages else "—"
        entry["oldest_queue_age"] = max(queue_ages) if queue_ages else None
        if entry["active"] >= high_threshold:
            entry["signal"], entry["signal_label"] = "high", "High concurrency"
        elif entry["active"] >= elevated_threshold:
            entry["signal"], entry["signal_label"] = "elevated", "Elevated concurrency"
        else:
            entry["signal"], entry["signal_label"] = "normal", "Normal concurrency"
        workload_rows.append(entry)
    workload_rows.sort(key=lambda entry: (-entry["active"], -entry["queue"], entry["owner"].casefold()))
    scoped_records = [record for record in decoded_records if record_matches(record)]
    published_30_days = sum(
        1 for record in scoped_records
        if (published_time := _parse_lifecycle_time(record.get("published_at"))) is not None
        and published_time >= recent_cutoff
    )
    started_30_days = sum(
        1 for record in scoped_records
        if (started_time := _parse_lifecycle_time(record.get("started_at"))) is not None
        and started_time >= recent_cutoff
    )
    queue_delta = published_30_days - started_30_days
    records = []
    for record in decoded_records:
        if not record_matches(record):
            continue
        anchor = _parse_lifecycle_time(record.get("completed_at") or record.get("published_at"))
        if cutoff is not None and (anchor is None or anchor < cutoff):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        draft_created = record.get("draft_created_at") or payload.get("_created_at")
        preparation = _duration_hours(draft_created, record.get("published_at"))
        queue = _duration_hours(record.get("published_at"), record.get("started_at"))
        execution = _duration_hours(record.get("started_at"), record.get("completed_at"))
        end_to_end = _duration_hours(draft_created, record.get("completed_at"))
        paused = _paused_hours(events_by_setup.get(str(record["id"]), []), str(record.get("completed_at") or ""))
        active_execution = max(0.0, execution - paused) if execution is not None else None
        prerequisite_times = [
            parsed
            for key in ("networking_attested_at", "aws_platform_attested_at")
            if (parsed := _parse_lifecycle_time(payload.get(key))) is not None
        ]
        networking_ready = _parse_lifecycle_time(payload.get("networking_attested_at"))
        aws_platform_ready = _parse_lifecycle_time(payload.get("aws_platform_attested_at"))
        prerequisite_ready = max(prerequisite_times) if prerequisite_times else None
        networking_hours = _duration_hours(draft_created, payload.get("networking_attested_at"))
        aws_platform_hours = _duration_hours(draft_created, payload.get("aws_platform_attested_at"))
        finalization_hours = _duration_hours(
            prerequisite_ready.strftime("%Y-%m-%d %H:%M UTC") if prerequisite_ready else "",
            record.get("published_at"),
        )
        if networking_ready and aws_platform_ready:
            if networking_ready > aws_platform_ready:
                final_blocker = "Networking"
            elif aws_platform_ready > networking_ready:
                final_blocker = "AWS & Platform"
            else:
                final_blocker = "Confirmed together"
        elif networking_ready:
            final_blocker = "Networking only recorded"
        elif aws_platform_ready:
            final_blocker = "AWS & Platform only recorded"
        else:
            final_blocker = "Not enough data"
        record.update({
            "preparation_hours": preparation,
            "queue_hours": queue,
            "execution_hours": execution,
            "active_execution_hours": active_execution,
            "paused_hours": paused,
            "end_to_end_hours": end_to_end,
            "prerequisite_ready_at": prerequisite_ready.strftime("%Y-%m-%d %H:%M UTC") if prerequisite_ready else "",
            "networking_ready_hours": networking_hours,
            "aws_platform_ready_hours": aws_platform_hours,
            "finalization_hours": finalization_hours,
            "final_blocker": final_blocker,
        })
        history = revision_history.get(str(record.get("logical_id") or ""), [record])
        original_target = str(history[0].get("target_date") or record.get("target_date") or "")
        revision_target_changes = sum(
            1 for previous, current in zip(history, history[1:])
            if str(previous.get("target_date") or "") != str(current.get("target_date") or "")
        )
        operational_target_changes = sum(
            1
            for revision in history
            for event in events_by_setup.get(str(revision.get("id") or ""), [])
            if event.get("previous_target_date") and event.get("new_target_date")
            and event.get("previous_target_date") != event.get("new_target_date")
        )
        record.update({
            "revision_count": len(history),
            "original_target_date": original_target,
            "target_change_count": revision_target_changes + operational_target_changes,
            "target_date_changed": original_target != str(record.get("target_date") or "") or operational_target_changes > 0,
            "complexity": _complexity_profile(record),
        })
        records.append(record)

    completed = [record for record in records if record.get("completed_at")]

    def metric(key: str, source: list[dict[str, Any]] = completed) -> dict[str, Any]:
        values = [float(record[key]) for record in source if record.get(key) is not None]
        value = float(median(values)) if values else None
        return {"hours": value, "label": _format_duration_hours(value), "sample": len(values)}

    comparison: dict[str, Any] | None = None
    period_days = valid_periods[period]
    if period_days is not None:
        previous_start = now - timedelta(days=period_days * 2)
        previous_end = now - timedelta(days=period_days)
        previous_records: list[dict[str, Any]] = []
        for previous in decoded_records:
            if not record_matches(previous):
                continue
            anchor = _parse_lifecycle_time(previous.get("completed_at") or previous.get("published_at"))
            if anchor is None or not (previous_start <= anchor < previous_end):
                continue
            payload = previous.get("payload") if isinstance(previous.get("payload"), dict) else {}
            previous_records.append({
                **previous,
                "preparation_hours": _duration_hours(
                    previous.get("draft_created_at") or payload.get("_created_at"),
                    previous.get("published_at"),
                ),
                "execution_hours": _duration_hours(previous.get("started_at"), previous.get("completed_at")),
                "revision_count": len(revision_history.get(str(previous.get("logical_id") or ""), [previous])),
                "paused_hours": _paused_hours(
                    events_by_setup.get(str(previous.get("id") or ""), []),
                    str(previous.get("completed_at") or ""),
                ),
            })
        previous_completed = [record for record in previous_records if record.get("completed_at")]

        def percent_delta(current: float | int | None, previous: float | int | None) -> float | None:
            if current is None or previous in (None, 0):
                return None
            return round(((float(current) - float(previous)) / float(previous)) * 100, 1)

        current_execution = metric("execution_hours")["hours"]
        previous_execution = metric("execution_hours", previous_completed)["hours"]
        current_preparation = metric("preparation_hours", records)["hours"]
        previous_preparation = metric("preparation_hours", previous_records)["hours"]

        def quality_rates(source: list[dict[str, Any]]) -> dict[str, int]:
            source_completed = [record for record in source if record.get("completed_at")]
            on_time = 0
            for record in source_completed:
                completed_time = _parse_lifecycle_time(record.get("completed_at"))
                try:
                    target = date.fromisoformat(str(record.get("target_date") or ""))
                except ValueError:
                    target = None
                if completed_time is not None and target is not None and completed_time.date() <= target:
                    on_time += 1
            return {
                "on_time": round((on_time / len(source_completed)) * 100) if source_completed else 0,
                "revised": round((sum(1 for record in source if int(record.get("revision_count") or 1) > 1) / len(source)) * 100) if source else 0,
                "held": round((sum(1 for record in source if float(record.get("paused_hours") or 0) > 0 or record.get("status") == "paused") / len(source)) * 100) if source else 0,
            }

        current_quality = quality_rates(records)
        previous_quality = quality_rates(previous_records)
        comparison = {
            "label": f"Previous {period_days} days",
            "completed": {
                "current": len(completed), "previous": len(previous_completed),
                "delta": percent_delta(len(completed), len(previous_completed)),
            },
            "execution": {
                "current": _format_duration_hours(current_execution),
                "previous": _format_duration_hours(previous_execution),
                "delta": percent_delta(current_execution, previous_execution),
            },
            "preparation": {
                "current": _format_duration_hours(current_preparation),
                "previous": _format_duration_hours(previous_preparation),
                "delta": percent_delta(current_preparation, previous_preparation),
            },
            "on_time": {
                "current": f"{current_quality['on_time']}%", "previous": f"{previous_quality['on_time']}%",
                "delta": current_quality["on_time"] - previous_quality["on_time"],
            },
            "revised": {
                "current": f"{current_quality['revised']}%", "previous": f"{previous_quality['revised']}%",
                "delta": current_quality["revised"] - previous_quality["revised"],
            },
            "held": {
                "current": f"{current_quality['held']}%", "previous": f"{previous_quality['held']}%",
                "delta": current_quality["held"] - previous_quality["held"],
            },
        }

    trend_groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        anchor = _parse_lifecycle_time(record.get("completed_at") or record.get("published_at"))
        if anchor is None:
            continue
        if period in {"30", "90"}:
            week_start = (anchor - timedelta(days=anchor.weekday())).date()
            label = week_start.strftime("%d %b")
            sort_key = week_start.isoformat()
        else:
            label = anchor.strftime("%b %Y")
            sort_key = anchor.strftime("%Y-%m")
        trend_groups.setdefault(sort_key, []).append(record)
        trend_groups[sort_key][0].setdefault("_trend_label", label)
    trend = []
    for sort_key in sorted(trend_groups):
        bucket_records = trend_groups[sort_key]
        bucket_completed = [record for record in bucket_records if record.get("completed_at")]
        bucket_on_time = 0
        for record in bucket_completed:
            completed_time = _parse_lifecycle_time(record.get("completed_at"))
            try:
                target = date.fromisoformat(str(record.get("target_date") or ""))
            except ValueError:
                target = None
            if completed_time is not None and target is not None and completed_time.date() <= target:
                bucket_on_time += 1
        trend.append({
            "label": str(bucket_records[0].get("_trend_label") or sort_key),
            "completed": len(bucket_completed),
            "execution_hours": metric("execution_hours", bucket_completed)["hours"],
            "execution_label": metric("execution_hours", bucket_completed)["label"],
            "preparation_hours": metric("preparation_hours", bucket_records)["hours"],
            "preparation_label": metric("preparation_hours", bucket_records)["label"],
            "on_time_rate": round((bucket_on_time / len(bucket_completed)) * 100) if bucket_completed else 0,
            "revision_rate": round((sum(1 for record in bucket_records if int(record.get("revision_count") or 1) > 1) / len(bucket_records)) * 100) if bucket_records else 0,
            "hold_rate": round((sum(1 for record in bucket_records if float(record.get("paused_hours") or 0) > 0 or record.get("status") == "paused") / len(bucket_records)) * 100) if bucket_records else 0,
        })
    # Keep timing bars comparable across different filters and browser views.
    # Four equal visual bands cover increasingly long, operationally meaningful
    # ranges: 0-6 hours, 6-24 hours, 1-7 days, and 1-30 days. This preserves
    # detail for short work without collapsing multi-day setups into one bar.
    duration_milestones = (0.0, 6.0, 24.0, 168.0, 720.0)

    def duration_percent(hours: float) -> float:
        bounded = max(0.0, min(hours, duration_milestones[-1]))
        for index, (lower, upper) in enumerate(zip(duration_milestones, duration_milestones[1:])):
            if bounded <= upper:
                position_in_band = (bounded - lower) / (upper - lower)
                return round((index + position_in_band) * 25, 1)
        return 100.0

    for bucket in trend:
        execution_hours = float(bucket["execution_hours"] or 0)
        preparation_hours = float(bucket["preparation_hours"] or 0)
        bucket["execution_percent"] = duration_percent(execution_hours)
        bucket["preparation_percent"] = duration_percent(preparation_hours)
        bucket["execution_capped"] = execution_hours > duration_milestones[-1]
        bucket["preparation_capped"] = preparation_hours > duration_milestones[-1]

    owners: dict[str, dict[str, Any]] = {}
    for record in completed:
        owner = str(record.get("devops_owner") or "Unassigned DevOps owner")
        entry = owners.setdefault(owner, {"owner": owner, "records": [], "on_time": 0})
        entry["records"].append(record)
        completed_time = _parse_lifecycle_time(record.get("completed_at"))
        try:
            target = date.fromisoformat(str(record.get("target_date") or ""))
        except ValueError:
            target = None
        if completed_time is not None and target is not None and completed_time.date() <= target:
            entry["on_time"] += 1
    owner_rows = []
    for entry in owners.values():
        owner_records = entry.pop("records")
        count = len(owner_records)
        entry.update({
            "completed": count,
            "median_execution": metric("execution_hours", owner_records)["label"],
            "median_active": metric("active_execution_hours", owner_records)["label"],
            "on_time_percent": round((entry["on_time"] / count) * 100) if count else 0,
        })
        owner_rows.append(entry)
    owner_rows.sort(key=lambda entry: (-entry["completed"], str(entry["owner"]).casefold()))

    customer_groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        customer_name = str(record.get("customer") or "Unspecified customer")
        customer_groups.setdefault(customer_name, []).append(record)
    customer_profiles = []
    for customer_name, customer_records in customer_groups.items():
        customer_completed = [record for record in customer_records if record.get("completed_at")]
        on_time = 0
        for record in customer_completed:
            completed_time = _parse_lifecycle_time(record.get("completed_at"))
            try:
                target = date.fromisoformat(str(record.get("target_date") or ""))
            except ValueError:
                target = None
            if completed_time is not None and target is not None and completed_time.date() <= target:
                on_time += 1
        blockers: dict[str, int] = {}
        for record in customer_records:
            blocker = str(record.get("final_blocker") or "Not enough data")
            if blocker != "Not enough data":
                blockers[blocker] = blockers.get(blocker, 0) + 1
        common_blocker = (
            sorted(blockers.items(), key=lambda item: (-item[1], item[0].casefold()))[0][0]
            if blockers else "Not enough data"
        )
        completed_count = len(customer_completed)
        customer_profiles.append({
            "customer": customer_name,
            "setups": len(customer_records),
            "active": sum(1 for record in customer_records if record.get("status") != "completed"),
            "completed": completed_count,
            "median_preparation": metric("preparation_hours", customer_records)["label"],
            "median_execution": metric("execution_hours", customer_completed)["label"],
            "on_time_rate": round((on_time / completed_count) * 100) if completed_count else 0,
            "revision_rate": round((sum(1 for record in customer_records if int(record.get("revision_count") or 1) > 1) / len(customer_records)) * 100) if customer_records else 0,
            "hold_rate": round((sum(1 for record in customer_records if float(record.get("paused_hours") or 0) > 0 or record.get("status") == "paused") / len(customer_records)) * 100) if customer_records else 0,
            "common_blocker": common_blocker,
            "sample_label": "Established sample" if completed_count >= 5 else "Limited sample",
            "sample_key": "established" if completed_count >= 5 else "limited",
        })
    customer_profiles.sort(key=lambda entry: (-entry["setups"], entry["customer"].casefold()))

    longest = sorted(
        (record for record in completed if record.get("execution_hours") is not None),
        key=lambda record: float(record["execution_hours"]), reverse=True,
    )[:5]
    for record in longest:
        record["execution_label"] = _format_duration_hours(record["execution_hours"])
        record["active_execution_label"] = _format_duration_hours(record["active_execution_hours"])
        record["preparation_label"] = _format_duration_hours(record["preparation_hours"])
        record["queue_label"] = _format_duration_hours(record["queue_hours"])

    blocker_counts: dict[str, int] = {}
    for record in records:
        blocker = str(record.get("final_blocker") or "Not enough data")
        blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
    slowest_prerequisites = sorted(
        (
            record for record in records
            if record.get("preparation_hours") is not None
            and record.get("final_blocker") != "Not enough data"
        ),
        key=lambda record: float(record["preparation_hours"]), reverse=True,
    )[:5]
    for record in slowest_prerequisites:
        record["preparation_label"] = _format_duration_hours(record["preparation_hours"])
        record["networking_ready_label"] = _format_duration_hours(record["networking_ready_hours"])
        record["aws_platform_ready_label"] = _format_duration_hours(record["aws_platform_ready_hours"])
        record["finalization_label"] = _format_duration_hours(record["finalization_hours"])

    revised_count = sum(1 for record in records if int(record.get("revision_count") or 1) > 1)
    target_changed_count = sum(1 for record in records if record.get("target_date_changed"))
    current_on_time = 0
    original_on_time = 0
    schedule_variances: list[int] = []
    for record in completed:
        completed_time = _parse_lifecycle_time(record.get("completed_at"))
        if completed_time is None:
            continue
        try:
            current_target = date.fromisoformat(str(record.get("target_date") or ""))
        except ValueError:
            current_target = None
        try:
            original_target = date.fromisoformat(str(record.get("original_target_date") or ""))
        except ValueError:
            original_target = None
        if current_target is not None:
            current_on_time += int(completed_time.date() <= current_target)
            schedule_variances.append((completed_time.date() - current_target).days)
        if original_target is not None:
            original_on_time += int(completed_time.date() <= original_target)

    changed_fields: dict[str, int] = {}
    for record in records:
        history = revision_history.get(str(record.get("logical_id") or ""), [])
        for previous, current in zip(history, history[1:]):
            previous_payload = previous.get("payload") if isinstance(previous.get("payload"), dict) else {}
            current_payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
            for change in _revision_changes(previous_payload, current_payload):
                field = str(change.get("field") or "Unknown field")
                changed_fields[field] = changed_fields.get(field, 0) + 1
    frequent_changes = [
        {"field": field, "count": count}
        for field, count in sorted(changed_fields.items(), key=lambda item: (-item[1], item[0].casefold()))[:8]
    ]
    revision_outliers = sorted(
        (record for record in records if int(record.get("revision_count") or 1) > 1 or record.get("target_date_changed")),
        key=lambda record: (
            -int(record.get("revision_count") or 1),
            -int(record.get("target_change_count") or 0),
            str(record.get("environment_name") or "").casefold(),
        ),
    )[:5]

    profile_order = ("low", "standard", "high")
    complexity_profiles = []
    profile_execution_medians: dict[str, float | None] = {}
    for key in profile_order:
        profile_records = [record for record in records if record.get("complexity", {}).get("key") == key]
        profile_completed = [record for record in profile_records if record.get("completed_at")]
        execution_metric = metric("execution_hours", profile_completed)
        profile_execution_medians[key] = execution_metric["hours"]
        on_time_count = 0
        for record in profile_completed:
            completed_time = _parse_lifecycle_time(record.get("completed_at"))
            try:
                target = date.fromisoformat(str(record.get("target_date") or ""))
            except ValueError:
                target = None
            if completed_time is not None and target is not None and completed_time.date() <= target:
                on_time_count += 1
        complexity_profiles.append({
            "key": key,
            "label": {"low": "Low complexity", "standard": "Standard complexity", "high": "High complexity"}[key],
            "setups": len(profile_records),
            "completed": len(profile_completed),
            "median_execution": execution_metric["label"],
            "median_preparation": metric("preparation_hours", profile_records)["label"],
            "on_time_rate": round((on_time_count / len(profile_completed)) * 100) if profile_completed else 0,
        })
    complexity_outliers = []
    for record in completed:
        profile_key = str(record.get("complexity", {}).get("key") or "")
        expected = profile_execution_medians.get(profile_key)
        actual = record.get("execution_hours")
        if expected is None or actual is None:
            continue
        variance = float(actual) - float(expected)
        record["complexity_expected_label"] = _format_duration_hours(expected)
        record["complexity_variance_hours"] = variance
        record["complexity_variance_label"] = _format_duration_hours(abs(variance))
        record["complexity_variance_direction"] = "above" if variance > 0 else "below" if variance < 0 else "at"
        complexity_outliers.append(record)
    complexity_outliers.sort(key=lambda record: abs(float(record.get("complexity_variance_hours") or 0)), reverse=True)

    historical_completed = []
    for historical in decoded_records:
        if not record_matches(historical) or not historical.get("completed_at"):
            continue
        payload = historical.get("payload") if isinstance(historical.get("payload"), dict) else {}
        historical_completed.append({
            **historical,
            "complexity": _complexity_profile(historical),
            "preparation_hours": _duration_hours(
                historical.get("draft_created_at") or payload.get("_created_at"),
                historical.get("published_at"),
            ),
            "queue_hours": _duration_hours(historical.get("published_at"), historical.get("started_at")),
            "execution_hours": _duration_hours(historical.get("started_at"), historical.get("completed_at")),
        })

    def forecast_for(candidate: dict[str, Any], *, kind: str) -> dict[str, Any]:
        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        complexity = _complexity_profile(candidate)
        setup_value = str(candidate.get("setup_type") or payload.get("setup_type") or "")
        environment_value = str(payload.get("environment_type") or "")
        comparisons = [
            (
                "same setup and environment type",
                [record for record in historical_completed if str(record.get("setup_type") or "").casefold() == setup_value.casefold() and str(record.get("payload", {}).get("environment_type") or "").casefold() == environment_value.casefold()],
            ),
            (
                f"same {complexity['label'].lower()} profile",
                [record for record in historical_completed if record.get("complexity", {}).get("key") == complexity["key"]],
            ),
            (
                "same setup type",
                [record for record in historical_completed if str(record.get("setup_type") or "").casefold() == setup_value.casefold()],
            ),
        ]
        basis, comparable = next(((label, matches) for label, matches in comparisons if len(matches) >= 3), ("not enough comparable history", []))
        sample = len(comparable)

        def forecast_metric(key: str) -> dict[str, Any]:
            values = [float(record[key]) for record in comparable if record.get(key) is not None]
            middle = float(median(values)) if values else None
            low = _percentile(values, .25)
            high = _percentile(values, .75)
            return {
                "hours": middle, "label": _format_duration_hours(middle),
                "range": f"{_format_duration_hours(low)}–{_format_duration_hours(high)}" if low is not None and high is not None else "Not enough data",
                "sample": len(values),
            }

        preparation_forecast = forecast_metric("preparation_hours")
        queue_forecast = forecast_metric("queue_hours")
        execution_forecast = forecast_metric("execution_hours")
        confidence = "high" if sample >= 10 else "medium" if sample >= 5 else "low" if sample >= 3 else "insufficient"
        estimate = None
        status = str(candidate.get("status") or "draft")
        if comparable and status != "paused" and execution_forecast["hours"] is not None:
            if kind == "draft":
                base = _parse_lifecycle_time(payload.get("_created_at")) or now
                total_hours = sum(
                    value for value in (
                        preparation_forecast["hours"], queue_forecast["hours"], execution_forecast["hours"]
                    ) if value is not None
                )
                estimate = base + timedelta(hours=total_hours)
            elif candidate.get("started_at"):
                estimate = _parse_lifecycle_time(candidate.get("started_at"))
                estimate = estimate + timedelta(hours=execution_forecast["hours"]) if estimate else None
            else:
                base = _parse_lifecycle_time(candidate.get("published_at")) or now
                estimate = base + timedelta(hours=(queue_forecast["hours"] or 0) + execution_forecast["hours"])
        return {
            "kind": kind,
            "id": str(candidate.get("id") or candidate.get("_draft_id") or ""),
            "environment": str(candidate.get("environment_name") or payload.get("environment_name") or "Unnamed environment"),
            "customer": str(candidate.get("customer") or payload.get("customer") or ""),
            "owner": str(candidate.get("devops_owner") or payload.get("devops_receiver") or "Unassigned"),
            "status": status,
            "complexity": complexity,
            "basis": basis,
            "sample": sample,
            "confidence": confidence,
            "preparation": preparation_forecast,
            "queue": queue_forecast,
            "execution": execution_forecast,
            "estimated_completion": estimate.strftime("%Y-%m-%d") if estimate else "",
            "estimate_is_past": bool(estimate and estimate < now),
        }

    forecasts = [forecast_for(record, kind="setup") for record in active_records]
    for draft in unpublished_drafts:
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        forecasts.append(forecast_for({**payload, "payload": payload, "_draft_id": draft["id"]}, kind="draft"))
    forecasts.sort(key=lambda item: (item["confidence"] == "insufficient", item["estimated_completion"] or "9999", item["environment"].casefold()))

    data_quality_issues: list[dict[str, Any]] = []

    def quality_issue(
        record: dict[str, Any], *, kind: str, category: str, label: str,
        detail: str, severity: str,
    ) -> None:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        data_quality_issues.append({
            "kind": kind,
            "id": str(record.get("id") or record.get("_draft_id") or ""),
            "environment": str(record.get("environment_name") or payload.get("environment_name") or "Unnamed environment"),
            "customer": str(record.get("customer") or payload.get("customer") or ""),
            "category": category, "label": label, "detail": detail, "severity": severity,
        })

    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if not (record.get("draft_created_at") or payload.get("_created_at")):
            quality_issue(record, kind="setup", category="draft_timing", label="Draft creation time missing", detail="Preparation and end-to-end duration cannot be calculated.", severity="warning")
        if not str(record.get("devops_owner") or "").strip():
            quality_issue(record, kind="setup", category="ownership", label="Receiving DevOps owner missing", detail="Delivery ownership and workload attribution are unavailable.", severity="error")
        if record.get("status") == "completed" and not str(record.get("completed_by") or "").strip():
            quality_issue(record, kind="setup", category="completion", label="Completion user missing", detail="The completion audit identity was not recorded.", severity="warning")
        if record.get("status") in {"started", "paused", "completed"} and not record.get("started_at"):
            quality_issue(record, kind="setup", category="lifecycle", label="Setup start time missing", detail="Praxis execution duration cannot be calculated.", severity="error")
        if record.get("status") == "completed" and not record.get("completed_at"):
            quality_issue(record, kind="setup", category="lifecycle", label="Completion time missing", detail="The completed status has no matching timestamp.", severity="error")
        if record.get("status") == "completed" and not str(record.get("wizard_workspace_id") or "").strip():
            quality_issue(record, kind="setup", category="wizard", label="Wizard workspace missing", detail="The completed setup is not linked to a provisioning workspace.", severity="warning")
        timestamps = [
            ("draft", record.get("draft_created_at") or payload.get("_created_at")),
            ("published", record.get("published_at")),
            ("acknowledged", record.get("acknowledged_at")),
            ("started", record.get("started_at")),
            ("completed", record.get("completed_at")),
        ]
        parsed_timestamps = [(label, parsed) for label, value in timestamps if (parsed := _parse_lifecycle_time(value)) is not None]
        if any(current[1] < previous[1] for previous, current in zip(parsed_timestamps, parsed_timestamps[1:])):
            quality_issue(record, kind="setup", category="timestamps", label="Lifecycle timestamps out of order", detail="A later lifecycle stage is timestamped before an earlier stage.", severity="error")
        record_events = events_by_setup.get(str(record.get("id") or ""), [])
        if record.get("status") == "paused":
            pause_events = [event for event in record_events if event.get("to_status") == "paused"]
            if pause_events and not str(pause_events[-1].get("note") or "").strip():
                quality_issue(record, kind="setup", category="hold_reason", label="Hold reason missing", detail="The current pause does not explain the blocker.", severity="warning")
        requirements = payload.get("requirements") if isinstance(payload.get("requirements"), dict) else {}
        missing_evidence = sum(
            1 for values in requirements.values()
            if isinstance(values, dict) and values.get("status") == "confirmed" and not str(values.get("evidence") or "").strip()
        )
        if missing_evidence:
            quality_issue(record, kind="setup", category="evidence", label="Confirmed prerequisites lack evidence", detail=f"{missing_evidence} confirmed prerequisite(s) have no ticket, link, or verification reference.", severity="warning")

    for draft in unpublished_drafts:
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        draft_record = {**payload, "payload": payload, "_draft_id": draft["id"]}
        missing_identity = [
            label for key, label in (
                ("environment_name", "environment name"), ("customer", "customer"),
                ("setup_type", "setup type"), ("devops_receiver", "Receiving DevOps owner"),
            ) if not str(payload.get(key) or "").strip()
        ]
        if missing_identity:
            quality_issue(draft_record, kind="draft", category="draft_identity", label="Draft identity incomplete", detail="Missing " + ", ".join(missing_identity) + ".", severity="warning")

    severity_order = {"error": 0, "warning": 1, "info": 2}
    data_quality_issues.sort(key=lambda issue: (severity_order.get(issue["severity"], 9), issue["environment"].casefold(), issue["label"]))
    quality_categories: dict[str, dict[str, Any]] = {}
    for issue in data_quality_issues:
        category = quality_categories.setdefault(issue["category"], {"key": issue["category"], "label": issue["label"], "count": 0})
        category["count"] += 1
    affected_records = {(issue["kind"], issue["id"]) for issue in data_quality_issues}
    data_quality = {
        "issues": data_quality_issues,
        "issue_count": len(data_quality_issues),
        "affected_records": len(affected_records),
        "errors": sum(1 for issue in data_quality_issues if issue["severity"] == "error"),
        "warnings": sum(1 for issue in data_quality_issues if issue["severity"] == "warning"),
        "categories": sorted(quality_categories.values(), key=lambda item: (-item["count"], item["label"].casefold())),
        "records_checked": len(records) + len(unpublished_drafts),
    }

    status_counts = {status: 0 for status in ("ready", "acknowledged", "started", "paused", "completed")}
    for record in records:
        status = str(record.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "period": period,
        "customer": selected_customer,
        "customers": customers,
        "setup_type": selected_setup_type,
        "setup_types": setup_types,
        "environment_type": selected_environment_type,
        "environment_types": environment_types,
        "attention": attention,
        "attention_counts": attention_counts,
        "attention_total": sum(attention_counts.values()),
        "workload": {
            "owners": workload_rows,
            "active": len(active_records),
            "queued": sum(1 for record in active_records if record.get("status") in {"ready", "acknowledged"}),
            "provisioning": sum(1 for record in active_records if record.get("status") == "started"),
            "paused": sum(1 for record in active_records if record.get("status") == "paused"),
            "incoming": len(unpublished_drafts),
            "assigned_incoming": sum(
                1 for draft in unpublished_drafts
                if str((draft.get("payload") or {}).get("devops_receiver") or "").strip()
            ),
            "near_ready_incoming": sum(1 for draft in unpublished_drafts if int(draft.get("current_step") or 1) >= 5),
            "published_30_days": published_30_days,
            "started_30_days": started_30_days,
            "queue_delta": queue_delta,
            "queue_direction": "growing" if queue_delta > 0 else "shrinking" if queue_delta < 0 else "stable",
            "oldest_queue_age": max(all_queue_age_values) if all_queue_age_values else None,
            "median_queue_age": f"{median(all_queue_age_values):g}d" if all_queue_age_values else "—",
            "elevated_threshold": elevated_threshold,
            "high_threshold": high_threshold,
        },
        "records": records,
        "published": len(records),
        "completed": len(completed),
        "active": sum(status_counts.get(status, 0) for status in ("ready", "acknowledged", "started", "paused")),
        "on_hold": status_counts.get("paused", 0),
        "completion_rate": round((len(completed) / len(records)) * 100) if records else 0,
        "metrics": {
            "preparation": metric("preparation_hours", records),
            "queue": metric("queue_hours"),
            "execution": metric("execution_hours"),
            "active_execution": metric("active_execution_hours"),
            "end_to_end": metric("end_to_end_hours"),
        },
        "prerequisites": {
            "networking": metric("networking_ready_hours", records),
            "aws_platform": metric("aws_platform_ready_hours", records),
            "finalization": metric("finalization_hours", records),
            "blocker_counts": blocker_counts,
            "sample": sum(1 for record in records if record.get("final_blocker") != "Not enough data"),
            "slowest": slowest_prerequisites,
        },
        "quality": {
            "revised_count": revised_count,
            "revision_rate": round((revised_count / len(records)) * 100) if records else 0,
            "average_revisions": round(sum(int(record.get("revision_count") or 1) for record in records) / len(records), 1) if records else 0,
            "target_changed_count": target_changed_count,
            "target_change_rate": round((target_changed_count / len(records)) * 100) if records else 0,
            "current_on_time_rate": round((current_on_time / len(completed)) * 100) if completed else 0,
            "original_on_time_rate": round((original_on_time / len(completed)) * 100) if completed else 0,
            "median_schedule_variance_days": float(median(schedule_variances)) if schedule_variances else None,
            "frequent_changes": frequent_changes,
            "outliers": revision_outliers,
        },
        "comparison": comparison,
        "trend": trend,
        "complexity_profiles": complexity_profiles,
        "complexity_outliers": complexity_outliers[:5],
        "forecasts": forecasts,
        "data_quality": data_quality,
        "owners": owner_rows,
        "customer_profiles": customer_profiles,
        "longest": longest,
    }


def setup_dashboard(
    *, status_filter: str = "active", customer: str = "", query: str = "",
    mine_filter: str = "all", current_user: str = "", urgency_filter: str = "",
    sort_by: str = "", sort_direction: str = "asc",
    page: int = 1, page_size: int = 25
) -> dict[str, Any]:
    status_sets = {
        "active": ("ready", "acknowledged", "started", "paused"),
        "ready": ("ready",),
        "acknowledged": ("acknowledged",),
        "started": ("started",),
        "paused": ("paused",),
        "completed": ("completed",),
        "all": (),
    }
    if status_filter not in status_sets:
        status_filter = "active"
    if mine_filter not in {"all", "owned", "assigned", "requested", "started"}:
        mine_filter = "all"
    if urgency_filter not in {"", "overdue", "completed_month"}:
        urgency_filter = ""
    sort_columns = {
        "environment": "environment_name COLLATE NOCASE",
        "setup": "setup_type COLLATE NOCASE",
        "jira": "jira_epic COLLATE NOCASE",
        "aws_account": "aws_account_id",
        "owner": "devops_owner COLLATE NOCASE",
        "setup_owner": "setup_owner COLLATE NOCASE",
        "target_date": "target_date",
        "status": "status",
        "published": "published_at",
    }
    if sort_by not in sort_columns:
        sort_by = ""
    sort_direction = "desc" if sort_direction == "desc" else "asc"
    page = max(1, page)
    page_size = max(1, min(100_000, page_size))
    base_where: list[str] = ["is_current = 1"]
    base_parameters: list[Any] = []
    if customer:
        base_where.append("customer = ? COLLATE NOCASE")
        base_parameters.append(customer)
    search_terms = [term.casefold() for term in query.split() if term][:8]
    search_haystack = "lower(environment_name || ' ' || customer || ' ' || setup_owner || ' ' || devops_owner || ' ' || aws_account_id || ' ' || jira_epic || ' ' || ncr_ticket || ' ' || architect || ' ' || requester)"
    for term in search_terms:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        base_where.append(f"{search_haystack} LIKE ? ESCAPE '\\'")
        base_parameters.append(f"%{escaped}%")
    identity = current_user.strip().casefold()
    identity_terms: list[str] = []
    for candidate in (identity, identity.split("@", 1)[0], identity.split()[0] if identity else ""):
        candidate = candidate.strip()
        if len(candidate) >= 3 and candidate not in identity_terms:
            identity_terms.append(candidate)

    def personal_clause(kind: str) -> tuple[str, list[str]]:
        if kind == "all":
            return "", []
        if kind == "owned":
            return ("lower(setup_owner) = ?", [identity]) if identity else ("0", [])
        column = {"assigned": "devops_owner", "requested": "requester", "started": "started_by"}[kind]
        if not identity_terms:
            return "0", []
        return "(" + " OR ".join(f"instr(lower({column}), ?) > 0" for _ in identity_terms) + ")", list(identity_terms)

    personal_sql, personal_parameters = personal_clause(mine_filter)
    scoped_where = list(base_where)
    scoped_parameters = list(base_parameters)
    if personal_sql:
        scoped_where.append(personal_sql)
        scoped_parameters.extend(personal_parameters)
    where = list(scoped_where)
    parameters = list(scoped_parameters)
    if urgency_filter == "overdue":
        where.append("status != 'completed' AND target_date < date('now')")
    elif urgency_filter == "completed_month":
        where.append("status = 'completed' AND substr(completed_at, 1, 7) = strftime('%Y-%m', 'now')")
    statuses = status_sets[status_filter]
    if statuses:
        where.append(f"status IN ({','.join('?' for _ in statuses)})")
        parameters.extend(statuses)
    where_sql = f" WHERE {' AND '.join(where)}"
    scoped_where_sql = f" WHERE {' AND '.join(scoped_where)}"

    with _connect() as connection:
        customers = [
            str(row["customer"])
            for row in connection.execute(
                "SELECT DISTINCT customer FROM environment_setups WHERE is_current = 1 AND customer <> '' ORDER BY customer COLLATE NOCASE"
            ).fetchall()
        ]
        raw_counts = connection.execute(
            f"SELECT status, COUNT(*) AS count FROM environment_setups{scoped_where_sql} GROUP BY status",
            scoped_parameters,
        ).fetchall()
        summary_row = connection.execute(
            f"""SELECT
                    SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END) AS awaiting,
                    SUM(CASE WHEN status = 'acknowledged' THEN 1 ELSE 0 END) AS acknowledged,
                    SUM(CASE WHEN status = 'started' THEN 1 ELSE 0 END) AS provisioning,
                    SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END) AS on_hold,
                    SUM(CASE WHEN status != 'completed' AND target_date < date('now') THEN 1 ELSE 0 END) AS overdue,
                    SUM(CASE WHEN status = 'completed' AND substr(completed_at, 1, 7) = strftime('%Y-%m', 'now') THEN 1 ELSE 0 END) AS completed_month
                  FROM environment_setups{scoped_where_sql}""",
            scoped_parameters,
        ).fetchone()
        personal_counts: dict[str, int] = {}
        for kind in ("all", "owned", "assigned", "requested", "started"):
            clause, clause_parameters = personal_clause(kind)
            count_where = list(base_where)
            count_parameters = list(base_parameters)
            if clause:
                count_where.append(clause)
                count_parameters.extend(clause_parameters)
            if statuses:
                count_where.append(f"status IN ({','.join('?' for _ in statuses)})")
                count_parameters.extend(statuses)
            personal_counts[kind] = int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM environment_setups WHERE {' AND '.join(count_where)}",
                    count_parameters,
                ).fetchone()["count"]
            )
        total = int(
            connection.execute(
                f"SELECT COUNT(*) AS count FROM environment_setups{where_sql}", parameters
            ).fetchone()["count"]
        )
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page, page_count)
        default_order = """CASE
                    WHEN status = 'paused' AND target_date < date('now') THEN 0
                    WHEN status != 'completed' AND target_date < date('now') THEN 1
                    WHEN status = 'paused' THEN 2
                    WHEN status != 'completed' AND target_date = date('now') THEN 3
                    WHEN status != 'completed' AND target_date <= date('now', '+5 days') THEN 4
                    ELSE 5 END,
                    CASE WHEN status = 'completed' THEN published_at END DESC,
                    target_date ASC, published_at DESC, id DESC"""
        order_by = (
            f"{sort_columns[sort_by]} {sort_direction.upper()}, id {sort_direction.upper()}"
            if sort_by
            else default_order
        )
        rows = connection.execute(
            f"""SELECT * FROM environment_setups{where_sql}
                 ORDER BY {order_by} LIMIT ? OFFSET ?""",
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
    counts = {"ready": 0, "acknowledged": 0, "started": 0, "paused": 0, "completed": 0}
    counts.update({str(row["status"]): int(row["count"]) for row in raw_counts})
    counts["active"] = counts["ready"] + counts["acknowledged"] + counts["started"] + counts["paused"]
    counts["all"] = sum(counts[key] for key in ("ready", "acknowledged", "started", "paused", "completed"))
    return {
        "setups": [_with_schedule(record) for row in rows if (record := _decode(row)) is not None],
        "customers": customers,
        "counts": counts,
        "status_filter": status_filter,
        "customer": customer,
        "query": query,
        "mine_filter": mine_filter,
        "current_user": current_user,
        "personal_counts": personal_counts,
        "urgency_filter": urgency_filter,
        "sort_by": sort_by,
        "sort_direction": sort_direction,
        "summary": {
            key: int(summary_row[key] or 0)
            for key in ("awaiting", "acknowledged", "provisioning", "on_hold", "overdue", "completed_month")
        },
        "page": page,
        "page_size": page_size,
        "total": total,
        "page_count": page_count,
    }


def home_snapshot(current_user: str) -> dict[str, Any]:
    """Return the compact, read-only Readiness Gate view used by the site home."""
    dashboard = setup_dashboard(status_filter="active", current_user=current_user, page_size=6)
    assigned = setup_dashboard(
        status_filter="active", mine_filter="assigned", current_user=current_user, page_size=5,
    )
    insights = setup_insights(period="30")
    drafts = list_drafts(current_user)[:4]
    attention_items = []
    for category, items in insights["attention"].items():
        for item in items:
            attention_items.append({**item, "category": category})
    attention_items.sort(
        key=lambda item: (
            0 if item.get("tone") == "red" else 1 if item.get("tone") == "amber" else 2,
            -int(item.get("age_days") or 0),
            str(item.get("environment") or "").casefold(),
        )
    )
    return {
        "counts": dashboard["counts"],
        "summary": dashboard["summary"],
        "attention_total": insights["attention_total"],
        "attention_items": attention_items[:5],
        "assigned_setups": assigned["setups"],
        "assigned_count": assigned["total"],
        "drafts": drafts,
    }


def export_setup_rows(
    *, status_filter: str = "active", customer: str = "", query: str = "",
    mine_filter: str = "all", current_user: str = "", urgency_filter: str = "",
    sort_by: str = "", sort_direction: str = "asc"
) -> list[dict[str, Any]]:
    dashboard = setup_dashboard(
        status_filter=status_filter,
        customer=customer,
        query=query,
        mine_filter=mine_filter,
        current_user=current_user,
        urgency_filter=urgency_filter,
        sort_by=sort_by,
        sort_direction=sort_direction,
        page=1,
        page_size=100_000,
    )
    rows = dashboard["setups"]
    if not rows:
        return []
    with _connect() as connection:
        original_setups = {
            str(row["logical_id"]): {
                "target_date": str(row["target_date"]),
                "published_at": str(row["published_at"]),
            }
            for row in connection.execute(
                """SELECT logical_id, target_date, published_at FROM environment_setups current_revision
                    WHERE revision = (SELECT MIN(revision) FROM environment_setups history
                                      WHERE history.logical_id = current_revision.logical_id)"""
            ).fetchall()
        }
        latest_hold_reasons: dict[str, str] = {}
        for event in connection.execute(
            """SELECT setup_id, note FROM environment_setup_events
                WHERE to_status = 'paused' ORDER BY id"""
        ).fetchall():
            latest_hold_reasons[str(event["setup_id"])] = str(event["note"] or "")
    today = datetime.now(timezone.utc).date()
    for row in rows:
        original = original_setups.get(str(row["logical_id"]), {})
        row["original_target_date"] = original.get("target_date", row["target_date"])
        row["hold_reason"] = latest_hold_reasons.get(str(row["id"]), "")
        try:
            published_date = date.fromisoformat(str(original.get("published_at", row["published_at"]))[:10])
        except ValueError:
            published_date = today
        try:
            end_date = date.fromisoformat(str(row["completed_at"])[:10]) if row["completed_at"] else today
        except ValueError:
            end_date = today
        row["days_active"] = max(0, (end_date - published_date).days)
    return rows


def get_setup(setup_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM environment_setups WHERE id = ?", (setup_id,)
        ).fetchone()
        events = connection.execute(
            "SELECT * FROM environment_setup_events WHERE setup_id = ? ORDER BY id DESC",
            (setup_id,),
        ).fetchall()
        revisions = []
        ownership_events = []
        devops_owner_events = []
        if row is not None:
            revisions = connection.execute(
                """SELECT id, revision, is_current, status, target_date, published_by, published_at,
                          payload_json
                     FROM environment_setups WHERE logical_id = ? ORDER BY revision DESC""",
                (row["logical_id"],),
            ).fetchall()
            ownership_events = connection.execute(
                """SELECT from_owner, to_owner, changed_by, changed_at
                     FROM environment_setup_ownership_events
                    WHERE logical_id = ? ORDER BY id DESC""",
                (row["logical_id"],),
            ).fetchall()
            devops_owner_events = connection.execute(
                """SELECT from_owner, to_owner, changed_by, changed_at, reason
                     FROM environment_setup_devops_owner_events
                    WHERE logical_id = ? ORDER BY id DESC""",
                (row["logical_id"],),
            ).fetchall()
        clarifications = connection.execute(
            "SELECT * FROM environment_setup_clarifications WHERE logical_id = ? ORDER BY id DESC",
            (row["logical_id"],),
        ).fetchall() if row else []
    record = _decode(row)
    if record is not None:
        record["clarifications"] = [dict(item) for item in clarifications]
        record["clarification_pending"] = next((dict(item) for item in clarifications if not item["resolved_at"]), None)
        record["events"] = [dict(event) for event in events]
        record["completion_event"] = next(
            (
                event
                for event in record["events"]
                if event.get("to_status") == "completed"
                and event.get("from_status") != "completed"
            ),
            {},
        )
        record["ownership_events"] = [dict(event) for event in ownership_events]
        record["devops_owner_events"] = [dict(event) for event in devops_owner_events]
        decoded_revisions = [dict(revision) for revision in revisions]
        previous_payload: dict[str, Any] | None = None
        for revision in reversed(decoded_revisions):
            try:
                payload = json.loads(str(revision.pop("payload_json", "{}")))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            revision["changes"] = (
                _revision_changes(previous_payload, payload) if previous_payload is not None else []
            )
            previous_payload = payload
        record["revisions"] = decoded_revisions
        record["original_target_date"] = record["revisions"][-1]["target_date"] if record["revisions"] else record["target_date"]
        _with_schedule(record)
        _with_setup_duration(record)
    return record


def complete_setup_bootstrap(
    setup_id: str, *, spacelift_space_id: str, admin_stack_reference: str,
    aws_integration_id: str, completed_by: str
) -> dict[str, Any] | None:
    """Record the DevOps-owned preparation required before a wizard can start."""
    values = {
        "spacelift_space_id": spacelift_space_id.strip(),
        "admin_stack_reference": admin_stack_reference.strip(),
        "aws_integration_id": aws_integration_id.strip(),
    }
    if any(not value for value in values.values()):
        raise ValueError("All DevOps bootstrap details are required.")
    values["completed_by"] = completed_by or "Unknown user"
    values["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status, is_current, wizard_workspace_id, bootstrap_json FROM environment_setups WHERE id = ?",
            (setup_id,),
        ).fetchone()
        if row is None:
            return None
        if not int(row["is_current"]):
            raise ValueError("Superseded revisions are read-only.")
        if str(row["status"]) != "started":
            raise ValueError("The setup must be started before completing DevOps bootstrap.")
        if str(row["wizard_workspace_id"] or ""):
            raise ValueError("Bootstrap details cannot change after the wizard has started.")
        try:
            existing_bootstrap = json.loads(str(row["bootstrap_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            existing_bootstrap = {}
        if isinstance(existing_bootstrap, dict) and existing_bootstrap.get("completed_at"):
            raise ValueError("DevOps bootstrap has already been confirmed.")
        connection.execute(
            "UPDATE environment_setups SET bootstrap_json = ? WHERE id = ?",
            (json.dumps(values, ensure_ascii=False, sort_keys=True), setup_id),
        )
    return get_setup(setup_id)


def link_wizard_workspace(
    setup_id: str, *, workspace_id: str, wizard_slug: str, wizard_owner: str
) -> dict[str, Any] | None:
    """Attach the one provisioning workspace created from a readiness snapshot."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT is_current, status, wizard_workspace_id FROM environment_setups WHERE id = ?",
            (setup_id,),
        ).fetchone()
        if row is None:
            return None
        if not int(row["is_current"]):
            raise ValueError("Superseded revisions cannot start a wizard.")
        if str(row["status"]) not in {"started", "paused"}:
            raise ValueError("Mark the setup started before creating its wizard workspace.")
        existing = str(row["wizard_workspace_id"] or "")
        if existing and existing != workspace_id:
            raise ValueError("This readiness record already has a wizard workspace.")
        connection.execute(
            """UPDATE environment_setups
                  SET wizard_workspace_id = ?, wizard_slug = ?, wizard_owner = ?
                WHERE id = ?""",
            (workspace_id, wizard_slug, wizard_owner, setup_id),
        )
    return get_setup(setup_id)


def unlink_wizard_workspace(workspace_id: str, *, wizard_owner: str = "") -> str:
    """Remove a deleted workspace link from every revision that inherited it.

    Returns the current readiness setup ID so callers can send the user back to
    the environment and let them start a replacement workspace.
    """
    workspace_id = workspace_id.strip()
    wizard_owner = wizard_owner.strip()
    if not workspace_id:
        return ""
    with _connect() as connection:
        params: list[Any] = [workspace_id]
        owner_filter = ""
        if wizard_owner:
            owner_filter = " AND wizard_owner = ?"
            params.append(wizard_owner)
        row = connection.execute(
            f"""SELECT id FROM environment_setups
                 WHERE wizard_workspace_id = ?{owner_filter} AND status != 'completed'
                 ORDER BY is_current DESC, revision DESC
                 LIMIT 1""",
            params,
        ).fetchone()
        if row is None:
            return ""
        connection.execute(
            f"""UPDATE environment_setups
                   SET wizard_workspace_id = '', wizard_slug = '', wizard_owner = ''
                 WHERE wizard_workspace_id = ?{owner_filter} AND status != 'completed'""",
            params,
        )
    return str(row["id"] or "")


def find_current_setup_by_environment(environment_name: str) -> dict[str, Any] | None:
    normalized = environment_name.strip()
    if not normalized:
        return None
    with _connect() as connection:
        row = connection.execute(
            """SELECT * FROM environment_setups
                WHERE is_current = 1 AND environment_name = ? COLLATE NOCASE
                ORDER BY published_at DESC, id DESC LIMIT 1""",
            (normalized,),
        ).fetchone()
    return _decode(row)


def transition_setup(
    setup_id: str, *, to_status: str, changed_by: str, note: str = "",
    revised_target_date: str = ""
) -> dict[str, Any] | None:
    allowed = {
        "ready": {"acknowledged"},
        "acknowledged": {"started"},
        "started": {"paused", "completed"},
        "paused": {"started", "completed"},
        "completed": set(),
    }
    changed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    actor = changed_by or "Unknown user"
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT status, is_current, target_date, setup_type, wizard_workspace_id, logical_id
                 FROM environment_setups WHERE id = ?""", (setup_id,)
        ).fetchone()
        if row is None:
            return None
        if not int(row["is_current"]):
            raise ValueError("Superseded revisions are read-only.")
        if to_status in {"acknowledged", "started"} and connection.execute(
            "SELECT 1 FROM environment_setup_clarifications WHERE logical_id = ? AND resolved_at = ''",
            (row["logical_id"],),
        ).fetchone():
            raise ValueError("The setup owner must respond to the clarification request before acknowledgement.")
        from_status = str(row["status"])
        previous_target_date = str(row["target_date"] or "")
        if to_status not in allowed.get(from_status, set()):
            raise ValueError(f"Cannot change setup from {from_status} to {to_status}.")
        if to_status == "paused" and not note.strip():
            raise ValueError("A reason is required when putting a setup on hold.")
        if from_status == "paused" and to_status == "started" and not note.strip():
            raise ValueError("A resolution is required when resuming a setup.")
        if to_status == "completed" and not note.strip():
            raise ValueError("A completion summary is required.")
        if (
            to_status == "completed"
            and str(row["setup_type"] or "") in WIZARD_SETUP_TYPES
            and not str(row["wizard_workspace_id"] or "").strip()
        ):
            raise ValueError("Start the provisioning wizard before completing the setup.")
        new_target_date = revised_target_date.strip()
        try:
            previous_target = date.fromisoformat(previous_target_date)
        except ValueError:
            previous_target = None
        if from_status == "paused" and to_status == "started" and previous_target and previous_target < datetime.now(timezone.utc).date() and not new_target_date:
            raise ValueError("An overdue setup needs a revised target date before it can resume.")
        if new_target_date:
            try:
                revised_target = date.fromisoformat(new_target_date)
            except ValueError as exc:
                raise ValueError("The revised target date is invalid.") from exc
            if revised_target < datetime.now(timezone.utc).date():
                raise ValueError("The revised target date cannot be in the past.")
        else:
            new_target_date = previous_target_date
        updated = connection.execute(
            """
            UPDATE environment_setups
               SET status = ?,
                   acknowledged_by = CASE WHEN ? = 'acknowledged' THEN ? ELSE acknowledged_by END,
                   acknowledged_at = CASE WHEN ? = 'acknowledged' THEN ? ELSE acknowledged_at END,
                   started_by = CASE WHEN ? = 'started' AND started_at = '' THEN ? ELSE started_by END,
                   started_at = CASE WHEN ? = 'started' AND started_at = '' THEN ? ELSE started_at END,
                   completed_by = CASE WHEN ? = 'completed' THEN ? ELSE completed_by END,
                   completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END,
                   target_date = ?
             WHERE id = ? AND status = ? AND is_current = 1
            """,
            (to_status, to_status, actor, to_status, changed_at, to_status, actor, to_status, changed_at,
             to_status, actor, to_status, changed_at, new_target_date, setup_id, from_status),
        )
        if updated.rowcount != 1:
            raise ValueError("The setup changed while this action was being processed. Reload and try again.")
        connection.execute(
            """INSERT INTO environment_setup_events
               (setup_id, from_status, to_status, changed_by, changed_at, note,
                previous_target_date, new_target_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (setup_id, from_status, to_status, actor, changed_at, note.strip()[:1000],
             previous_target_date, new_target_date),
        )
    return get_setup(setup_id)


def clarify_setup(setup_id: str, *, actor: str, note: str, resolve: bool = False) -> None:
    """Return a pre-start handoff for clarification without changing its snapshot."""
    if not note.strip():
        raise ValueError("A clarification reason or response is required.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM environment_setups WHERE id = ?", (setup_id,)).fetchone()
        if row is None or not row["is_current"] or row["status"] not in {"ready", "acknowledged"}:
            raise ValueError("Clarification is available only on the current handoff before provisioning starts.")
        pending = connection.execute(
            "SELECT id FROM environment_setup_clarifications WHERE logical_id = ? AND resolved_at = ''",
            (row["logical_id"],),
        ).fetchone()
        if resolve:
            if not pending:
                raise ValueError("There is no pending clarification request.")
            connection.execute(
                "UPDATE environment_setup_clarifications SET resolved_by = ?, resolved_at = ?, response = ? WHERE id = ?",
                (actor, now, note.strip()[:1000], pending["id"]),
            )
        else:
            if pending:
                raise ValueError("A clarification request is already pending.")
            connection.execute(
                "INSERT INTO environment_setup_clarifications (logical_id, setup_id, requested_by, requested_at, reason) VALUES (?, ?, ?, ?, ?)",
                (row["logical_id"], setup_id, actor, now, note.strip()[:1000]),
            )
            connection.execute(
                "UPDATE environment_setups SET status = 'ready', acknowledged_by = '', acknowledged_at = '' WHERE id = ?",
                (setup_id,),
            )
        connection.execute(
            """INSERT INTO environment_setup_events (setup_id, from_status, to_status, changed_by, changed_at, note)
               VALUES (?, ?, 'ready', ?, ?, ?)""",
            (setup_id, row["status"], actor, now, ('Clarification response: ' if resolve else 'Returned for clarification: ') + note.strip()[:1000]),
        )


def setups_for_workspaces(workspace_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Find the newest associated revision for only the caller's visible workspaces."""
    found: dict[str, dict[str, Any]] = {}
    if not workspace_ids:
        return found
    with _connect() as connection:
        for offset in range(0, len(workspace_ids), 400):
            batch = workspace_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""SELECT id, environment_name, revision, wizard_workspace_id
                    FROM environment_setups WHERE wizard_workspace_id IN ({placeholders})
                    ORDER BY is_current DESC, revision DESC, published_at DESC""", batch,
            ).fetchall()
            for row in rows:
                found.setdefault(str(row["wizard_workspace_id"]), dict(row))
    return found
