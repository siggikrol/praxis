import io
import json
import uuid
import zipfile
from pathlib import Path

import yaml
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for

from . import github, lifecycle, store

bp = Blueprint('terraform_stacks', __name__, url_prefix='/terraform/workspace', template_folder='templates')


def owner():
    if not session.get('user'):
        abort(401)
    return str(session['user'])


@bp.before_request
def limits():
    request.max_content_length = 1_000_000


@bp.context_processor
def context():
    return {'github_connected': github.connected(), 'config_repository': github.repository()}


@bp.route('/')
def index():
    return render_template('terraform_stacks/index.html', catalog={k: store.objects(owner(), k) for k in store.KINDS})


@bp.route('/new/<kind>', methods=['GET', 'POST'])
@bp.route('/edit/<kind>/<key>', methods=['GET', 'POST'])
def edit(kind, key=None):
    if kind not in store.KINDS:
        abort(404)
    item = store.get(owner(), key, kind) if key else None
    data = dict(item['data']) if item else {}
    error = None
    if request.method == 'POST':
        try:
            allowed = {'module': ['description', 'repository', 'compatibility', 'status'], 'customer': ['description'],
                'environment': ['customer', 'type', 'description'], 'account': ['customer', 'provider', 'account_id', 'execution_environment', 'description'],
                'stack': ['customer', 'environment', 'account', 'region', 'module', 'version']}
            data.update({k: request.form.get(k, '').strip() for k in allowed[kind]})
            if kind == 'module' and item and data['repository'] != item['data']['repository']:
                raise ValueError('Register a new module to use a different repository; existing version pins must remain valid.')
            if kind == 'stack':
                data['inputs'] = json.loads(request.form.get('inputs', '{}'))
            key = store.save(owner(), kind, request.form.get('name', '').strip(), data, key, request.form.get('revision', type=int))
            return redirect(url_for('.detail', key=key))
        except (ValueError, TypeError) as exc:
            error = str(exc)
    return render_template('terraform_stacks/edit.html', kind=kind, item=item, data=data, error=error,
                           catalog={k: store.objects(owner(), k) for k in store.KINDS})


@bp.route('/objects/<key>')
def detail(key):
    try:
        item = store.get(owner(), key)
    except ValueError:
        abort(404)
    definition = store.definition(owner(), item) if item['kind'] == 'stack' else None
    history = store.runs(owner(), key) if definition else []
    from modules.terraform_module_builder import store as drafts
    return render_template('terraform_stacks/detail.html', item=item, definition=definition,
        definition_yaml=yaml.safe_dump(definition, sort_keys=False) if definition else '', history=history,
        drafts=drafts.list_drafts(owner()) if item['kind'] == 'module' else [],
        lookup={o['id']: o for k in store.KINDS for o in store.objects(owner(), k)})


@bp.post('/objects/<key>/<action>')
def action(key, action):
    try:
        item = store.get(owner(), key)
        if action == 'sync' and item['kind'] == 'module':
            versions = github.versions(item['data']['repository'])
            _, commit = github.head(item['data']['repository'])
            checks = github.workflow_runs(item['data']['repository'], commit)
            data = dict(item['data'], versions=versions, current_version=versions[0]['tag'] if versions else '',
                        checks=[{'name': r['name'], 'status': r.get('conclusion') or r['status'], 'url': r['html_url'], 'commit': r['head_sha']} for r in checks[:10]])
            store.save(owner(), 'module', item['name'], data, key, item['revision'])
            flash(f'Synced {len(versions)} Git tags and latest branch checks.', 'success')
        elif action == 'publish-draft' and item['kind'] == 'module':
            from modules.terraform_module_builder import store as drafts, testing
            draft = drafts.get(owner(), request.form.get('draft'))
            result = testing.latest(owner(), draft['id']) if draft else None
            if not result or result['status'] != 'passed' or result['revision'] != draft['revision']:
                raise ValueError('Run a successful sanity check on the current draft revision before submitting it.')
            if request.form.get('generic') != 'yes':
                raise ValueError('Confirm that the module contains no deployment-specific configuration.')
            files = draft['document']['files']
            if any(p.startswith('.github/') for p in files):
                raise ValueError('Drafts cannot publish workflow files. Install reviewed module CI separately.')
            pr = github.pull_request(item['data']['repository'], 'praxis/module-' + uuid.uuid4().hex,
                files, 'Submit module ' + item['name'])
            flash('Module submitted for review: ' + pr['html_url'], 'success')
        elif action == 'publish' and item['kind'] == 'stack':
            lifecycle.publish(owner(), item)
            flash('Configuration pull request created. Merge it in GitHub before requesting a workflow.', 'success')
        elif action in ('validate', 'plan', 'apply', 'approve', 'refresh') and item['kind'] == 'stack':
            if action == 'refresh':
                lifecycle.refresh(owner(), item)
            elif action == 'approve':
                lifecycle.approve(owner(), item, request.form.get('run'))
            else:
                plan = next((r for r in store.runs(owner(), key) if r['id'] == request.form.get('run')), None)
                lifecycle.start(owner(), item, action, plan)
            flash('Workflow request updated.', 'success')
        else:
            abort(404)
    except (ValueError, KeyError) as exc:
        flash(str(exc), 'warning')
    return redirect(url_for('.detail', key=key))


@bp.get('/objects/<key>/stack.yaml')
def download(key):
    stack = store.get(owner(), key, 'stack')
    return send_file(io.BytesIO(yaml.safe_dump(store.definition(owner(), stack), sort_keys=False).encode()),
                     mimetype='application/yaml', as_attachment=True, download_name='stack.yaml')


@bp.get('/workflow-kit.zip')
def kit():
    stream = io.BytesIO()
    root = Path(__file__).parent / 'workflows'
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                archive.write(path, path.relative_to(root).as_posix())
    stream.seek(0)
    return send_file(stream, mimetype='application/zip', as_attachment=True, download_name='praxis-github-foundation.zip')
