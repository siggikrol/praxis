"""Supply reviewed release automation for module submissions and existing repositories."""
import json
from pathlib import Path
import re
import uuid
from . import github


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
