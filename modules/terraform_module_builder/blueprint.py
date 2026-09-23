import io
import json
import zipfile

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for

from . import authoring, registry, store, testing

bp = Blueprint('terraform_module_builder', __name__, url_prefix='/modules', template_folder='templates')


def owner():
    if not session.get('user'):
        abort(401)
    return str(session['user'])


@bp.before_request
def limit_body():
    request.max_content_length = 3_000_000
    request.max_form_memory_size = 3_000_000
    request.max_form_parts = 2000


@bp.route('/')
def index():
    query = request.args.get('q', '').strip()
    provider = request.args.get('provider', 'aws')
    if provider not in ('aws', 'google'):
        abort(400)
    view = 'drafts' if request.args.get('view') == 'drafts' else 'registry'
    results, error = [], None
    offset = max(0, min(request.args.get('offset', 0, type=int), 10000))
    if query:
        try:
            if '/' in query:
                return redirect(url_for('.choose', source=registry.module_address(query)))
            results = registry.search(query, offset, provider).get('modules', [])
        except registry.RegistryError as exc:
            error = str(exc)
    return render_template('terraform_module_builder/index.html', drafts=store.list_drafts(owner()), results=results,
                           query=query, offset=offset, error=error, step=1, provider=provider, view=view)


@bp.route('/choose')
def choose():
    try:
        module = registry.details(request.args.get('source'), request.args.get('version'))
    except registry.RegistryError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('.index'))
    return render_template('terraform_module_builder/choose.html', module=module, step=2)


@bp.route('/customize', methods=['GET', 'POST'])
def customize():
    values = request.form if request.method == 'POST' else request.args
    try:
        module = registry.details(values.get('source'), values.get('version'))
    except registry.RegistryError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('.index'))
    error = None
    if request.method == 'POST':
        try:
            name = values.get('name', '').strip()
            if not authoring.NAME.fullmatch(name) or len(name) > 80:
                raise ValueError('Choose a name using letters, numbers, underscores and hyphens, starting with a letter or underscore (up to 80).')
            provenance = {'address': module['address'], 'version': module['version'], 'registry_url': module['registry_url'], 'mode': values.get('mode', 'wrapper')}
            warnings = []
            if provenance['mode'] == 'example':
                files, warnings, origin = registry.import_example(module, values.get('example'))
                provenance.update(origin)
            elif provenance['mode'] == 'wrapper':
                choices = {item['name']: (values.get('mode_'+item['name'], 'expose' if item.get('required') else 'default'), values.get('value_'+item['name'], '')) for item in module['root'].get('inputs', [])}
                files = authoring.wrapper(module, name, choices, values.getlist('outputs'))
            else:
                raise ValueError('Choose wrapper or example.')
            authoring.validate_files(files)
            key = store.create(owner(), name, {'files': files, 'source': provenance, 'warnings': warnings})
            return redirect(url_for('.draft', key=key))
        except ValueError as exc:
            error = str(exc)
    return render_template('terraform_module_builder/customize.html', module=module, values=values, error=error, step=3)


@bp.route('/drafts/<key>', methods=['GET', 'POST'])
def draft(key):
    item = store.get(owner(), key)
    if not item:
        abort(404)
    error, status = None, 200
    if request.method == 'POST':
        files = {name: request.form.get(f'file_{i}', content) for i, (name, content) in enumerate(sorted(item['document']['files'].items()))}
        item['document']['files'] = files
        item['name'] = request.form.get('name', item['name']).strip()
        item['revision'] = request.form.get('revision', type=int)
        try:
            if not authoring.NAME.fullmatch(item['name']) or len(item['name']) > 80:
                raise ValueError('Use a draft name starting with a letter or underscore, with up to 80 letters, numbers, underscores or hyphens.')
            authoring.validate_files(files)
            revision = request.form.get('revision', type=int)
            if not store.update(owner(), key, revision, item['document'], item['name']):
                item['revision'] = revision
                error, status = 'This draft changed in another tab. Copy your edits, then reload before saving.', 409
            else:
                flash('Draft saved. Terraform syntax checked.', 'success')
                return redirect(url_for('.draft', key=key))
        except ValueError as exc:
            error, status = str(exc), 400
    return render_template('terraform_module_builder/draft.html', draft=item, error=error, step=4, view='drafts', test_run=testing.latest(owner(), key)), status


@bp.post('/drafts/<key>/test')
def test_draft(key):
    item = store.get(owner(), key)
    if not item:
        abort(404)
    try:
        if request.form.get('revision', type=int) != item['revision']:
            raise ValueError('The draft changed. Reload and review the latest saved revision before testing.')
        mode = request.form.get('mode')
        if mode not in ('validate', 'mock'):
            raise ValueError('Choose validation or mock testing.')
        settings = {}
        for name in ('variables', 'data_defaults', 'expected_outputs'):
            settings[name] = json.loads(request.form.get(name, '{}') or '{}')
            if not isinstance(settings[name], dict):
                raise ValueError('Test settings must be JSON objects.')
        testing.start(owner(), item, mode, settings)
        return redirect(url_for('.test_results', key=key))
    except ValueError as exc:
        return render_template('terraform_module_builder/draft.html', draft=item, error=str(exc), step=4,
                               view='drafts', test_run=testing.latest(owner(), key), test_values=request.form), 400


@bp.get('/drafts/<key>/tests')
def test_results(key):
    item = store.get(owner(), key)
    if not item:
        abort(404)
    return render_template('terraform_module_builder/tests.html', draft=item,
                           test_run=testing.latest(owner(), key), step=4, view='drafts')


@bp.route('/drafts/<key>/delete', methods=['GET', 'POST'])
def delete_draft(key):
    item = store.get(owner(), key)
    if not item:
        abort(404)
    error, status = None, 200
    if request.method == 'POST':
        if request.form.get('confirm') != 'delete':
            error, status = 'Confirm that you want to delete this draft.', 400
        elif not store.delete(owner(), key, request.form.get('revision', type=int)):
            error, status = 'This draft changed since you opened the confirmation. Review the current revision below before deleting.', 409
        else:
            flash(f'Deleted draft {item["name"]}.', 'success')
            return redirect(url_for('.index', view='drafts'))
    return render_template('terraform_module_builder/delete.html', draft=item, error=error, step=4, view='drafts'), status


@bp.get('/drafts/<key>/download')
def download(key):
    item = store.get(owner(), key)
    if not item:
        abort(404)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in item['document']['files'].items():
            archive.writestr(item['name'] + '/' + name, content)
        archive.writestr(item['name'] + '/praxis-source.json', json.dumps(item['document']['source'], indent=2) + '\n')
        archive.writestr(item['name'] + '/PRAXIS-IMPORT-NOTES.txt', 'Syntax checked only. Review before use.\n' + '\n'.join(item['document']['warnings']))
    buffer.seek(0)
    return send_file(buffer, mimetype='application/zip', as_attachment=True, download_name=item['name']+'.zip')
