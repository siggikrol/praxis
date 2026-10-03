"""Revision-bound test jobs, executed outside the web application."""
import json
import os
import threading
import time
import uuid

import requests
from .store import connection


def _table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS module_test_runs (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, draft_id TEXT NOT NULL,
        revision INTEGER NOT NULL, mode TEXT NOT NULL, created REAL NOT NULL,
        status TEXT NOT NULL, settings TEXT NOT NULL, result TEXT NOT NULL,
        backend TEXT NOT NULL DEFAULT 'http', cleanup_pending INTEGER NOT NULL DEFAULT 0)''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(module_test_runs)')}
    for name, definition in [('backend', "TEXT NOT NULL DEFAULT 'http'"), ('cleanup_pending', 'INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:
            try:
                conn.execute(f'ALTER TABLE module_test_runs ADD COLUMN {name} {definition}')
            except __import__('sqlite3').OperationalError as exc:
                if 'duplicate column' not in str(exc):
                    raise


def latest(owner, key):
    if os.getenv('PRAXIS_TEST_BACKEND') == 'kubernetes':
        reconcile()
    with connection() as conn:
        _table(conn)
        conn.execute("UPDATE module_test_runs SET status='failed',result=? WHERE status='running' AND backend='http' AND created<?",
                     (json.dumps({'error':'The worker stopped or exceeded its time limit. Run the check again.'}), time.time()-420))
        row = conn.execute('SELECT * FROM module_test_runs WHERE owner=? AND draft_id=? ORDER BY created DESC LIMIT 1', (owner,key)).fetchone()
    if not row:
        return None
    result = dict(row)
    result['result'] = json.loads(result['result'])
    result['settings'] = json.loads(result['settings'])
    return result


def start(owner, draft, mode, settings):
    backend = os.getenv('PRAXIS_TEST_BACKEND', 'http')
    endpoint = os.getenv('PRAXIS_TOFU_RUNNER_URL', '')
    if backend not in ('http', 'kubernetes'):
        raise ValueError('Unknown OpenTofu test backend.')
    payload = {'files':draft['document']['files'], 'mode':mode, 'settings':settings}
    source = draft['document'].get('source', {})
    if mode != 'format' and source.get('mode') == 'draft-wrapper':
        from modules.terraform_stacks import github
        token = github.access_token(owner)
        if token:
            payload['git_auth'] = {'host': 'github.com', 'token': token}
    if backend == 'kubernetes':
        reconcile()
        if not os.getenv('PRAXIS_TOFU_IMAGE'):
            raise ValueError('The Kubernetes test image is not configured.')
        from .kubernetes_jobs import payload_bytes
        payload_bytes(payload)
    if backend == 'http' and not endpoint:
        raise ValueError('OpenTofu runner is not configured. Start the Docker Compose setup to enable testing.')
    key = uuid.uuid4().hex
    with connection() as conn:
        _table(conn)
        conn.execute('BEGIN IMMEDIATE')
        running = conn.execute("SELECT count(*) FROM module_test_runs WHERE status='running' AND created>?", (time.time()-900,)).fetchone()[0]
        if running:
            raise ValueError('A check is already running. Wait for it to finish before starting another.')
        conn.execute('INSERT INTO module_test_runs(id,owner,draft_id,revision,mode,created,status,settings,result,backend) VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (key,owner,draft['id'],draft['revision'],mode,time.time(),'running',json.dumps(settings),'{}',backend))
    if backend == 'kubernetes':
        try:
            from . import kubernetes_jobs
            kubernetes_jobs.submit(key, draft['id'], payload)
        except Exception:
            with connection() as conn:
                conn.execute("UPDATE module_test_runs SET status='failed',result=?,cleanup_pending=1 WHERE id=?",
                             (json.dumps({'error':'Could not create the test Job. Check cluster availability, runner image, and namespace permissions.'}),key))
    else:
        threading.Thread(target=_execute, args=(endpoint,key,payload), daemon=True).start()
    return key


def save_format_result(conn, key, result):
    """Apply formatted files once, only while the tested revision still exists."""
    row = conn.execute("SELECT * FROM module_test_runs WHERE id=? AND status='running'", (key,)).fetchone()
    if not row or row['mode'] != 'format' or not result.get('passed'):
        return result
    result = dict(result)
    changed = result.pop('formatted_files', {})
    draft = conn.execute('SELECT document FROM module_drafts WHERE owner=? AND id=? AND revision=?',
                         (row['owner'], row['draft_id'], row['revision'])).fetchone()
    if not draft:
        return dict(result, passed=False, error='Draft changed or was deleted while formatting. Nothing was overwritten; retry on the current revision.')
    document = json.loads(draft['document'])
    if not isinstance(changed, dict) or any(name not in document['files'] or not isinstance(value, str) for name, value in changed.items()):
        return dict(result, passed=False, error='Invalid formatting result. Draft was not changed.')
    document['files'].update(changed)
    conn.execute('UPDATE module_drafts SET document=?,revision=revision+1,updated=? WHERE owner=? AND id=? AND revision=?',
                 (json.dumps(document), time.time(), row['owner'], row['draft_id'], row['revision']))
    return dict(result, saved_revision=row['revision'] + 1)


def _execute(endpoint, key, payload):
    try:
        response = requests.post(endpoint.rstrip('/')+'/run', json=payload, timeout=(5,390), allow_redirects=False)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Invalid runner response')
        status = 'passed' if response.ok and result.get('passed') else 'failed'
    except (requests.RequestException, ValueError):
        result, status = {'error':'Cannot complete the OpenTofu check. Verify the runner is available and try again.'}, 'failed'
    with connection() as conn:
        _table(conn)
        conn.execute('BEGIN IMMEDIATE')
        result = save_format_result(conn, key, result)
        status = 'passed' if status == 'passed' and result.get('passed') else 'failed'
        conn.execute('UPDATE module_test_runs SET status=?,result=? WHERE id=?', (status,json.dumps(result),key))


_RECONCILING = threading.Lock()


def reconcile():
    """Recover results after web restarts; cleanup can be safely retried by any worker."""
    if not _RECONCILING.acquire(blocking=False):
        return
    try:
        from . import kubernetes_jobs
        from kubernetes.client.exceptions import ApiException
        with connection() as conn:
            _table(conn)
            rows = [dict(r) for r in conn.execute("SELECT * FROM module_test_runs WHERE backend='kubernetes' AND (status='running' OR cleanup_pending=1)")]
        for row in rows:
            try:
                if row['status'] == 'deleted':
                    kubernetes_jobs.delete(row['id'])
                    with connection() as conn:
                        conn.execute('DELETE FROM module_test_runs WHERE id=?', (row['id'],))
                    continue
                if row['status'] == 'running':
                    try:
                        result = kubernetes_jobs.result(row['id'])
                    except ApiException as exc:
                        if exc.status == 404 and time.time()-row['created'] > 60:
                            result = {'error':'The Kubernetes Job is missing. Run the check again.'}
                        else:
                            raise
                    if result is None and time.time()-row['created'] > 900:
                        result = {'error':'The Kubernetes Job exceeded the overall time limit.'}
                        kubernetes_jobs.delete(row['id'])
                    if result is None:
                        continue
                    with connection() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        result = save_format_result(conn, row['id'], result)
                        conn.execute("UPDATE module_test_runs SET status=?,result=?,cleanup_pending=1 WHERE id=? AND status='running'",
                                     ('passed' if result.get('passed') else 'failed',json.dumps(result),row['id']))
                # Persist before setting TTL. If the web process stops, retry this on restart.
                kubernetes_jobs.schedule_cleanup(row['id'])
                with connection() as conn:
                    conn.execute('UPDATE module_test_runs SET cleanup_pending=0 WHERE id=?', (row['id'],))
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Could not reconcile OpenTofu Job %s; will retry',row['id'],exc_info=True)
    finally:
        _RECONCILING.release()


def start_reconciler():
    if os.getenv('PRAXIS_TEST_BACKEND') != 'kubernetes':
        return
    def sweep():
        while True:
            try:
                reconcile()
            except Exception:
                import logging
                logging.getLogger(__name__).exception('OpenTofu result reconciliation failed')
            time.sleep(10)
    threading.Thread(target=sweep, daemon=True, name='praxis-job-results').start()
