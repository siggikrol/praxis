"""Supply reviewed release automation for module submissions and existing repositories."""
import json
import re
import uuid
from pathlib import Path, PurePosixPath

from modules.terraform_module_builder import authoring
from modules.terraform_module_builder import draft_wrappers
from modules.terraform_module_builder import registry
from modules.terraform_module_builder import store as draft_store
from modules.terraform_module_builder import testing

from . import github
from . import store as catalog_store


SEMVER = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')


def setup_files(repository, preserve_existing=False):
    branch, sha = github.head(repository)
    if branch != 'main':
        raise ValueError('The supplied release workflow targets main. Set main as the default branch or adapt the downloaded workflow kit.')
    root = Path(__file__).parent / 'workflows' / 'module'
    files = {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob('*') if p.is_file()}
    tree = github.request('GET', github.repo_path(repository) + '/git/trees/' + sha, params={'recursive': 1})
    if tree.get('truncated'):
        raise ValueError('Repository tree is too large to safely check for existing release configuration.')
    conflicts = set(files) & {p['path'] for p in tree.get('tree', [])}
    if preserve_existing and conflicts == set(files):
        return {}  # Existing version and release configuration remain owned by Git.
    if conflicts:
        raise ValueError('Existing release files need manual review; download the kit rather than overwriting: ' + ', '.join(sorted(conflicts)))
    versions = []
    for tag in github.versions(repository):
        match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', tag['tag'])
        if match:
            versions.append(tuple(map(int, match.groups())))
    version = '.'.join(map(str, max(versions))) if versions else '0.0.0'
    files['.release-please-manifest.json'] = json.dumps({'.': version}, indent=2) + '\n'
    files['version.txt'] = version + '\n'
    return files


def install(module):
    previous = module['data'].get('release_workflow_pr')
    if previous:
        return previous
    repository = module['data']['repository']
    files = setup_files(repository)
    try:
        pr = github.pull_request(repository, 'praxis/release-setup-' + uuid.uuid4().hex, files,
                                 'feat: configure module validation and releases')
    except ValueError as exc:
        raise ValueError(str(exc) + ' Installing workflow files also requires Workflows write permission on your token/App. No release has been created.') from exc
    return {'number': pr['number'], 'url': pr['html_url']}


def _version_tuple(value):
    match = SEMVER.fullmatch(str(value or ''))
    return tuple(map(int, match.groups())) if match else None


def next_version(repository, change_type, remote_versions=None, local_releases=None):
    if change_type not in ('feat', 'fix', 'feat!'):
        raise ValueError('Choose feature, fix or breaking change for the release.')
    found = []
    for item in remote_versions if remote_versions is not None else github.versions(repository):
        parsed = _version_tuple(item.get('tag'))
        if parsed:
            found.append(parsed)
    for item in local_releases or []:
        if item.get('repository', '').lower() == repository.lower():
            parsed = _version_tuple(item.get('version'))
            if parsed:
                found.append(parsed)
    major, minor, patch = max(found, default=(0, 0, 0))
    if change_type == 'feat!':
        major, minor, patch = major + 1, 0, 0
    elif change_type == 'feat':
        major, minor, patch = major, minor + 1, 0
    else:
        major, minor, patch = major, minor, patch + 1
    return f'{major}.{minor}.{patch}'


def checked_draft(owner, key, revision):
    draft = draft_store.get(owner, key)
    if not draft or draft['revision'] != revision:
        raise ValueError('The draft changed. Reload and test its current saved revision before releasing.')
    if not testing.release_ready(owner, key, revision):
        raise ValueError('Run a successful mock test on this saved revision before releasing it.')
    authoring.validate_files(draft['document']['files'])
    if any(PurePosixPath(path).parts[0] in ('.github', '.git')
           for path in draft['document']['files']):
        raise ValueError('Release drafts cannot modify .github or .git files.')
    return draft


def _upstream(owner, draft):
    source = draft['document'].get('source', {})
    if source.get('mode') == 'draft-wrapper':
        binding = source.get('wrapped_release', {})
        upstream = draft_store.get_release(owner, binding.get('id', ''))
        if (not upstream or upstream['status'] != 'published'
                or binding.get('version') != upstream['version']
                or binding.get('tag') != upstream['tag']
                or binding.get('commit') != upstream['commit_sha']
                or binding.get('repository') != upstream['repository']
                or binding.get('repository_url', upstream['repository_url']) != upstream['repository_url']):
            raise ValueError('This deployment wrapper is not pinned to a published organization module release.')
        if not testing.release_ready(owner, upstream['draft_id'], upstream['draft_revision']):
            raise ValueError(
                'The pinned organization module release has not passed a mock test for '
                'its released root revision. Test the root module before releasing this wrapper.'
            )
        dependencies = draft_wrappers.interface(draft['document']['files'])['dependencies']
        module_name = source.get('module_name') or 'upstream'
        selected = [item for item in dependencies if item['name'] == module_name]
        if (len(selected) != 1
                or selected[0]['source'] != draft_wrappers.release_source(upstream)
                or selected[0].get('release_tag') != upstream['tag']):
            raise ValueError('The wrapper code no longer matches its published organization module release pin.')
        return ('deployment-wrapper', f'git::https://github.com/{upstream["repository"]}.git',
                upstream['version'], upstream['id'])
    address = str(source.get('address') or '')
    version = str(source.get('version') or '')
    if not address or not version:
        raise ValueError('Organization root modules must retain their upstream source and version.')
    if source.get('mode') != 'upload':
        try:
            address = registry.module_address(address)
            version = registry.version_number(version)
        except registry.RegistryError as exc:
            raise ValueError('Organization root modules must retain a versioned public module source.') from exc
    return 'organization-root', address, version, None


