import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
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
        conn.execute("""CREATE TABLE IF NOT EXISTS module_releases (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, draft_id TEXT NOT NULL,
            draft_revision INTEGER NOT NULL, layer TEXT NOT NULL, status TEXT NOT NULL,
            version TEXT NOT NULL, tag TEXT NOT NULL, repository TEXT NOT NULL COLLATE NOCASE,
            repository_url TEXT NOT NULL DEFAULT '',
            commit_sha TEXT NOT NULL DEFAULT '', upstream_source TEXT NOT NULL,
            upstream_version TEXT NOT NULL, upstream_release_id TEXT,
            source_metadata TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
            UNIQUE(owner,draft_id,draft_revision), UNIQUE(owner,repository,tag))""")
        release_columns = {row[1] for row in conn.execute('PRAGMA table_info(module_releases)')}
        migrated = False
        if 'repository_url' not in release_columns:
            conn.execute("ALTER TABLE module_releases ADD COLUMN repository_url TEXT NOT NULL DEFAULT ''")
            migrated = True
        if conn.execute("SELECT 1 FROM module_releases WHERE repository_url='' LIMIT 1").fetchone():
            conn.execute(
                "UPDATE module_releases SET repository_url='https://github.com/' || repository "
                "WHERE repository_url=''"
            )
            migrated = True
        if migrated:
            conn.commit()
        yield conn


def create(owner, name, document):
    key = uuid.uuid4().hex
    with connection() as conn:
        conn.execute("INSERT INTO module_drafts(id,owner,name,document,updated) VALUES(?,?,?,?,?)",
                     (key, owner, name, json.dumps(document), time.time()))
    return key


def cloud_provider(document):
    source = document.get('source', {})
    provider = source.get('provider') or source.get('address', '').rstrip('/').split('/')[-1]
    return provider if provider in ('aws', 'google') else 'unknown'


def list_drafts(owner):
    with connection() as conn:
        result = []
        for row in conn.execute('SELECT id,name,updated,revision,document FROM module_drafts WHERE owner=? ORDER BY updated DESC', (owner,)):
            item = dict(row)
            document=json.loads(item.pop('document'))
            item['provider'] = cloud_provider(document)
            item['source'] = document.get('source',{})
            result.append(item)
        return result


def get(owner, key):
    with connection() as conn:
        row = conn.execute("SELECT * FROM module_drafts WHERE owner=? AND id=?", (owner, key)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["document"] = json.loads(result["document"])
    result["provider"] = cloud_provider(result["document"])
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


def snapshot_sha256(document):
    encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def decode_release(row):
    if not row:
        return None
    item = dict(row)
    item['source_metadata'] = json.loads(item['source_metadata'])
    item['repository_url'] = item.get('repository_url') or (
        'https://github.com/' + item['repository']
    )
    return item


def releases(owner, draft_id=None, status='published'):
    query = 'SELECT * FROM module_releases WHERE owner=?'
    values = [owner]
    if draft_id:
        query += ' AND draft_id=?'
        values.append(draft_id)
    if status:
        query += ' AND status=?'
        values.append(status)
    query += ' ORDER BY created DESC'
    with connection() as conn:
        return [decode_release(row) for row in conn.execute(query, values)]


def get_release(owner, key):
    with connection() as conn:
        row = conn.execute('SELECT * FROM module_releases WHERE owner=? AND id=?',
                           (owner, key)).fetchone()
    return decode_release(row)


def release_for_revision(owner, draft_id, revision):
    with connection() as conn:
        row = conn.execute(
            'SELECT * FROM module_releases WHERE owner=? AND draft_id=? AND draft_revision=?',
            (owner, draft_id, revision),
        ).fetchone()
    return decode_release(row)


def reserve_release(owner, draft, layer, version, tag, repository, upstream_source,
                    upstream_version, upstream_release_id=None):
    now = time.time()
    key = uuid.uuid4().hex
    metadata = {
        'draft_name': draft['name'],
        'source': draft['document'].get('source', {}),
        'file_paths': sorted(draft['document'].get('files', {})),
        'files': draft['document'].get('files', {}),
    }
    try:
        with connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute(
                'SELECT * FROM module_releases WHERE owner=? AND draft_id=? AND draft_revision=?',
                (owner, draft['id'], draft['revision']),
            ).fetchone()
            if existing:
                return decode_release(existing)
            conn.execute(
                '''INSERT INTO module_releases(
                    id,owner,draft_id,draft_revision,layer,status,version,tag,repository,repository_url,
                    upstream_source,upstream_version,upstream_release_id,source_metadata,
                    snapshot_sha256,created,updated)
                   VALUES(?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?)''',
                (key, owner, draft['id'], draft['revision'], layer, version, tag, repository,
                 'https://github.com/' + repository,
                 upstream_source, upstream_version, upstream_release_id,
                 json.dumps(metadata), snapshot_sha256(draft['document']), now, now),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError('That repository version is already reserved by another release.') from exc
    return get_release(owner, key)


def update_release(owner, key, status, commit_sha=None, error=''):
    if status not in ('pending', 'published', 'failed'):
        raise ValueError('Unknown release status.')
    with connection() as conn:
        changed = conn.execute(
            '''UPDATE module_releases SET status=?,commit_sha=COALESCE(?,commit_sha),
               error=?,updated=? WHERE owner=? AND id=?''',
            (status, commit_sha, error, time.time(), owner, key),
        ).rowcount
    if not changed:
        raise ValueError('Release record not found.')
    return get_release(owner, key)
