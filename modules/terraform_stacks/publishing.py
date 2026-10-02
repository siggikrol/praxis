"""Publish a saved, sanity-tested snapshot; keep the local catalog linked to its PR."""
import uuid
from pathlib import PurePosixPath
from modules.terraform_module_builder import store as drafts, testing, authoring
from . import github, store, releases, repositories


def checked_draft(owner, key, revision):
    draft = drafts.get(owner, key)
    if not draft or draft['revision'] != revision:
        raise ValueError('The draft changed. Reload and test the current saved revision before submitting.')
    result = testing.latest(owner, key)
    if not result or result['status'] != 'passed' or result['revision'] != revision or result.get('mode') == 'format' or result.get('result', {}).get('check_suite') != 2:
        raise ValueError('Run a successful sanity check on this saved revision before submitting it to Git.')
    authoring.validate_files(draft['document']['files'])
    if any(PurePosixPath(p).parts[0] in ('.github', '.git') for p in draft['document']['files']):
        raise ValueError('Publish reviewed GitHub workflows separately; draft submissions cannot modify .github or .git files.')
    return draft


def submit(owner, draft_id, revision, name, repo, generic, create=False, change_type="feat"):
    if change_type not in ('feat', 'fix', 'feat!'):
        raise ValueError('Choose feature, fix or breaking change for the release.')
    if not generic:
        raise ValueError('Confirm that the module contains no deployment-specific configuration.')
    draft = checked_draft(owner, draft_id, revision)
    if not github.connected():
        raise ValueError('Configure a GitHub API token or GitHub App in GitHub settings first. SSH keys are not used here.')
    data = {'repository': repo, 'status': 'Development', 'provider': draft['provider'],
            'description': 'Submitted from Praxis Module Builder.', 'compatibility': '', 'versions': []}
    store.validate(owner, 'module', name, data)
    modules = store.objects(owner, 'module')
    existing = next((m for m in modules if m['data']['repository'].lower() == repo.lower()), None)
    same_name = next((m for m in modules if m['name'] == name), None)
    if same_name and (not existing or same_name['id'] != existing['id']):
        raise ValueError('This module name already belongs to another repository.')
    if existing and not create:
        existing = repositories.check(owner, existing)
        previous = existing['data'].get('submission', {})
        if previous.get('draft') == draft_id and previous.get('revision') == revision:
            try:
                remote = github.request('GET', github.repo_path(repo) + '/pulls/' + str(previous['number']))
            except ValueError as exc:
                raise ValueError('Cannot verify the previous submission. If you deleted the repository, select Create a new private repository to recreate it. ' + str(exc)) from exc
            if previous.get('github_id') and previous['github_id'] != remote.get('id'):
                raise ValueError('The repository or pull request has been replaced. The saved submission no longer matches GitHub.')
            if remote.get('state') == 'open' or remote.get('merged'):
                return existing['id'], remote['html_url']
    reserved = {'.release-please-manifest.json', 'release-please-config.json', 'version.txt'}
    if reserved & draft['document']['files'].keys():
        raise ValueError('Release configuration is managed in Git. Remove release-please-config.json, .release-please-manifest.json and version.txt from the draft before submitting.')
    release_permissions = None
    if create:
        created = github.create_repository(repo)
        if isinstance(created, dict):
            release_permissions = created.get('praxis_release_permissions')
        if existing:
            clean = dict(existing['data'], versions=[], checks=[], current_version='', status='Development')
            clean.pop('repository_health', None)
            clean.pop('submission', None)
            clean.pop('release_workflow_pr', None)
            store.save(owner, 'module', existing['name'], clean, existing['id'], existing['revision'])
            existing = store.get(owner, existing['id'])
    else:
        github.head(repo)  # Check access and initialized branch before creating local metadata.
    if release_permissions is not None:
        if existing:
            updated = dict(existing['data'], release_permissions=release_permissions)
            store.save(owner, 'module', existing['name'], updated, existing['id'], existing['revision'])
            existing = store.get(owner, existing['id'])
        else:
            data['release_permissions'] = release_permissions
    workflow_files = releases.setup_files(repo, preserve_existing=True)
    files = dict(draft['document']['files'], **workflow_files)
    if not existing:
        module_id = store.save(owner, 'module', name, data)
        existing = store.get(owner, module_id)
    try:
        pr = github.pull_request(repo, 'praxis/module-' + uuid.uuid4().hex,
                                 files, change_type + ': update module ' + existing['name'])
    except ValueError as exc:
        if workflow_files:
            raise ValueError(str(exc) + ' This submission includes GitHub workflow files; the Praxis token/App also needs Workflows write permission. The module PR was not confirmed.') from exc
        raise
    data = dict(existing['data'], provider=draft['provider'], submission={
        'draft': draft_id, 'revision': revision, 'url': pr['html_url'], 'number': pr['number'], 'github_id': pr.get('id')})
    store.save(owner, 'module', existing['name'], data, existing['id'], existing['revision'])
    return existing['id'], pr['html_url']