def _previous_paths(owner, repository):
    for release in draft_store.releases(owner):
        if release['repository'].lower() == repository.lower():
            return release['source_metadata'].get('file_paths', [])
    return []


def _sync_catalog(owner, draft, release):
    """Keep the old catalog useful without making it the release source of truth."""
    modules = catalog_store.objects(owner, 'module')
    item = next((module for module in modules
                 if module['data']['repository'].lower() == release['repository'].lower()), None)
    data = dict(item['data']) if item else {
        'repository': release['repository'], 'status': 'Development',
        'description': 'Released from Praxis Module Builder.', 'compatibility': '',
    }
    versions = [entry for entry in data.get('versions', []) if entry.get('tag') != release['tag']]
    versions.insert(0, {'tag': release['tag'], 'commit': release['commit_sha']})
    data.update(provider=draft['provider'], versions=versions, current_version=release['tag'],
                release={'id': release['id'], 'draft': draft['id'],
                         'revision': draft['revision'], 'version': release['version']})
    name = draft['name'].lower().replace('_', '-')[:63]
    if not catalog_store.SLUG.fullmatch(name):
        name = 'module-' + draft['id'][:8]
    if item:
        catalog_store.save(owner, 'module', item['name'], data, item['id'], item['revision'])
    else:
        catalog_store.save(owner, 'module', name, data)


def promote(owner, draft_id, revision, repository, change_type='feat', create=False):
    """Publish one tested draft revision as a commit and semantic Git tag."""
    existing = draft_store.release_for_revision(owner, draft_id, revision)
    if existing and existing['status'] == 'published':
        return existing
    if existing and existing['commit_sha']:
        try:
            github.create_tag(existing['repository'], existing['tag'], existing['commit_sha'])
            release = draft_store.update_release(owner, existing['id'], 'published',
                                                 existing['commit_sha'])
            draft = draft_store.get(owner, draft_id)
            if draft:
                try:
                    _sync_catalog(owner, draft, release)
                except ValueError:
                    pass
            return release
        except ValueError as exc:
            draft_store.update_release(owner, existing['id'], 'failed',
                                       existing['commit_sha'], str(exc))
            raise

    draft = checked_draft(owner, draft_id, revision)
    if not catalog_store.REPO.fullmatch(repository):
        raise ValueError('Use a GitHub repository in owner/repository format.')
    if existing and existing['repository'].lower() != repository.lower():
        raise ValueError(f'This release is already reserved for {existing["repository"]}.')
    prior = draft_store.releases(owner, draft_id)
    if prior and any(item['repository'].lower() != repository.lower() for item in prior):
        raise ValueError(f'Continue releasing this module to {prior[0]["repository"]}.')
    claimed = [item for item in draft_store.releases(owner, status=None)
               if item['repository'].lower() == repository.lower()
               and item['draft_id'] != draft_id
               and item['status'] in ('pending', 'published')]
    if claimed:
        raise ValueError('That repository is already linked to another Praxis module draft.')
    if not github.connected():
        raise ValueError('Configure a GitHub API token or GitHub App before releasing this draft.')
    if create:
        github.create_repository(repository)
    else:
        github.head(repository)
    layer, upstream_source, upstream_version, upstream_release_id = _upstream(owner, draft)
    if existing:
        release = existing
    else:
        local = [item for item in draft_store.releases(owner, status=None)
                 if item['status'] in ('pending', 'published')]
        version = next_version(repository, change_type, local_releases=local)
        release = draft_store.reserve_release(
            owner, draft, layer, version, 'v' + version, repository,
            upstream_source, upstream_version, upstream_release_id,
        )
    if release['repository'].lower() != repository.lower():
        raise ValueError(f'This release is already reserved for {release["repository"]}.')
    repository = release['repository']
    try:
        commit = github.commit_files(
            repository, draft['document']['files'],
            f'{change_type}: release {draft["name"]} {release["version"]}',
            delete_paths=_previous_paths(owner, repository),
        )
        draft_store.update_release(owner, release['id'], 'pending', commit)
        github.create_tag(repository, release['tag'], commit)
        release = draft_store.update_release(owner, release['id'], 'published', commit)
    except ValueError as exc:
        current = draft_store.get_release(owner, release['id'])
        draft_store.update_release(owner, release['id'], 'failed',
                                   current.get('commit_sha') or None, str(exc))
        raise
    try:
        _sync_catalog(owner, draft, release)
    except ValueError:
        pass
    return release
