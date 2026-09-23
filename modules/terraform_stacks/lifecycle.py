"""Git-backed stack lifecycle. This process never executes Terraform."""
import hashlib
import json
import os
import re
import time
import uuid

import yaml
from . import github, store


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def publish(owner, stack):
    definition = store.definition(owner, stack)
    pr = github.pull_request(github.repository(), 'praxis/stack-' + uuid.uuid4().hex,
        {store.config_path(owner, stack): yaml.safe_dump(definition, sort_keys=False)}, 'Configure stack ' + stack['name'])
    data = dict(stack['data'], pull_request={'number': pr['number'], 'url': pr['html_url'], 'definition_hash': digest(definition)})
    store.save(owner, 'stack', stack['name'], data, stack['id'], stack['revision'])
    return pr


def merged_revision(owner, stack):
    branch, sha = github.head(github.repository())
    actual = yaml.safe_load(github.contents(github.repository(), store.config_path(owner, stack), sha))
    if actual != store.definition(owner, stack):
        raise ValueError('The Git definition differs from this stack. Submit and merge its configuration PR first.')
    return branch, sha


def execution_ready(owner, stack):
    account = store.get(owner, stack['data']['account'], 'account')
    if os.getenv('PRAXIS_CLOUD_EXECUTION_ENABLED', '0') != '1':
        raise ValueError('Cloud execution is disabled. Configure an AWS execution account and GitHub environment before enabling it.')
    if not account['data'].get('account_id') or not account['data'].get('execution_environment'):
        raise ValueError('This AWS target needs an account ID and GitHub execution environment.')
    module = store.get(owner, stack['data']['module'], 'module')
    if module['data']['status'] != 'Approved':
        raise ValueError('Cloud execution requires an approved module.')


def start(owner, stack, operation, plan=None):
    if operation not in ('validate', 'plan', 'apply'):
        raise ValueError('Unknown operation.')
    if not github.connected():
        raise ValueError('Connect the GitHub App before requesting workflows.')
    if operation != 'validate':
        execution_ready(owner, stack)
    branch, sha = merged_revision(owner, stack)
    definition_hash = digest(store.definition(owner, stack))
    data = {'commit': sha, 'definition_hash': definition_hash, 'revision': stack['revision'], 'repository': github.repository()}
    if operation == 'apply':
        if not plan or plan['operation'] != 'plan' or plan['status'] != 'approved':
            raise ValueError('Approve a successful plan before applying it.')
        if plan['data']['commit'] != sha or plan['data']['definition_hash'] != definition_hash:
            raise ValueError('Configuration changed after planning. Create and approve a new plan.')
        if time.time() - plan['created'] > 86400:
            raise ValueError('The plan expired. Create and approve a new plan.')
        data.update(plan_run_id=str(plan['data']['github_run']), plan_sha256=plan['data']['result']['plan_sha256'], approval=plan['data']['approval'])
    with store.connection() as db:
        # Serialize starts across web workers, including the approval consumption below.
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT id FROM tf_runs WHERE owner=? AND stack_id=? AND status IN ('requesting','queued','in_progress')", (owner, stack['id'])).fetchone()
        if active:
            raise ValueError('A workflow request is already active. Refresh its result before starting another.')
        if operation == 'apply':
            consumed = db.execute("UPDATE tf_runs SET status='applying' WHERE owner=? AND id=? AND status='approved'", (owner, plan['id'])).rowcount
            if not consumed:
                raise ValueError('This approval has already been used.')
        key = uuid.uuid4().hex
        db.execute('INSERT INTO tf_runs VALUES(?,?,?,?,?,?,?)', (key, owner, stack['id'], operation, 'requesting', json.dumps(data), time.time()))
    inputs = {'operation': operation, 'stack_path': store.config_path(owner, stack), 'config_commit': sha,
              'request_id': key, 'execution_environment': store.definition(owner, stack)['target']['execution_environment'] or 'unconfigured',
              'plan_run_id': data.get('plan_run_id', ''), 'plan_sha256': data.get('plan_sha256', '')}
    try:
        github.dispatch(github.repository(), branch, inputs)
    except Exception:
        # A timeout can happen after acceptance: retain correlation ID and do not retry automatically.
        store.update_run(owner, key, 'requesting', dict(data, error='Dispatch not confirmed. Refresh to locate the run; do not blindly retry.'))
        raise
    store.update_run(owner, key, 'queued', data)
    return key


def refresh(owner, stack):
    for run in store.runs(owner, stack['id']):
        if run['status'] not in ('requesting', 'queued', 'in_progress'):
            continue
        data = run['data']
        matches = [r for r in github.workflow_runs(data['repository']) if r.get('display_title') == 'praxis-' + run['id'] and r.get('event') == 'workflow_dispatch' and r.get('path') == '.github/workflows/praxis-stack.yml']
        if not matches:
            continue
        remote = matches[0]
        data.update(github_run=remote['id'], url=remote['html_url'])
        if remote['status'] != 'completed':
            store.update_run(owner, run['id'], 'in_progress', data)
            continue
        status = 'failed'
        if remote['conclusion'] == 'success':
            result = github.report(data['repository'], remote['id'])
            if result.get('request_id') != run['id'] or result.get('commit') != data['commit'] or result.get('operation') != run['operation'] or result.get('stack_id') != stack['id']:
                raise ValueError('Workflow result does not match the requested stack and commit.')
            if run['operation'] == 'plan' and not re.fullmatch(r'[a-f0-9]{64}', result.get('plan_sha256', '')):
                raise ValueError('Plan result is missing its artifact checksum.')
            data['result'] = result
            status = 'passed' if run['operation'] == 'validate' else ('planned' if run['operation'] == 'plan' else 'applied')
        else:
            data['error'] = 'GitHub workflow finished: ' + str(remote['conclusion']) + '. Open the workflow log for details.'
        store.update_run(owner, run['id'], status, data)


def approve(owner, stack, key):
    execution_ready(owner, stack)
    plan = next((r for r in store.runs(owner, stack['id']) if r['id'] == key), None)
    if not plan or plan['operation'] != 'plan' or plan['status'] != 'planned':
        raise ValueError('Only a successful, unapplied plan can be approved.')
    _, sha = merged_revision(owner, stack)
    if sha != plan['data']['commit'] or digest(store.definition(owner, stack)) != plan['data']['definition_hash'] or time.time() - plan['created'] > 86400:
        raise ValueError('This plan is stale. Request a new plan.')
    data = dict(plan['data'], approval={'by': owner, 'at': time.time(), 'commit': sha, 'plan_sha256': plan['data']['result']['plan_sha256']})
    with store.connection() as db:
        if not db.execute("UPDATE tf_runs SET status='approved',data=? WHERE owner=? AND id=? AND status='planned'", (json.dumps(data), owner, key)).rowcount:
            raise ValueError('Plan changed while approving. Refresh the page.')
