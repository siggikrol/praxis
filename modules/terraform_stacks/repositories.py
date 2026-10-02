"""Persist repository health without deleting local module or stack history."""
from datetime import datetime, timezone
from . import github, store


def check(owner, item):
    repo = item['data']['repository']
    error = None
    try:
        github.request('GET', github.repo_path(repo))
        state, message = 'available', 'Repository is accessible.'
    except github.ResourceNotFound as exc:
        state = 'unavailable'
        message = 'Repository missing or inaccessible. GitHub cannot distinguish a deleted repository from a private repository your credential cannot access. Restore access in GitHub settings, edit the repository destination, or explicitly recreate a deleted repository using Submit a tested draft below.'
        error = exc
    except ValueError as exc:
        state, message, error = 'check_failed', str(exc), exc
    data = dict(item['data'], repository_health={
        'state': state, 'message': message, 'repository': repo,
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    store.save(owner, 'module', item['name'], data, item['id'], item['revision'])
    if error:
        raise ValueError(message) from error
    return store.get(owner, item['id'])
