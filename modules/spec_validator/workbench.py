from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from typing import Any

import yaml

from engine.wizards.factory.core_validate_jobs import get_job

DEFAULT_DB_PATH = "/tmp/praxis-cache/spec-workbench.sqlite3"
DEFAULT_DB_TIMEOUT_SECONDS = 30.0
DEFAULT_DB_BUSY_TIMEOUT_MS = 30_000


def _now() -> float:
    return time.time()


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _db_path() -> str:
    raw = (os.getenv("PS_SPEC_WORKBENCH_DB_PATH") or DEFAULT_DB_PATH).strip()
    return os.path.abspath(os.path.normpath(raw))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=DEFAULT_DB_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_DB_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                title TEXT NOT NULL,
                working_yaml TEXT NOT NULL,
                working_hash TEXT NOT NULL,
                confluence_parent_page_id TEXT,
                confluence_parent_page_url TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_drafts_owner_updated
                ON drafts(owner, updated_at DESC);

            CREATE TABLE IF NOT EXISTS validations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                spec_yaml TEXT NOT NULL,
                harbor_registry_project TEXT NOT NULL,
                pc_version TEXT NOT NULL,
                job_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                ok INTEGER,
                error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (draft_id) REFERENCES drafts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_validations_draft_created
                ON validations(draft_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_validations_owner_job
                ON validations(owner, job_id);

            CREATE TABLE IF NOT EXISTS publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT NOT NULL,
                validation_id INTEGER NOT NULL,
                revision_number INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                spec_yaml TEXT NOT NULL,
                published_by TEXT NOT NULL,
                confluence_parent_page_id TEXT NOT NULL,
                confluence_parent_page_url TEXT NOT NULL,
                confluence_revision_page_id TEXT NOT NULL,
                confluence_revision_page_url TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (draft_id) REFERENCES drafts(id) ON DELETE CASCADE,
                FOREIGN KEY (validation_id) REFERENCES validations(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_publications_draft_revision
                ON publications(draft_id, revision_number);

            CREATE INDEX IF NOT EXISTS idx_publications_draft_created
                ON publications(draft_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS publication_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT NOT NULL,
                reserved_by TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (draft_id) REFERENCES drafts(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_reservations_draft_revision
                ON publication_reservations(draft_id, revision_number);
            """
        )
        existing_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(drafts)").fetchall()
        }
        migrations = {
            "mode": "TEXT NOT NULL DEFAULT 'playground'",
            "architecture_metadata": "TEXT NOT NULL DEFAULT '{}'",
            "source_template_key": "TEXT NOT NULL DEFAULT ''",
            "source_draft_id": "TEXT NOT NULL DEFAULT ''",
            "baseline_yaml": "TEXT NOT NULL DEFAULT ''",
        }
        for column, declaration in migrations.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE drafts ADD COLUMN {column} {declaration}")


def _normalize_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if "ok" in data and data["ok"] is not None:
        data["ok"] = bool(data["ok"])
    if "architecture_metadata" in data:
        try:
            metadata = json.loads(str(data.get("architecture_metadata") or "{}"))
        except (TypeError, ValueError):
            metadata = {}
        data["architecture_metadata"] = metadata if isinstance(metadata, dict) else {}
    return data


def _normalize_rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _normalize_row(row)
        if item is not None:
            out.append(item)
    return out


def analyze_spec_yaml(spec_yaml: str) -> dict[str, Any]:
    text = spec_yaml or ""
    summary: dict[str, Any] = {
        "line_count": len(text.splitlines()) or (1 if text else 0),
        "byte_count": len(text.encode("utf-8")),
        "parsed_ok": False,
        "error": "",
        "error_line": None,
        "error_column": None,
        "error_context_start_line": None,
        "error_context": [],
        "top_type": "empty",
        "top_level_keys": [],
        "title_candidate": "",
        "name": "",
        "spec_type": "",
    }
    if not text.strip():
        summary["error"] = "Spec draft is empty."
        return summary

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        summary["top_type"] = "invalid"
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            error_line = mark.line + 1
            error_column = mark.column + 1
            summary["error_line"] = error_line
            summary["error_column"] = error_column
            summary["error"] = f"YAML parse error at line {error_line}, column {error_column}."

            lines = text.splitlines()
            start_line = max(1, error_line - 2)
            end_line = min(len(lines), error_line + 2)
            summary["error_context_start_line"] = start_line
            summary["error_context"] = [
                {
                    "number": line_number,
                    "text": lines[line_number - 1],
                    "is_error": line_number == error_line,
                }
                for line_number in range(start_line, end_line + 1)
            ]
        else:
            summary["error"] = "YAML parse error."
        return summary

    summary["parsed_ok"] = True
    if isinstance(parsed, dict):
        keys = [str(key) for key in parsed.keys()]
        summary["top_type"] = "mapping"
        summary["top_level_keys"] = keys[:12]
        summary["name"] = str(parsed.get("name") or "").strip()
        summary["spec_type"] = str(parsed.get("spec_type") or parsed.get("type") or "").strip()
        title_candidate = summary["name"]
        metadata = parsed.get("metadata")
        if not title_candidate and isinstance(metadata, dict):
            title_candidate = str(metadata.get("name") or metadata.get("title") or "").strip()
        if not title_candidate:
            title_candidate = str(parsed.get("title") or parsed.get("spec_name") or "").strip()
        summary["title_candidate"] = title_candidate
        return summary

    if isinstance(parsed, list):
        summary["top_type"] = "list"
        return summary

    summary["top_type"] = type(parsed).__name__
    return summary


def derive_title(*, title: str, spec_yaml: str, uploaded_filename: str = "") -> str:
    cleaned = (title or "").strip()
    if cleaned:
        return cleaned[:180]

    analysis = analyze_spec_yaml(spec_yaml)
    candidate = str(analysis.get("title_candidate") or "").strip()
    if candidate:
        return candidate[:180]

    filename = os.path.basename((uploaded_filename or "").strip())
    if filename:
        stem, _ext = os.path.splitext(filename)
        if stem:
            return stem[:180]

    return "Untitled Spec"


def create_draft(
    *,
    owner: str,
    title: str,
    spec_yaml: str,
    mode: str = "playground",
    architecture_metadata: dict[str, Any] | None = None,
    source_template_key: str = "",
    source_draft_id: str = "",
    baseline_yaml: str = "",
) -> dict[str, Any]:
    init_db()
    now = _now()
    draft_id = uuid.uuid4().hex
    normalized_yaml = spec_yaml if spec_yaml.endswith("\n") else f"{spec_yaml}\n"
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO drafts (
                id, owner, title, working_yaml, working_hash, mode,
                architecture_metadata, source_template_key, source_draft_id,
                baseline_yaml, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                owner,
                title,
                normalized_yaml,
                _sha256_text(normalized_yaml),
                "prerequisite" if mode == "prerequisite" else "playground",
                json.dumps(architecture_metadata or {}, sort_keys=True),
                str(source_template_key or "").strip().lower(),
                str(source_draft_id or "").strip(),
                baseline_yaml or normalized_yaml,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    draft = _normalize_row(row)
    assert draft is not None
    return draft


def list_drafts(owner: str) -> list[dict[str, Any]]:
    init_db()
    sync_owner_jobs(owner)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                d.*,
                (
                    SELECT v.state
                    FROM validations v
                    WHERE v.draft_id = d.id
                    ORDER BY v.created_at DESC
                    LIMIT 1
                ) AS last_validation_state,
                (
                    SELECT v.ok
                    FROM validations v
                    WHERE v.draft_id = d.id
                    ORDER BY v.created_at DESC
                    LIMIT 1
                ) AS last_validation_ok,
                (
                    SELECT v.created_at
                    FROM validations v
                    WHERE v.draft_id = d.id
                    ORDER BY v.created_at DESC
                    LIMIT 1
                ) AS last_validation_at,
                (
                    SELECT p.revision_number
                    FROM publications p
                    WHERE p.draft_id = d.id
                    ORDER BY p.revision_number DESC
                    LIMIT 1
                ) AS last_revision_number,
                (
                    SELECT p.created_at
                    FROM publications p
                    WHERE p.draft_id = d.id
                    ORDER BY p.revision_number DESC
                    LIMIT 1
                ) AS last_published_at
            FROM drafts d
            WHERE d.owner = ?
            ORDER BY d.updated_at DESC
            """,
            (owner,),
        ).fetchall()
    return _normalize_rows(rows)


def get_draft(owner: str, draft_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND owner = ?",
            (draft_id, owner),
        ).fetchone()
    return _normalize_row(row)


def update_draft(
    *,
    owner: str,
    draft_id: str,
    title: str,
    spec_yaml: str,
    mode: str | None = None,
    architecture_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    init_db()
    now = _now()
    normalized_yaml = spec_yaml if spec_yaml.endswith("\n") else f"{spec_yaml}\n"
    with _connect() as conn:
        conn.execute(
            """
            UPDATE drafts
            SET title = ?, working_yaml = ?, working_hash = ?, updated_at = ?
            WHERE id = ? AND owner = ?
            """,
            (
                title,
                normalized_yaml,
                _sha256_text(normalized_yaml),
                now,
                draft_id,
                owner,
            ),
        )
        if mode is not None or architecture_metadata is not None:
            current = conn.execute(
                "SELECT mode, architecture_metadata FROM drafts WHERE id = ? AND owner = ?",
                (draft_id, owner),
            ).fetchone()
            if current is not None:
                next_mode = (
                    "prerequisite" if mode == "prerequisite" else "playground"
                ) if mode is not None else str(current["mode"] or "playground")
                next_metadata = architecture_metadata
                if next_metadata is None:
                    try:
                        next_metadata = json.loads(str(current["architecture_metadata"] or "{}"))
                    except (TypeError, ValueError):
                        next_metadata = {}
                conn.execute(
                    """
                    UPDATE drafts
                    SET mode = ?, architecture_metadata = ?, updated_at = ?
                    WHERE id = ? AND owner = ?
                    """,
                    (next_mode, json.dumps(next_metadata or {}, sort_keys=True), now, draft_id, owner),
                )
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND owner = ?",
            (draft_id, owner),
        ).fetchone()
    return _normalize_row(row)


def update_draft_confluence_parent(
    *,
    owner: str,
    draft_id: str,
    confluence_parent_page_id: str,
    confluence_parent_page_url: str,
) -> dict[str, Any] | None:
    init_db()
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE drafts
            SET confluence_parent_page_id = ?, confluence_parent_page_url = ?, updated_at = ?
            WHERE id = ? AND owner = ?
            """,
            (
                confluence_parent_page_id,
                confluence_parent_page_url,
                now,
                draft_id,
                owner,
            ),
        )
        row = conn.execute(
            "SELECT * FROM drafts WHERE id = ? AND owner = ?",
            (draft_id, owner),
        ).fetchone()
    return _normalize_row(row)


def delete_draft(*, owner: str, draft_id: str) -> bool:
    init_db()
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM drafts WHERE id = ? AND owner = ?",
            (draft_id, owner),
        )
    return cursor.rowcount > 0


def list_validations(draft_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
    init_db()
    sync_draft_jobs(draft_id)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM validations
            WHERE draft_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (draft_id, max(1, limit)),
        ).fetchall()
    return _normalize_rows(rows)


def list_publications(draft_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM publications
            WHERE draft_id = ?
            ORDER BY revision_number DESC
            LIMIT ?
            """,
            (draft_id, max(1, limit)),
        ).fetchall()
    return _normalize_rows(rows)


def create_validation(
    *,
    owner: str,
    draft_id: str,
    spec_yaml: str,
    harbor_registry_project: str,
    pc_version: str,
    job_id: str,
    state: str,
) -> dict[str, Any]:
    init_db()
    now = _now()
    normalized_yaml = spec_yaml if spec_yaml.endswith("\n") else f"{spec_yaml}\n"
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO validations (
                draft_id,
                owner,
                content_hash,
                spec_yaml,
                harbor_registry_project,
                pc_version,
                job_id,
                state,
                ok,
                error,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?, ?)
            """,
            (
                draft_id,
                owner,
                _sha256_text(normalized_yaml),
                normalized_yaml,
                harbor_registry_project,
                pc_version,
                job_id,
                state,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM validations WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    validation = _normalize_row(row)
    assert validation is not None
    return validation


def get_validation_by_job(job_id: str) -> dict[str, Any] | None:
    init_db()
    sync_validation_job(job_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM validations WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return _normalize_row(row)


def latest_successful_validation(draft_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM validations
            WHERE draft_id = ? AND state = 'done' AND ok = 1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (draft_id,),
        ).fetchone()
    return _normalize_row(row)


def latest_matching_successful_validation(
    draft_id: str,
    *,
    content_hash: str,
) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM validations
            WHERE draft_id = ? AND content_hash = ? AND state = 'done' AND ok = 1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (draft_id, content_hash),
        ).fetchone()
    return _normalize_row(row)


def reserve_next_revision_number(*, draft_id: str, owner: str) -> int:
    init_db()
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT COALESCE(MAX(revision_number), 0) AS max_revision
            FROM (
                SELECT revision_number
                FROM publications
                WHERE draft_id = ?
                UNION ALL
                SELECT revision_number
                FROM publication_reservations
                WHERE draft_id = ?
            )
            """,
            (draft_id, draft_id),
        ).fetchone()
        max_revision = int(row["max_revision"] or 0) if row is not None else 0
        revision_number = max_revision + 1
        conn.execute(
            """
            INSERT INTO publication_reservations (
                draft_id,
                reserved_by,
                revision_number,
                created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (draft_id, owner, revision_number, now),
        )
    return revision_number


def release_reserved_revision(*, draft_id: str, revision_number: int) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM publication_reservations
            WHERE draft_id = ? AND revision_number = ?
            """,
            (draft_id, revision_number),
        )


def record_publication(
    *,
    owner: str,
    draft_id: str,
    validation_id: int,
    revision_number: int,
    content_hash: str,
    spec_yaml: str,
    confluence_parent_page_id: str,
    confluence_parent_page_url: str,
    confluence_revision_page_id: str,
    confluence_revision_page_url: str,
    reservation_required: bool = False,
) -> dict[str, Any]:
    init_db()
    now = _now()
    normalized_yaml = spec_yaml if spec_yaml.endswith("\n") else f"{spec_yaml}\n"
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        released = conn.execute(
            """
            DELETE FROM publication_reservations
            WHERE draft_id = ? AND revision_number = ?
            """,
            (draft_id, revision_number),
        )
        if reservation_required and released.rowcount != 1:
            raise RuntimeError("Publication revision reservation was not found.")
        conn.execute(
            """
            INSERT INTO publications (
                draft_id,
                validation_id,
                revision_number,
                content_hash,
                spec_yaml,
                published_by,
                confluence_parent_page_id,
                confluence_parent_page_url,
                confluence_revision_page_id,
                confluence_revision_page_url,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                validation_id,
                revision_number,
                content_hash,
                normalized_yaml,
                owner,
                confluence_parent_page_id,
                confluence_parent_page_url,
                confluence_revision_page_id,
                confluence_revision_page_url,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE drafts
            SET confluence_parent_page_id = ?, confluence_parent_page_url = ?, updated_at = ?
            WHERE id = ? AND owner = ?
            """,
            (
                confluence_parent_page_id,
                confluence_parent_page_url,
                now,
                draft_id,
                owner,
            ),
        )
        row = conn.execute(
            """
            SELECT *
            FROM publications
            WHERE draft_id = ? AND revision_number = ?
            """,
            (draft_id, revision_number),
        ).fetchone()
    publication = _normalize_row(row)
    assert publication is not None
    return publication


def sync_owner_jobs(owner: str) -> None:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT v.job_id
            FROM validations v
            INNER JOIN drafts d ON d.id = v.draft_id
            WHERE d.owner = ? AND v.state NOT IN ('done', 'error')
            """,
            (owner,),
        ).fetchall()
    for row in rows:
        sync_validation_job(str(row["job_id"]))


def sync_draft_jobs(draft_id: str) -> None:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT job_id
            FROM validations
            WHERE draft_id = ? AND state NOT IN ('done', 'error')
            """,
            (draft_id,),
        ).fetchall()
    for row in rows:
        sync_validation_job(str(row["job_id"]))


def sync_validation_job(job_id: str) -> dict[str, Any] | None:
    init_db()
    job = get_job(job_id)
    if not job:
        return None

    state = str(job.get("state") or "").strip() or "unknown"
    ok = job.get("ok")
    ok_value = None if ok is None else (1 if bool(ok) else 0)
    error = str(job.get("error") or "").strip()
    now = _now()

    with _connect() as conn:
        conn.execute(
            """
            UPDATE validations
            SET state = ?, ok = ?, error = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (state, ok_value, error, now, job_id),
        )
        row = conn.execute(
            "SELECT * FROM validations WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return _normalize_row(row)
