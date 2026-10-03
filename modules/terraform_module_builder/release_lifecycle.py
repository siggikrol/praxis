"""Bind deployment wrapper revisions to immutable organization module releases."""
from . import draft_wrappers, store, testing


def version_tuple(value):
    try:
        parts = tuple(int(part) for part in str(value).split('.'))
    except ValueError:
        return (-1, -1, -1)
    return parts if len(parts) == 3 else (-1, -1, -1)


def latest_release(owner, draft_id):
    releases = released_versions(owner, draft_id)
    return releases[0] if releases else None


def released_versions(owner, draft_id):
    """Return published root releases newest-first by semantic version."""
    releases=[]
    for release in store.releases(owner, draft_id):
        if release.get('layer') != 'organization-root':
            continue
        try:
            draft_wrappers.release_source(release)
        except ValueError:
            continue
        releases.append(release)
    return sorted(
        releases,
        key=lambda item: version_tuple(item['version']),
        reverse=True,
    )


def wrapper_release_state(owner, draft):
    source = draft['document'].get('source', {})
    if source.get('mode') != 'draft-wrapper':
        return {'current': None, 'latest': None, 'update_available': False}
    binding = source.get('wrapped_release', {})
    current = store.get_release(owner, binding.get('id', ''))
    root_id = (current or {}).get('draft_id') or source.get('wrapped_draft', {}).get('id')
    latest = latest_release(owner, root_id) if root_id else None
    return {
        'current': current,
        'latest': latest,
        'update_available': bool(
            current and latest
            and version_tuple(latest['version']) > version_tuple(current['version'])
        ),
    }


def upgrade_wrapper(owner, key, revision, release_id):
    draft = store.get(owner, key)
    if not draft:
        raise ValueError('Deployment wrapper draft not found.')
    if draft['revision'] != revision:
        raise ValueError('The wrapper changed. Reload before selecting a newer release.')
    source = draft['document'].get('source', {})
    if source.get('mode') != 'draft-wrapper':
        raise ValueError('Only deployment wrappers can use organization module releases.')
    target = store.get_release(owner, release_id)
    if not target or target['status'] != 'published' or target['layer'] != 'organization-root':
        raise ValueError('Choose a published organization module release.')
    state = wrapper_release_state(owner, draft)
    current = state['current']
    root_id = (current or {}).get('draft_id') or source.get('wrapped_draft', {}).get('id')
    if target['draft_id'] != root_id:
        raise ValueError('The selected release belongs to a different organization module.')
    if current and version_tuple(target['version']) <= version_tuple(current['version']):
        raise ValueError('Choose a newer organization module release.')

    previous_test = testing.latest(owner, key)
    module_name = source.get('module_name') or 'upstream'
    files = draft_wrappers.update_module_release(draft['document']['files'], module_name, target)
    root = store.get(owner, target['draft_id'])
    source.update({
        'address': draft_wrappers.release_source(target),
        'version': target['version'],
        'repository': target['repository'],
        'repository_url': target['repository_url'],
        'module_name': module_name,
        'wrapped_release': {
            'id': target['id'], 'version': target['version'], 'tag': target['tag'],
            'commit': target['commit_sha'], 'repository': target['repository'],
            'repository_url': target['repository_url'],
        },
        'wrapped_draft': {
            'id': target['draft_id'],
            'name': target['source_metadata'].get('draft_name') or (
                root['name'] if root else source.get('wrapped_draft', {}).get('name', '')
            ),
            'revision': target['draft_revision'],
        },
    })
    draft['document']['files'] = files
    if not store.update(owner, key, revision, draft['document']):
        raise ValueError('The wrapper changed while it was being updated. Reload and try again.')
    updated = store.get(owner, key)
    mode = previous_test.get('mode') if previous_test else 'validate'
    mode = mode if mode in ('validate', 'mock') else 'validate'
    settings = previous_test.get('settings', {}) if previous_test else {}
    try:
        job = testing.start(owner, updated, mode, settings)
        return updated, job, None
    except ValueError as exc:
        return updated, None, str(exc)
