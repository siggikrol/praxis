"""GitHub transport using Praxis's existing GitHub App authentication."""
import base64
import io
import json
import os
import zipfile
from urllib.parse import quote

import requests
from services.github_helpers import github_api
from . import store, settings


def repository():
    repo = settings.load()['repository']
    if not store.REPO.fullmatch(repo):
        raise ValueError('Invalid environments repository configuration.')
    return repo


def connected():
    data = settings.load()
    if data['auth_mode'] == 'token':
        return bool(data.get('token'))
    return all(os.getenv(k) for k in ('GITHUB_APP_ID', 'GITHUB_INSTALLATION_ID', 'GITHUB_PRIVATE_KEY'))


def access_token(owner=None):
    """Return the configured HTTPS Git credential for one ephemeral operation."""
    data = settings.load(owner)
    if data['auth_mode'] == 'token':
        return settings.token(owner)
    if all(os.getenv(k) for k in ('GITHUB_APP_ID', 'GITHUB_INSTALLATION_ID', 'GITHUB_PRIVATE_KEY')):
        try:
            return github_api.get_token()
        except RuntimeError as exc:
            raise ValueError('GitHub App authentication could not create an installation token.') from exc
    return None


def transport(method, path, **kwargs):
    token = settings.token()
    if token:
        return requests.request(method, 'https://api.github.com' + path, timeout=30,
            headers={'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + token}, **kwargs)
    if connected():
        return github_api._request(method, path, **kwargs)
    if method != 'GET':
        raise ValueError('GitHub is not connected. Configure an API token or GitHub App in GitHub settings. SSH keys are not used for submission.')
    return requests.get('https://api.github.com' + path, timeout=30, headers={'Accept': 'application/vnd.github+json'}, **kwargs)


class RepositoryConflict(ValueError):
    """GitHub returned a conflict; callers supply operation-specific guidance."""


class ResourceNotFound(ValueError):
    """GitHub 404: absent resource or inaccessible private resource."""


def request(method, path, **kwargs):
    try:
        response = transport(method, path, **kwargs)
        if response.status_code == 401:
            raise ValueError('GitHub rejected the credential (401). Replace the expired or invalid token, or check the GitHub App configuration.')
        if response.status_code == 403:
            raise ValueError('GitHub denied access (403). Check repository permissions, organization approval/SSO and API rate limits.')
        if response.status_code == 404:
            raise ResourceNotFound('GitHub repository or resource not found (404). Check that it exists and that your token or App has access; private repositories also return 404 when unauthorized.')
        if response.status_code == 409:
            raise RepositoryConflict('GitHub returned a repository conflict (409); the repository may not have an initial commit.')
        if response.status_code == 422:
            raise ValueError('GitHub could not create this change (422). Check for an existing repository, branch or PR and ensure the submission changes files.')
        if not 200 <= response.status_code < 300:
            raise ValueError(f'GitHub returned HTTP {response.status_code}. Retry later or check GitHub service status.')
        return response.json() if response.content else {}
    except (RuntimeError, requests.RequestException) as exc:
        raise ValueError('GitHub could not be reached or authenticated. Check the connection and GitHub settings.') from exc


def check_connection():
    if not connected():
        raise ValueError('No GitHub credential configured. Save an API token or configure the GitHub App first.')
    account = settings.load()['owner']
    target = request('GET', '/users/' + quote(account, safe=''))
    if settings.load()['auth_mode'] == 'token':
        identity = request('GET', '/user')['login']
        return f'Authenticated as {identity}. Destination {target["login"]} exists. Identity verified only; this does not verify access to module repositories.'
    request('GET', '/installation/repositories', params={'per_page': 1})
    return f'GitHub App authentication succeeded. Destination: {target["login"]}.'


def create_repository(repo):
    account, name = repo.split('/')
    target = request('GET', '/users/' + quote(account, safe=''))
    if target['type'] == 'Organization':
        path = '/orgs/' + quote(account, safe='') + '/repos'
    else:
        identity = request('GET', '/user')
        if identity['login'].lower() != account.lower():
            raise ValueError('A personal repository can only be created for the authenticated user. Create it in GitHub first or choose your own account.')
        path = '/user/repos'
    created = request('POST', path, json={'name': name, 'private': True, 'auto_init': True,
                                      'description': 'Reusable Terraform module managed through Praxis.'})
    try:
        configure_release_permissions(repo)
        created['praxis_release_permissions'] = {'configured': True}
    except ValueError as exc:
        created['praxis_release_permissions'] = {'configured': False, 'message': str(exc)}
    return created


def configure_release_permissions(repo):
    path = repo_path(repo) + '/actions/permissions/workflow'
    try:
        request('PUT', path, json={'can_approve_pull_request_reviews': True})
        if request('GET', path).get('can_approve_pull_request_reviews') is not True:
            raise ValueError('GitHub did not confirm the requested setting.')
    except ValueError as exc:
        raise ValueError('Repository exists, but release PR permission could not be configured. '
                         'Grant Administration write permission to the Praxis token/App and retry, '
                         'or enable Allow GitHub Actions to create and approve pull requests in '
                         'repository Settings → Actions → General. Organization policy may prevent this. '
                         + str(exc)) from exc


def repo_path(repo):
    if not store.REPO.fullmatch(repo):
        raise ValueError('Invalid repository.')
    return '/repos/' + repo


def head(repo):
    root = repo_path(repo)
    info = request('GET', root)
    branch = info['default_branch']
    return branch, request('GET', root + '/commits/' + quote(branch, safe=''))['sha']


def versions(repo):
    # Resolve tags to commits (including annotated tags); never use a mutable tag at execution time.
    root = repo_path(repo)
    result = []
    for page in range(1, 11):
        tags = request('GET', root + '/tags', params={'per_page': 100, 'page': page})
        result.extend({'tag': t['name'], 'commit': t['commit']['sha']} for t in tags)
        if len(tags) < 100:
            break
    return result


def contents(repo, path, ref):
    value = request('GET', repo_path(repo) + '/contents/' + quote(path, safe='/'), params={'ref': ref})
    if value.get('encoding') != 'base64':
        raise ValueError('Git file could not be read.')
    return base64.b64decode(value['content']).decode()


def pull_request(repo, branch, files, title):
    root = repo_path(repo)
    base, sha = head(repo)
    commit = request('GET', root + '/git/commits/' + sha)
    tree = request('POST', root + '/git/trees', json={'base_tree': commit['tree']['sha'], 'tree': [
        {'path': path, 'mode': '100644', 'type': 'blob', 'content': content} for path, content in files.items()]})
    new = request('POST', root + '/git/commits', json={'message': title, 'tree': tree['sha'], 'parents': [sha]})
    request('POST', root + '/git/refs', json={'ref': 'refs/heads/' + branch, 'sha': new['sha']})
    return request('POST', root + '/pulls', json={'title': title, 'head': branch, 'base': base,
        'body': 'Generated by Praxis. Review the configuration and required checks before merging.'})


def commit_files(repo, files, message, delete_paths=()):
    """Commit one Praxis release snapshot directly to the default branch."""
    root = repo_path(repo)
    branch, parent = head(repo)
    commit = request('GET', root + '/git/commits/' + parent)
    entries = [
        {'path': path, 'mode': '100644', 'type': 'blob', 'content': content}
        for path, content in sorted(files.items())
    ]
    entries.extend(
        {'path': path, 'mode': '100644', 'type': 'blob', 'sha': None}
        for path in sorted(set(delete_paths) - set(files))
    )
    tree = request('POST', root + '/git/trees', json={
        'base_tree': commit['tree']['sha'], 'tree': entries,
    })
    created = request('POST', root + '/git/commits', json={
        'message': message, 'tree': tree['sha'], 'parents': [parent],
    })
    request('PATCH', root + '/git/refs/heads/' + quote(branch, safe='/'),
            json={'sha': created['sha'], 'force': False})
    return created['sha']


def create_tag(repo, tag, commit):
    """Create an immutable lightweight tag, or accept an identical existing tag."""
    existing = next((item for item in versions(repo) if item['tag'] == tag), None)
    if existing:
        if existing['commit'] != commit:
            raise ValueError(f'Git tag {tag} already points to a different commit.')
        return existing
    request('POST', repo_path(repo) + '/git/refs',
            json={'ref': 'refs/tags/' + tag, 'sha': commit})
    return {'tag': tag, 'commit': commit}


def dispatch(repo, branch, inputs):
    request('POST', repo_path(repo) + '/actions/workflows/praxis-stack.yml/dispatches',
            json={'ref': branch, 'inputs': inputs})


def workflow_runs(repo, commit=None):
    params = {'per_page': 100}
    if commit:
        params['head_sha'] = commit
    return request('GET', repo_path(repo) + '/actions/runs', params=params).get('workflow_runs', [])


def _report(repo, run_id):
    root = repo_path(repo)
    artifacts = request('GET', root + f'/actions/runs/{int(run_id)}/artifacts')['artifacts']
    artifact = next((a for a in artifacts if a['name'] == 'praxis-result' and not a['expired']), None)
    if not artifact:
        raise ValueError('The workflow has no available Praxis result artifact.')
    # Redirect is a signed GitHub blob URL. Never forward the App Authorization header to it.
    response = transport('GET', root + f"/actions/artifacts/{artifact['id']}/zip", allow_redirects=False)
    if response.status_code != 302:
        raise ValueError('Could not download the workflow result.')
    with requests.get(response.headers['Location'], timeout=30, stream=True) as download:
        download.raise_for_status()
        chunks, size = [], 0
        for chunk in download.iter_content(65536):
            size += len(chunk)
            if size > 2_000_000:
                raise ValueError('Workflow result exceeds 2 MB.')
            chunks.append(chunk)
    with zipfile.ZipFile(io.BytesIO(b''.join(chunks))) as archive:
        info = archive.getinfo('result.json')
        if info.file_size > 1_000_000:
            raise ValueError('Workflow result is too large.')
        return json.loads(archive.read(info))


def report(repo, run_id):
    try:
        return _report(repo, run_id)
    except (requests.RequestException, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise ValueError('The workflow result could not be read. Refresh again or inspect its GitHub logs.') from exc


def repository_status(repo):
    """Read-only preflight; GitHub identity is not a repository permission check."""
    try:
        info = request('GET', repo_path(repo))
        if info.get('permissions', {}).get('push') is False:
            return False, f'{repo}: readable, but your GitHub user has no push permission.'
        head(repo)
        return True, f'{repo}: accessible and initialized. Token write permissions are verified when creating the PR.'
    except ValueError as exc:
        return False, f'{repo}: {exc}'
