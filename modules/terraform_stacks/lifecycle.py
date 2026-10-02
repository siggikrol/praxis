"""Git-backed stack lifecycle. This process never executes Terraform."""
import base64
from pathlib import Path
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
    repo = github.repository()
    try:
        _, sha = github.head(repo)
    except github.RepositoryConflict:
        # Only initialize a repository whose commit listing confirms it is empty.
        try:
            commits = github.request('GET', github.repo_path(repo) + '/commits', params={'per_page': 1})
        except github.RepositoryConflict:
            commits = []
        if commits:
            raise ValueError('The stack repository has a conflict; resolve it in GitHub before publishing.')
        github.request('PUT', github.repo_path(repo) + '/contents/README.md', json={
            'message': 'Initialize Praxis stack configuration repository',
            'content': base64.b64encode(b'# Praxis environments\n\nStack configuration managed through reviewed pull requests.\n').decode()})
        _, sha = github.head(repo)
    tree = github.request('GET', github.repo_path(repo) + '/git/trees/' + sha, params={'recursive': 1})
    if tree.get('truncated'):
        raise ValueError('Repository tree is too large to safely inspect workflow setup.')
    present = {p['path'] for p in tree.get('tree', [])}
    root = Path(__file__).parent / 'workflows' / 'environments'
    files = {store.config_path(owner, stack): yaml.safe_dump(definition, sort_keys=False)}
    for path in root.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            name = path.relative_to(root).as_posix()
            if name not in present:
                files[name] = path.read_text()
    try:
        pr = github.pull_request(repo, 'praxis/stack-' + uuid.uuid4().hex, files, 'Configure stack ' + stack['name'])
    except ValueError as exc:
        raise ValueError(f'Stack configuration PR for {repo} could not be created. Installing workflow files requires Workflows write permission. {exc}') from exc
    data = dict(stack['data'], pull_request={'number': pr['number'], 'url': pr['html_url'], 'definition_hash': digest(definition)})
    store.save(owner, 'stack', stack['name'], data, stack['id'], stack['revision'])
    return pr


def merged_revision(owner, stack):
    repo = github.repository()
    try:
        branch, sha = github.head(repo)
    except github.RepositoryConflict as exc:
        raise ValueError(f'Stack configuration repository {repo} is not initialized. Use Submit configuration PR to initialize it and include the stack workflow, merge that PR in GitHub, then validate. No validation was started.') from exc
    try:
        actual = yaml.safe_load(github.contents(repo, store.config_path(owner, stack), sha))
    except github.ResourceNotFound as exc:
        raise ValueError(f'This stack is not available on the default branch of {repo}. Submit and merge its configuration PR before validating.') from exc
    if actual != store.definition(owner, stack):
        raise ValueError('The Git definition differs from this stack. Submit and merge its configuration PR first.')
    try:
        github.contents(repo, '.github/workflows/praxis-stack.yml', sha)
        github.contents(repo, 'scripts/praxis_stack.py', sha)
    except github.ResourceNotFound as exc:
        raise ValueError(f'Stack workflow setup is missing in {repo}. Submit and merge a configuration PR to install it before validating.') from exc
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
        if plan['data']['commit'] != sha or plan['data']['definition_hash'] != definition_hash or plan['data'].get('repository', github.repository()) != github.repository():
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
    if plan['data'].get('repository', github.repository()) != github.repository() or sha != plan['data']['commit'] or digest(store.definition(owner, stack)) != plan['data']['definition_hash'] or time.time() - plan['created'] > 86400:
        raise ValueError('This plan is stale. Request a new plan.')
    data = dict(plan['data'], approval={'by': owner, 'at': time.time(), 'commit': sha, 'plan_sha256': plan['data']['result']['plan_sha256']})
    with store.connection() as db:
        if not db.execute("UPDATE tf_runs SET status='approved',data=? WHERE owner=? AND id=? AND status='planned'", (json.dumps(data), owner, key)).rowcount:
            raise ValueError('Plan changed while approving. Refresh the page.')
