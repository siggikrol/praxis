"""Draft and published networking templates; each release is an immutable snapshot."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import sqlite3

from flask import current_app, has_app_context

BUNDLES = ContextVar('networking_admin_bundle', default=None)
ARCHITECTURES = {
    'catalyst-single-vpc': 'Catalyst Single-VPC',
    'catalyst-multi-vpc': 'Catalyst Multi-VPC',
    'rgs': 'RGS',
    'loyalty': 'Loyalty',
}


@contextmanager
def content_overlay(bundle):
    token = BUNDLES.set(bundle)
    try:
        yield
    finally:
        BUNDLES.reset(token)


def _path():
    configured = current_app.config.get('NCR_TEMPLATE_DB') or os.getenv('PS_NCR_TEMPLATE_DB')
    readiness = current_app.config.get('ENVIRONMENT_READINESS_DB') or os.getenv('PS_ENVIRONMENT_READINESS_DB')
    return Path(configured or (str(Path(readiness).with_name('networking-templates.sqlite3')) if readiness else '/var/lib/praxis-environment-readiness/networking-templates.sqlite3'))


@contextmanager
def _connect():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE IF NOT EXISTS template_revisions (id INTEGER PRIMARY KEY, architecture TEXT NOT NULL, bundle TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS template_heads (architecture TEXT PRIMARY KEY, draft INTEGER, published INTEGER)')
    try:
        with con:
            yield con
    finally:
        con.close()


def state(architecture):
    if not has_app_context() or not _path().exists():
        return {'draft': None, 'published': None, 'history': []}
    with _connect() as con:
        head = con.execute('SELECT * FROM template_heads WHERE architecture=?', (architecture,)).fetchone()
        rows = con.execute('SELECT id, actor, created_at FROM template_revisions WHERE architecture=? ORDER BY id DESC LIMIT 20', (architecture,)).fetchall()
        return {**({'draft': head['draft'], 'published': head['published']} if head else {'draft': None, 'published': None}), 'history': [dict(row) for row in rows]}


def revision(architecture, revision_id):
    if not revision_id:
        return None
    with _connect() as con:
        row = con.execute('SELECT bundle FROM template_revisions WHERE architecture=? AND id=?', (architecture, revision_id)).fetchone()
        if not row:
            raise ValueError('Template revision not found.')
        return json.loads(row['bundle'])


def published_bundle(architecture):
    if architecture not in ARCHITECTURES:
        return None
    return revision(architecture, state(architecture)['published'])


def current_bundle(architecture):
    from .networking import load_profile, load_presentation
    head = state(architecture)
    stored = revision(architecture, head['draft'] or head['published'])
    if stored:
        return stored
    profile = load_profile(architecture)['profile']
    profile.setdefault('presentation', 'networking')
    presentation = load_presentation(profile['presentation'])['definition']
    return {f'profiles/{architecture}': profile, f"presentations/{profile['presentation']}": presentation}


def save(architecture, bundle, actor, expected):
    from .template_validation import validate_bundle
    validate_bundle(bundle, architecture)
    with _connect() as con:
        con.execute('BEGIN IMMEDIATE')
        head = con.execute('SELECT draft FROM template_heads WHERE architecture=?', (architecture,)).fetchone()
        if (head['draft'] if head else None) != expected:
            raise ValueError('Another administrator saved changes. Reload before editing again.')
        row = con.execute('INSERT INTO template_revisions (architecture,bundle,actor,created_at) VALUES (?,?,?,?)', (architecture, json.dumps(bundle), actor, datetime.now(timezone.utc).isoformat(timespec='seconds')))
        con.execute('INSERT INTO template_heads (architecture,draft) VALUES (?,?) ON CONFLICT(architecture) DO UPDATE SET draft=excluded.draft', (architecture, row.lastrowid))
        return row.lastrowid


def publish(architecture, revision_id, expected_published):
    with _connect() as con:
        con.execute('BEGIN IMMEDIATE')
        head = con.execute('SELECT * FROM template_heads WHERE architecture=?', (architecture,)).fetchone()
        if not head or head['draft'] != revision_id or head['published'] != expected_published:
            raise ValueError('The template changed. Reload and preview the current draft before publishing.')
        from .template_validation import validate_bundle
        stored = con.execute('SELECT bundle FROM template_revisions WHERE id=? AND architecture=?', (revision_id, architecture)).fetchone()
        validate_bundle(json.loads(stored['bundle']), architecture)
        con.execute('UPDATE template_heads SET published=? WHERE architecture=?', (revision_id, architecture))


def edit_bundle(bundle, architecture, form):
    bundle = deepcopy(bundle)
    profile = bundle[f'profiles/{architecture}']
    presentation = bundle[f"presentations/{profile['presentation']}"]
    if form.get('endpoint_editor') == '1':
        keys = form.getlist('endpoint_key')
        if len(keys) > 250 or len(set(keys)) != len(keys):
            raise ValueError('Application endpoints: use at most 250 unique rows.')
        endpoints = {}
        for key in keys:
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', key):
                raise ValueError('Invalid endpoint row identifier.')
            if key not in profile['endpoints'] and not key.startswith('new-'):
                raise ValueError('Unknown endpoint row. Reload the current draft.')
            item = deepcopy(profile['endpoints'].get(key, {
                'namespace': 'custom', 'gateways': 'custom',
                'attribution': 'Configured in networking template administration',
            }))
            for prop in ('host', 'name', 'category', 'protocol'):
                value = form.get(f'endpoints.{key}.{prop}', item.get(prop, '')).strip()
                if not value or len(value) > 300:
                    raise ValueError(f'Application endpoint: {prop} is required (maximum 300 characters).')
                item[prop] = value
            endpoints[key] = item
        profile['endpoints'] = endpoints
    for group, properties in {'vpcs': ('requested_prefix',), 'endpoints': ('host',), 'services': ('destination', 'protocol', 'purpose'), 'egress': ('destination', 'protocol', 'purpose'), 'routing': ('requirement',)}.items():
        for key, item in profile.get(group, {}).items():
            for prop in properties:
                field = f'{group}.{key}.{prop}'
                if field in form:
                    value = form[field].strip()
                    if prop == 'requested_prefix':
                        if not value:
                            continue
                        try:
                            value = int(value.lstrip('/'))
                        except ValueError:
                            raise ValueError(f'{key.upper()} CIDR prefix: enter a number such as /21.') from None
                        if not 0 <= value <= 32:
                            raise ValueError('CIDR prefixes must be between /0 and /32.')
                    item[prop] = value
    for name in ('registry_destinations', 'directory_destinations'):
        if f'defaults.{name}' in form:
            profile['defaults'][name] = [v.strip() for v in form[f'defaults.{name}'].splitlines() if v.strip()]
    for name in ('harbor_host', 'ad_access_group', 'bde_cidr', 'bde_vault_host', 'bde_rancher_ingress_host', 'bde_rancher_egress_host'):
        if name in profile['defaults'] and f'defaults.{name}' in form:
            profile['defaults'][name] = form[f'defaults.{name}'].strip()
    if 'cross_vpc.tcp_ports' in form and profile.get('cross_vpc'):
        try:
            profile['cross_vpc']['tcp_ports'] = [int(v.strip()) for v in form['cross_vpc.tcp_ports'].split(',')]
        except ValueError:
            raise ValueError('Cross-VPC TCP ports: enter comma-separated numbers from 1 to 65535.') from None
    if 'cross_vpc.ilp_egress.ports' in form and profile.get('cross_vpc', {}).get('ilp_egress'):
        try:
            values = form['cross_vpc.ilp_egress.ports'].split(',')
            if not all(re.fullmatch(r'[0-9]+', value.strip()) for value in values):
                raise ValueError
            ports = list(dict.fromkeys(int(value.strip()) for value in values))
            if not all(1 <= port <= 65535 for port in ports):
                raise ValueError
        except ValueError:
            raise ValueError('ILP egress ports: enter comma-separated numbers from 1 to 65535.') from None
        rule = profile['cross_vpc']['ilp_egress']
        rule.pop('port', None)
        rule['ports'] = ports
    for key in presentation['sections']:
        if f'section.{key}' in form:
            presentation['sections'][key] = form[f'section.{key}']
    return bundle
