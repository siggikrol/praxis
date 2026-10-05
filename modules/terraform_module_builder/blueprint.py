import io
import json
import zipfile

from flask import current_app, Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for

from . import authoring, draft_wrappers, mock_setup, registry, release_lifecycle, store, testing

bp = Blueprint('terraform_module_builder', __name__, url_prefix='/modules', template_folder='templates')


def owner():
    if not session.get('user'):
        abort(401)
    return str(session['user'])


def versioned_dependencies(files):
    dependencies = draft_wrappers.interface(files)['dependencies']
    result = []
    for dependency in dependencies:
        dependency = dict(dependency, available_versions=[], latest_version='', version_lookup_error='')
        source = str(dependency.get('source') or '')
        if len(source.split('/')) == 3:
            try:
                versions, latest = registry.available_versions(source)
                current = str(dependency.get('version') or '')
                if current and current not in versions:
                    versions.append(current)
                dependency.update(available_versions=versions, latest_version=latest)
            except registry.RegistryError:
                dependency['version_lookup_error'] = 'Published versions are temporarily unavailable.'
        result.append(dependency)
    return result


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
            provenance = {'provider': module['provider'], 'address': module['address'], 'version': module['version'], 'registry_url': module['registry_url'], 'mode': values.get('mode', 'wrapper')}
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
        files = {name: request.form.get(f'file_{i}', content) for i, (name, content) in enumerate(sorted(item['document']['files'].items())) if request.form.get(f'remove_{i}') != 'yes'}
        item['document']['files'] = files
        item['name'] = request.form.get('name', item['name']).strip()
        item['revision'] = request.form.get('revision', type=int)
        try:
            if not authoring.NAME.fullmatch(item['name']) or len(item['name']) > 80:
                raise ValueError('Use a draft name starting with a letter or underscore, with up to 80 letters, numbers, underscores or hyphens.')
            new_name = request.form.get('new_file_name', '').strip()
            if new_name:
                if new_name in files:
                    raise ValueError('A file with that name already exists.')
                files[new_name] = request.form.get('new_file_content', '')
            if not files:
                raise ValueError('Keep at least one file in the draft.')
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
    linked_modules = []
    if 'terraform_stacks.manage_module' in current_app.view_functions:
        from modules.terraform_stacks import store as catalog_store
        linked_modules = [m for m in catalog_store.objects(owner(), 'module') if m['data'].get('submission', {}).get('draft') == key]
    source=item['document'].get('source',{})
    parent=source.get('wrapped_draft',{}) if source.get('mode') == 'draft-wrapper' else {}
    parent_draft=store.get(owner(),parent.get('id')) if parent.get('id') else None
    test_run=testing.latest(owner(), key)
    release_history=store.releases(owner(), key)
    return render_template('terraform_module_builder/draft.html', draft=item, error=error, step=4, view='drafts', linked_modules=linked_modules,
                           test_run=test_run, dependencies=versioned_dependencies(item['document']['files']),
                           parent_draft=parent_draft, parent=parent, releases=release_history,
                           release_state=release_lifecycle.wrapper_release_state(owner(),item),
                           release_ready=testing.release_ready(owner(), key, item['revision'])), status


def available_wrapper_name(item):
    existing={draft['name'] for draft in store.list_drafts(owner())}
    base=(item['name'][:72]+'_wrapper')
    if base not in existing:
        return base
    number=2
    while True:
        suffix=f'_{number}'
        candidate=base[:80-len(suffix)]+suffix
        if candidate not in existing:
            return candidate
        number += 1


@bp.route('/drafts/<key>/wrap',methods=['GET','POST'])
def wrap_draft(key):
    item=store.get(owner(),key)
    if not item:
        abort(404)
    if item['document'].get('source', {}).get('mode') == 'draft-wrapper':
        abort(404)
    available_releases=release_lifecycle.released_versions(owner(),key)
    values=request.form if request.method == 'POST' else {
        'name':available_wrapper_name(item),
        'release_id':available_releases[0]['id'] if available_releases else '',
    }
    selected_release=next(
        (release for release in available_releases
         if release['id'] == values.get('release_id')),
        available_releases[0] if available_releases else None,
    )
    error=None
    if request.method == 'POST':
        try:
            if not available_releases:
                raise ValueError(
                    f'{item["name"]} has not been released. '
                    'Test and release the module before creating a deployment wrapper.'
                )
            if request.form.get('revision',type=int) != item['revision']:
                raise ValueError('The source draft changed. Reload and review its current interface.')
            wrapper_name=request.form.get('name','').strip()
            if any(draft['name'] == wrapper_name for draft in store.list_drafts(owner())):
                raise ValueError('A draft already uses this wrapper name. Choose a different name or remove the existing draft first.')
            release=store.get_release(owner(),request.form.get('release_id',''))
            if (not release or release['status'] != 'published' or release['draft_id'] != item['id']
                    or release['layer'] != 'organization-root'):
                raise ValueError('Choose a published release of this organization module.')
            released_files=release['source_metadata'].get('files')
            if not isinstance(released_files,dict) or not released_files:
                raise ValueError('This older release has no saved interface snapshot. Release the current revision before creating its wrapper.')
            parent_name=release['source_metadata'].get('draft_name') or item['name']
            files,warnings,metadata=draft_wrappers.build_released(
                released_files,wrapper_name,release,parent_name)
            document={'files':files,'warnings':warnings,'source':{
                'provider':item['provider'],'address':draft_wrappers.release_source(release),
                'version':release['version'],'repository':release['repository'],
                'repository_url':release['repository_url'],'mode':'draft-wrapper',
                'module_name':metadata['wrapper_module_name'],
                'wrapped_draft':{'id':item['id'],'name':parent_name,'revision':release['draft_revision']},
                'wrapped_release':{'id':release['id'],'version':release['version'],'tag':release['tag'],
                    'commit':release['commit_sha'],'repository':release['repository'],
                    'repository_url':release['repository_url']},
                'dependencies':metadata['dependencies'],
            }}
            created=store.create(owner(),wrapper_name,document)
            flash(f'Created deployment wrapper from {item["name"]} {release["tag"]}.','success')
            return redirect(url_for('.draft',key=created))
        except (ValueError,registry.RegistryError) as exc:
            error=str(exc)
    return render_template('terraform_module_builder/wrap_draft.html',draft=item,values=values,
                           releases=available_releases,selected_release=selected_release,
                           error=error,step=3,view='drafts'), (400 if error else 200)


