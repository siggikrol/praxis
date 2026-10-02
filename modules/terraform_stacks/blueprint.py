import io
import os
import json
import uuid
import zipfile
from pathlib import Path

import yaml
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for

from . import github, lifecycle, store, settings, publishing, repositories

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
    return {'cloud_execution_enabled': os.getenv('PRAXIS_CLOUD_EXECUTION_ENABLED', '0') == '1', 'github_connected': github.connected(), 'config_repository': github.repository(), 'git_owner': settings.load()['owner']}


def catalog_data():
    return {k: store.objects(owner(), k) for k in store.KINDS}


def stack_status(stack):
    runs = store.runs(owner(), stack['id'])
    applied = next((r for r in runs if r['status'] == 'applied'), None)
    status = 'Not deployed'
    if applied:
        status = 'Deployed' if applied['data']['definition_hash'] == lifecycle.digest(store.definition(owner(), stack)) else 'Deployed · changes pending'
    return status + (' · ' + runs[0]['operation'] + ': ' + runs[0]['status'] if runs else '')


@bp.route('/')
def index():
    catalog = catalog_data()
    query = request.args.get('q', '').strip()
    customers = [c for c in catalog['customer'] if query.casefold() in (c['name'] + ' ' + c['data'].get('description', '')).casefold()]
    pages = max(1, (len(customers) + 11) // 12)
    page = max(1, min(request.args.get('page', 1, type=int), pages))
    counts = {c['id']: {kind: sum(o['data'].get('customer') == c['id'] for o in catalog[kind]) for kind in ('environment', 'stack', 'account')} for c in customers}
    return render_template('terraform_stacks/index.html', customers=customers[(page-1)*12:page*12], counts=counts,
                           query=query, page=page, pages=pages, total=len(customers))


@bp.get('/catalog/<kind>')
def catalog(kind):
    if kind not in ('module', 'account'):
        abort(404)
    items = store.objects(owner(), kind)
    if kind == 'account':
        items = [o for o in items if not o['data'].get('customer')]
    return render_template('terraform_stacks/catalog.html', kind=kind, items=items)


@bp.route('/new/<kind>', methods=['GET', 'POST'])
@bp.route('/edit/<kind>/<key>', methods=['GET', 'POST'])
def edit(kind, key=None):
    if kind not in store.KINDS:
        abort(404)
    try:
        item = store.get(owner(), key, kind) if key else None
    except ValueError:
        abort(404)
    data = dict(item['data']) if item else {}
    scope = {}
    try:
        customer_id = data.get('customer') if item else request.args.get('customer')
        environment_id = data.get('environment') if item else request.args.get('environment')
        if kind == 'stack' and environment_id:
            scope['environment'] = store.get(owner(), environment_id, 'environment')
            parent = scope['environment']['data']['customer']
            if customer_id and customer_id != parent:
                abort(404)
            customer_id = parent
        if kind in ('environment', 'account', 'stack') and customer_id:
            scope['customer'] = store.get(owner(), customer_id, 'customer')
        data.update({k: v['id'] for k, v in scope.items()})
    except ValueError:
        abort(404)
    error = None
    if request.method == 'POST':
        try:
            allowed = {'module': ['description', 'repository', 'compatibility', 'status'], 'customer': ['description'],
                'environment': ['customer', 'type', 'description'], 'account': ['customer', 'provider', 'account_id', 'execution_environment', 'description'],
                'stack': ['customer', 'environment', 'account', 'region', 'module', 'version']}
            data.update({k: request.form.get(k, '').strip() for k in allowed[kind]})
            data.update({k: v['id'] for k, v in scope.items()})
            if kind == 'module' and item and data['repository'] != item['data']['repository']:
                raise ValueError('Register a new module to use a different repository; existing version pins must remain valid.')
            if kind == 'stack':
                data['inputs'] = json.loads(request.form.get('inputs', '{}'))
                json.dumps(data['inputs'], allow_nan=False)
            key = store.save(owner(), kind, request.form.get('name', '').strip(), data, key, request.form.get('revision', type=int))
            return redirect(url_for('.detail', key=key))
        except (ValueError, TypeError) as exc:
            error = str(exc)
    catalog = catalog_data()
    if scope.get('customer'):
        customer_id = scope['customer']['id']
        catalog['environment'] = [e for e in catalog['environment'] if e['data']['customer'] == customer_id]
        catalog['account'] = [a for a in catalog['account'] if a['data'].get('customer') in (None, '', customer_id)]
    return render_template('terraform_stacks/edit.html', kind=kind, item=item, data=data, error=error,
                           catalog=catalog, scope=scope)


@bp.route('/objects/<key>')
def detail(key):
    try:
        item = store.get(owner(), key)
    except ValueError:
        abort(404)
    if item['kind'] == 'module':
        from . import documentation
        docs, error = None, None
        try:
            docs = documentation.load(item, request.args.get('version', ''))
        except ValueError as exc:
            error = str(exc)
        return render_template('terraform_stacks/module_catalog.html', item=item, docs=docs, error=error)
    if item['kind'] in ('customer', 'environment'):
        catalog = catalog_data()
        customer = item if item['kind'] == 'customer' else store.get(owner(), item['data']['customer'], 'customer')
        environments = [e for e in catalog['environment'] if e['data']['customer'] == customer['id']]
        stacks = [s for s in catalog['stack'] if s['data'].get('environment') == item['id']]
        accounts = [a for a in catalog['account'] if a['data'].get('customer') == customer['id']]
        shared = [a for a in catalog['account'] if not a['data'].get('customer')]
        return render_template('terraform_stacks/scope.html', item=item, customer=customer, environments=environments,
            stacks=stacks, accounts=accounts, shared=shared,
            counts={e['id']: sum(s['data'].get('environment') == e['id'] for s in catalog['stack']) for e in environments},
            lookup={o['id']: o for items in catalog.values() for o in items},
            statuses={s['id']: stack_status(s) for s in stacks})
    definition = store.definition(owner(), item) if item['kind'] == 'stack' else None
    history = store.runs(owner(), key) if definition else []
    from modules.terraform_module_builder import store as drafts
    return render_template('terraform_stacks/detail.html', item=item, definition=definition,
        definition_yaml=yaml.safe_dump(definition, sort_keys=False) if definition else '', history=history,
        drafts=drafts.list_drafts(owner()) if item['kind'] == 'module' else [],
        lookup={o['id']: o for k in store.KINDS for o in store.objects(owner(), k)})


@bp.get('/builder/modules/<key>')
def manage_module(key):
    item = store.get(owner(), key, 'module')
    from modules.terraform_module_builder import store as drafts
    source = drafts.get(owner(), item['data'].get('submission', {}).get('draft', ''))
    return render_template('terraform_stacks/manage_module.html', item=item,
                           source_draft=source, drafts=drafts.list_drafts(owner()), authoring_page=True)


@bp.post('/objects/<key>/<action>')
def action(key, action):
    try:
        item = store.get(owner(), key)
        if action == 'configure-release-permissions' and item['kind'] == 'module':
            github.configure_release_permissions(item['data']['repository'])
            store.save(owner(), 'module', item['name'], dict(item['data'], release_permissions={'configured': True}), item['id'], item['revision'])
            flash('GitHub Actions can now create release pull requests. Re-run the failed release job in GitHub.', 'success')
        elif action == 'setup-releases' and item['kind'] == 'module':
            from . import releases
            pr = releases.install(item)
            store.save(owner(), 'module', item['name'], dict(item['data'], release_workflow_pr=pr), item['id'], item['revision'])
            flash('Release workflow PR prepared: ' + pr['url'] + '. Review and merge it to enable automation.', 'success')
        elif action == 'check-repository' and item['kind'] == 'module':
            item = repositories.check(owner(), item)
            ok, message = github.repository_status(item['data']['repository'])
            flash(message, 'success' if ok else 'warning')
        elif action == 'sync' and item['kind'] == 'module':
            item = repositories.check(owner(), item)
            versions = github.versions(item['data']['repository'])
            _, commit = github.head(item['data']['repository'])
            checks = github.workflow_runs(item['data']['repository'], commit)
            data = dict(item['data'], versions=versions, current_version=versions[0]['tag'] if versions else '',
                        checks=[{'name': r['name'], 'status': r.get('conclusion') or r['status'], 'url': r['html_url'], 'commit': r['head_sha']} for r in checks[:10]])
            store.save(owner(), 'module', item['name'], data, key, item['revision'])
            flash(f'Synced {len(versions)} Git tags and latest branch checks.', 'success')
        elif action == 'publish-draft' and item['kind'] == 'module':
            from modules.terraform_module_builder import store as drafts
            draft = drafts.get(owner(), request.form.get('draft'))
            if not draft:
                raise ValueError('Draft not found.')
            _, link = publishing.submit(owner(), draft['id'], draft['revision'], item['name'],
                item['data']['repository'], request.form.get('generic') == 'yes', request.form.get('create_repository') == 'yes', request.form.get('change_type', 'feat'))
            flash('Module submitted for review: ' + link, 'success')
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
    return redirect(url_for('.manage_module' if item['kind'] == 'module' else '.detail', key=key))


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


@bp.route('/github', methods=['GET', 'POST'])
def github_settings():
    user = owner()
    if request.method == 'POST':
        try:
            if request.form.get('action') == 'test':
                flash(github.check_connection(), 'success')
                repositories = list(dict.fromkeys([github.repository()] + [m['data']['repository'] for m in store.objects(user, 'module')]))
                for repo in repositories[:20]:
                    ok, message = github.repository_status(repo)
                    flash(message, 'success' if ok else 'warning')
                if len(repositories) > 20:
                    flash('Checked the first 20 repositories. Use Check repository on other module pages.', 'info')
            else:
                settings.save(user, request.form, request.form.get('token', ''), request.form.get('remove_token') == 'yes')
                flash('GitHub settings saved. Use Test connection to verify authentication.', 'success')
            return redirect(url_for('.github_settings'))
        except ValueError as exc:
            flash(str(exc), 'warning')
    data = settings.load(user)
    return render_template('terraform_stacks/github.html', settings=data, token_saved=bool(data.get('token')))


@bp.route('/submit-draft/<key>', methods=['GET', 'POST'])
def submit_draft(key):
    from modules.terraform_module_builder import store as drafts, testing
    draft = drafts.get(owner(), key)
    if not draft:
        abort(404)
    if request.method == 'POST':
        try:
            module_id, _ = publishing.submit(owner(), key, request.form.get('revision', type=int),
                request.form.get('name', '').strip(), request.form.get('repository', '').strip(),
                request.form.get('generic') == 'yes', request.form.get('create_repository') == 'yes', request.form.get('change_type', 'feat'))
            flash('Module registered in Modules & Stacks and submitted to GitHub for review. Merge the PR and tag a release before syncing versions.', 'success')
            return redirect(url_for('.manage_module', key=module_id))
        except ValueError as exc:
            flash(str(exc), 'warning')
    result = testing.latest(owner(), key)
    ready = bool(result and result['status'] == 'passed' and result['revision'] == draft['revision'] and result.get('result', {}).get('check_suite') == 2)
    return render_template('terraform_stacks/submit.html', draft=draft, ready=ready,
        module_name=draft['name'].lower().replace('_', '-'), git_settings=settings.load())
