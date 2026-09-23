#!/usr/bin/env python3
"""One-time local cutover; never overwrite a Kubernetes workspace with drafts."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
KUBE = ['kubectl', '--context', 'rancher-desktop', '-n', 'praxis']
COMPOSE = ['docker', '--context', 'desktop-linux', 'compose']


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def main():
    deployment = json.loads(run(KUBE + ['get', 'deployment', 'praxis', '-o', 'json'], capture_output=True).stdout)
    image = deployment['spec']['template']['spec']['containers'][0]['image']
    helper = 'praxis-data-migration'
    stopped = False
    scaled = False
    success = False
    try:
        # Block browser writes while taking a consistent SQLite backup.
        run(COMPOSE + ['stop', 'nginx'])
        stopped = True
        snapshot = run(COMPOSE + ['exec', '-T', 'web', 'python', '-c', '''
import os, sqlite3, tempfile, sys
path=os.getenv('PRAXIS_TERRAFORM_MODULE_BUILDER_DB') or os.getenv('PRAXIS_MODULE_BUILDER_DB','/tmp/praxis-cache/module-builder.sqlite3')
source=sqlite3.connect('file:'+path+'?mode=ro',uri=True)
if source.execute("SELECT 1 FROM sqlite_master WHERE name='module_test_runs'").fetchone():
    assert not source.execute("SELECT 1 FROM module_test_runs WHERE status='running'").fetchone(), 'Wait for active tests to finish.'
with tempfile.TemporaryDirectory() as directory:
    target=sqlite3.connect(directory+'/backup.sqlite3')
    source.backup(target)
    target.close()
    sys.stdout.buffer.write(open(directory+'/backup.sqlite3','rb').read())
'''], capture_output=True).stdout
        directory = ROOT / '.local-backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        backup = directory / ('compose-' + str(int(time.time())) + '.sqlite3')
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as file:
            file.write(snapshot)
        with sqlite3.connect(backup) as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            count = conn.execute('SELECT count(*) FROM module_drafts').fetchone()[0]
        run(KUBE + ['scale', 'deployment/praxis', '--replicas=0'])
        scaled = True
        run(KUBE + ['wait', '--for=delete', 'pod', '-l', 'app=praxis', '--timeout=120s'])
        pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': helper}, 'spec': {
            'restartPolicy': 'Never', 'automountServiceAccountToken': False,
            'securityContext': {'runAsUser': 65532, 'runAsGroup': 65532, 'fsGroup': 65532},
            'containers': [{'name': 'copy', 'image': image, 'imagePullPolicy': 'IfNotPresent',
                'command': ['python', '-c', 'import time; time.sleep(600)'],
                'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}],
            'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'praxis-data'}}]}}
        run(KUBE + ['create', '-f', '-'], input=json.dumps(pod), text=True)
        run(KUBE + ['wait', '--for=condition=Ready', 'pod/' + helper, '--timeout=120s'])
        digest = hashlib.sha256(snapshot).hexdigest()
        run(KUBE + ['exec', '-i', helper, '--', 'python', '-c', '''
import hashlib, os, sqlite3, sys
from pathlib import Path
path=Path('/data/module-builder.sqlite3')
if path.exists():
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT count(*) FROM module_drafts').fetchone()[0]==0, 'Destination already contains drafts; refusing to replace it.'
data=sys.stdin.buffer.read()
assert hashlib.sha256(data).hexdigest()==sys.argv[1]
temporary=path.with_suffix('.import')
temporary.write_bytes(data)
with sqlite3.connect(temporary) as conn:
    assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
os.replace(temporary,path)
''', digest], input=snapshot)
        run(KUBE + ['delete', 'pod', helper, '--wait=true'])
        run(KUBE + ['scale', 'deployment/praxis', '--replicas=1'])
        scaled = False
        run(KUBE + ['rollout', 'status', 'deployment/praxis', '--timeout=180s'])
        # Compare every draft including ownership, contents and revision after startup.
        actual = run(KUBE + ['exec', 'deployment/praxis', '--', 'python', '-c',
            "import sqlite3,json; c=sqlite3.connect('/tmp/praxis-cache/module-builder.sqlite3'); print(json.dumps(c.execute('SELECT * FROM module_drafts ORDER BY id').fetchall()))"], capture_output=True).stdout
        with sqlite3.connect(backup) as conn:
            expected = [list(row) for row in conn.execute('SELECT * FROM module_drafts ORDER BY id')]
        assert json.loads(actual) == expected, 'Draft comparison failed; source is retained.'
        run(COMPOSE + ['stop', 'web', 'tofu-runner'])
        success = True
        print(f'Migrated and verified {count} drafts. Backup: {backup.relative_to(ROOT)}')
        print('Source Compose containers stopped; their data volume is retained.')
    finally:
        subprocess.run(KUBE + ['delete', 'pod', helper, '--ignore-not-found'], cwd=ROOT, capture_output=True)
        if scaled:
            run(KUBE + ['scale', 'deployment/praxis', '--replicas=1'])
        if stopped and not success:
            run(COMPOSE + ['start', 'nginx'])


if __name__ == '__main__':
    main()