@bp.post('/drafts/<key>/modules/<module_name>/version')
def update_module_version(key,module_name):
    item=store.get(owner(),key)
    if not item:
        abort(404)
    try:
        if item['document'].get('source',{}).get('mode') == 'draft-wrapper':
            raise ValueError('Deployment wrappers can only update to a published organization module release.')
        revision=request.form.get('revision',type=int)
        if revision != item['revision']:
            raise ValueError('The draft changed. Reload before updating its module version.')
        dependencies=draft_wrappers.interface(item['document']['files'])['dependencies']
        dependency=next((entry for entry in dependencies if entry['name'] == module_name),None)
        if not dependency:
            raise ValueError('The module dependency is no longer present in this draft.')
        files,version=draft_wrappers.update_module_version(
            item['document']['files'],module_name,request.form.get('module_version',''))
        item['document']['files']=files
        source=item['document'].get('source',{})
        if source.get('mode') == 'draft-wrapper' and source.get('address') == dependency['source']:
            source['version']=version
        if not store.update(owner(),key,revision,item['document']):
            raise ValueError('The draft changed while its module version was being updated. Reload and try again.')
        flash(f'Updated module.{module_name} to {version}. Run a check for the new draft revision.','success')
        return redirect(url_for('.draft',key=key)+'#test-panel')
    except ValueError as exc:
        linked_modules=[]
        return render_template('terraform_module_builder/draft.html',draft=item,error=str(exc),step=4,view='drafts',
                               linked_modules=linked_modules,test_run=testing.latest(owner(),key),
                               dependencies=versioned_dependencies(item['document']['files'])),400


@bp.post('/drafts/<key>/upgrade-release')
def upgrade_wrapper_release(key):
    item=store.get(owner(),key)
    if not item:
        abort(404)
    try:
        updated,job,test_error=release_lifecycle.upgrade_wrapper(
            owner(),key,request.form.get('revision',type=int),request.form.get('release_id',''))
        if test_error:
            flash(f'Wrapper updated to revision {updated["revision"]}, but its automatic check could not start: {test_error}','warning')
            return redirect(url_for('.draft',key=key)+'#test-panel')
        flash(f'Wrapper updated to revision {updated["revision"]}. Its mock test is running.','success')
        return redirect(url_for('.test_results',key=key))
    except ValueError as exc:
        flash(str(exc),'warning')
        return redirect(url_for('.draft',key=key))


@bp.post('/drafts/<key>/test')
def test_draft(key):
    item = store.get(owner(), key)
    if not item:
        abort(404)
    try:
        if request.form.get('revision', type=int) != item['revision']:
            raise ValueError('The draft changed. Reload and review the latest saved revision before testing.')
        mode = request.form.get('mode')
        if mode not in ('validate', 'mock', 'format', 'generate'):
            raise ValueError('Choose validation or mock testing.')
        if mode == 'generate':
            generated = mock_setup.generate(item['document']['files'])
            test_values = {name:json.dumps(generated[name], indent=2)
                           for name in ('variables', 'data_defaults', 'expected_outputs')}
            return render_template('terraform_module_builder/draft.html', draft=item, error=None, step=4,
                                   view='drafts', test_run=testing.latest(owner(), key), test_values=test_values,
                                   mock_setup=generated)
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


@bp.route('/upload', methods=['GET', 'POST'])
def upload():
    error = None
    if request.method == 'POST':
        try:
            from .uploads import import_zip
            name = request.form.get('name', '').strip()
            provider = request.form.get('provider', '')
            if not authoring.NAME.fullmatch(name) or len(name) > 80:
                raise ValueError('Enter a module name using letters, numbers, underscores and hyphens (up to 80 characters).')
            if provider not in ('aws', 'google', 'unknown'):
                raise ValueError('Choose the module cloud provider.')
            uploaded = request.files.get('archive')
            if not uploaded or not uploaded.filename:
                raise ValueError('Choose a module ZIP file.')
            files, warnings = import_zip(uploaded.stream.read(2_500_001))
            key = store.create(owner(), name, {'files': files, 'warnings': warnings,
                'source': {'address': 'Uploaded module', 'version': 'local', 'mode': 'upload', 'provider': provider}})
            flash('Module uploaded as a draft. Review the files and run a sanity check.', 'success')
            return redirect(url_for('.draft', key=key))
        except ValueError as exc:
            error = str(exc)
    return render_template('terraform_module_builder/upload.html', error=error, view='drafts', step=1), (400 if error else 200)
