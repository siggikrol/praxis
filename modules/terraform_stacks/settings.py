"""Per-user GitHub destinations and encrypted API tokens. No SSH keys are mounted."""
import base64
import hashlib
import json
import os
import re
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, has_request_context, session
from . import store


def current_owner():
    return str(session.get('user', '')) if has_request_context() else ''


def load(owner=None):
    owner = current_owner() if owner is None else owner
    defaults = {'owner': os.getenv('PRAXIS_GITHUB_OWNER', 'siggikrol'),
                'repository': os.getenv('PRAXIS_ENVIRONMENTS_REPOSITORY', 'siggikrol/praxis-environments'),
                'auth_mode': 'app', 'token': ''}
    if owner:
        with store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tf_git_settings (owner TEXT PRIMARY KEY, data TEXT NOT NULL)')
            row = db.execute('SELECT data FROM tf_git_settings WHERE owner=?', (owner,)).fetchone()
        if row:
            defaults.update(json.loads(row['data']))
    return defaults


def cipher():
    secret = current_app.secret_key
    if not secret:
        raise ValueError('Configure a stable Flask secret before storing a GitHub token.')
    raw = secret.encode() if isinstance(secret, str) else secret
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'praxis-github-token-v1\0' + raw).digest()))


def token():
    data = load()
    if data['auth_mode'] != 'token' or not data.get('token'):
        return None
    try:
        return cipher().decrypt(data['token'].encode()).decode()
    except InvalidToken as exc:
        raise ValueError('The saved GitHub token cannot be decrypted. Re-enter it in GitHub settings.') from exc


def save(owner, values, new_token='', remove=False):
    data = load(owner)
    account = values.get('owner', '').strip()
    repo = values.get('repository', '').strip()
    mode = values.get('auth_mode', 'app')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', account):
        raise ValueError('Enter a GitHub username or organization name.')
    if not store.REPO.fullmatch(repo):
        raise ValueError('Enter the stack configuration repository as owner/repository.')
    if mode not in ('app', 'token'):
        raise ValueError('Choose GitHub App or personal access token.')
    data.update(owner=account, repository=repo, auth_mode=mode)
    if remove:
        data['token'] = ''
    elif new_token.strip():
        value = new_token.strip()
        if len(value) > 512 or any(c.isspace() for c in value):
            raise ValueError('The token is invalid; paste only the token value.')
        data['token'] = cipher().encrypt(value.encode()).decode()
    with store.connection() as db:
        db.execute('INSERT OR REPLACE INTO tf_git_settings VALUES(?,?)', (owner, json.dumps(data)))
