import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path


@contextmanager
def connection():
    # Retain the original file and environment override so existing drafts remain available.
    path = Path(os.getenv("PRAXIS_TERRAFORM_MODULE_BUILDER_DB") or
                os.getenv("PRAXIS_MODULE_BUILDER_DB", "/tmp/praxis-cache/module-builder.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS module_drafts (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
            document TEXT NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1)""")
        yield conn


def create(owner, name, document):
    key = uuid.uuid4().hex
    with connection() as conn:
        conn.execute("INSERT INTO module_drafts(id,owner,name,document,updated) VALUES(?,?,?,?,?)",
                     (key, owner, name, json.dumps(document), time.time()))
    return key


def list_drafts(owner):
    with connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id,name,updated,revision FROM module_drafts WHERE owner=? ORDER BY updated DESC", (owner,))]


def get(owner, key):
    with connection() as conn:
        row = conn.execute("SELECT * FROM module_drafts WHERE owner=? AND id=?", (owner, key)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["document"] = json.loads(result["document"])
    return result


def update(owner, key, revision, document, name=None):
    with connection() as conn:
        result = conn.execute("UPDATE module_drafts SET document=?,name=COALESCE(?,name),updated=?,revision=revision+1 WHERE owner=? AND id=? AND revision=?",
                              (json.dumps(document), name, time.time(), owner, key, revision))
        return result.rowcount == 1


def delete(owner, key, revision):
    with connection() as conn:
        result = conn.execute("DELETE FROM module_drafts WHERE owner=? AND id=? AND revision=?", (owner, key, revision))
        deleted = result.rowcount == 1
        if deleted and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='module_test_runs'").fetchone():
            columns = {r[1] for r in conn.execute('PRAGMA table_info(module_test_runs)')}
            if 'backend' in columns:
                # Leave only cleanup metadata until the reconciler deletes the Job.
                conn.execute("UPDATE module_test_runs SET status='deleted',settings='{}',result='{}',cleanup_pending=1 WHERE owner=? AND draft_id=? AND backend='kubernetes'", (owner,key))
                conn.execute("DELETE FROM module_test_runs WHERE owner=? AND draft_id=? AND backend!='kubernetes'", (owner,key))
            else:
                conn.execute("DELETE FROM module_test_runs WHERE owner=? AND draft_id=?", (owner,key))
        return deleted
