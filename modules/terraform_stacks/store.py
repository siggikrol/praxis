"""Small persistent catalog; code and published stack definitions live in Git."""
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

KINDS = {'module', 'customer', 'environment', 'account', 'stack'}
SLUG = re.compile(r'^[a-z][a-z0-9-]{0,62}$')
REPO = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
SHA = re.compile(r'^[a-f0-9]{40}$')


@contextmanager
def connection():
    path = Path(os.getenv('PRAXIS_TERRAFORM_DB', '/tmp/praxis-cache/terraform-foundation.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS tf_objects (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
            data TEXT NOT NULL, revision INTEGER NOT NULL, updated REAL NOT NULL,
            UNIQUE(owner,kind,name))''')
        db.execute('''CREATE TABLE IF NOT EXISTS tf_runs (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, stack_id TEXT NOT NULL,
            operation TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
            created REAL NOT NULL)''')
        yield db


def decode(row):
    if not row:
        raise ValueError('The requested object was not found.')
    item = dict(row)
    item['data'] = json.loads(item['data'])
    return item


def get(owner, key, kind=None):
    with connection() as db:
        row = db.execute('SELECT * FROM tf_objects WHERE owner=? AND id=?', (owner, key)).fetchone()
    item = decode(row)
    if kind and item['kind'] != kind:
        raise ValueError('Unexpected object type.')
    return item


def objects(owner, kind):
    with connection() as db:
        return [decode(r) for r in db.execute('SELECT * FROM tf_objects WHERE owner=? AND kind=? ORDER BY name', (owner, kind))]


def validate(owner, kind, name, data):
    if kind not in KINDS or not SLUG.fullmatch(name):
        raise ValueError('Use a lowercase name with letters, numbers and hyphens (up to 63 characters).')
    if kind == 'module':
        if not REPO.fullmatch(data.get('repository', '')):
            raise ValueError('Use a GitHub repository in owner/repository format.')
        if data.get('status') not in ('Development', 'Approved', 'Deprecated'):
            raise ValueError('Choose a module status.')
    elif kind == 'environment':
        get(owner, data.get('customer'), 'customer')
        if data.get('type') not in ('Development', 'Test', 'SIT', 'UAT', 'Staging', 'Production'):
            raise ValueError('Choose an environment type.')
    elif kind == 'account':
        if data.get('customer'):
            get(owner, data['customer'], 'customer')
        if data.get('provider') != 'aws':
            raise ValueError('Stack execution currently supports AWS only.')
        if data.get('account_id') and not re.fullmatch(r'\d{12}', data['account_id']):
            raise ValueError('AWS account ID must contain 12 digits, or leave it empty until connected.')
        if data.get('execution_environment') and not SLUG.fullmatch(data['execution_environment']):
            raise ValueError('Execution environment must be a lowercase slug.')
    elif kind == 'stack':
        customer = get(owner, data.get('customer'), 'customer')
        environment = get(owner, data.get('environment'), 'environment')
        account = get(owner, data.get('account'), 'account')
        module = get(owner, data.get('module'), 'module')
        if environment['data']['customer'] != customer['id']:
            raise ValueError('The environment belongs to a different customer.')
        if account['data'].get('customer') not in ('', None, customer['id']):
            raise ValueError('The cloud account belongs to a different customer.')
        versions = module['data'].get('versions', [])
        version = next((v for v in versions if v['tag'] == data.get('version')), None)
        if not version or not SHA.fullmatch(version.get('commit', '')):
            raise ValueError('Sync module versions from Git before creating a stack.')
        if module['data']['status'] == 'Deprecated':
            raise ValueError('Choose a module that is not deprecated.')
        if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', data.get('region', '')):
            raise ValueError('Enter an AWS region, for example eu-central-1.')
        if not isinstance(data.get('inputs'), dict):
            raise ValueError('Inputs must be a JSON object.')
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) or k in ('source', 'version', 'providers', 'count', 'for_each', 'depends_on') for k in data['inputs']):
            raise ValueError('Inputs must be valid module argument names, without reserved Terraform arguments.')
        data['module_commit'] = version['commit']


def save(owner, kind, name, data, key=None, revision=None):
    previous = get(owner, key, kind) if key else None
    validate(owner, kind, name, data)
    if kind == 'stack' and previous and all(previous['data'].get(k) == data.get(k) for k in ('module', 'version')):
        # A moved upstream tag must never silently change an existing stack's code.
        data['module_commit'] = previous['data']['module_commit']
    try:
        with connection() as db:
            if key:
                changed = db.execute('UPDATE tf_objects SET name=?,data=?,revision=revision+1,updated=? WHERE owner=? AND id=? AND kind=? AND revision=?',
                    (name, json.dumps(data), time.time(), owner, key, kind, revision)).rowcount
                if not changed:
                    raise ValueError('This object changed. Reload before saving.')
            else:
                key = uuid.uuid4().hex
                db.execute('INSERT INTO tf_objects VALUES(?,?,?,?,?,1,?)', (key, owner, kind, name, json.dumps(data), time.time()))
    except sqlite3.IntegrityError as exc:
        raise ValueError('An object of this type already uses that name.') from exc
    return key


def record(owner, stack_id, operation, data):
    key = uuid.uuid4().hex
    with connection() as db:
        db.execute('INSERT INTO tf_runs VALUES(?,?,?,?,?,?,?)', (key, owner, stack_id, operation, 'requesting', json.dumps(data), time.time()))
    return key


def runs(owner, stack_id):
    with connection() as db:
        return [decode(r) for r in db.execute('SELECT * FROM tf_runs WHERE owner=? AND stack_id=? ORDER BY created DESC', (owner, stack_id))]


def update_run(owner, key, status, data):
    with connection() as db:
        db.execute('UPDATE tf_runs SET status=?,data=? WHERE owner=? AND id=?', (status, json.dumps(data), owner, key))


def definition(owner, stack):
    d = stack['data']
    customer = get(owner, d['customer'], 'customer')
    environment = get(owner, d['environment'], 'environment')
    account = get(owner, d['account'], 'account')
    module = get(owner, d['module'], 'module')
    return {'schema_version': 1, 'id': stack['id'], 'name': stack['name'],
            'customer': customer['name'], 'environment': environment['name'],
            'module': {'name': module['name'], 'repository': module['data']['repository'], 'version': d['version'], 'commit': d['module_commit']},
            'target': {'provider': 'aws', 'account': account['name'], 'account_id': account['data'].get('account_id', ''), 'region': d['region'], 'execution_environment': account['data'].get('execution_environment', '')},
            'state': {'backend': 's3', 'key': 'stacks/' + stack['id'] + '/terraform.tfstate'}, 'inputs': d['inputs']}


def config_path(owner, stack):
    d = definition(owner, stack)
    return f"{d['customer']}/{d['environment']}/{d['name']}/stack.yaml"
