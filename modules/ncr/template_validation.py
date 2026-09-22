"""Validate typed networking fields without interpreting descriptive requirements as DNS."""
import ipaddress
import re


def _hostname(value, *, wildcard=False):
    if wildcard and value.startswith('*.'):
        value = value[2:]
    value = value.rstrip('.')
    labels = value.split('.')
    return (len(value) <= 253 and len(labels) >= 2
            and not all(label.isdigit() for label in labels)
            and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels))


def validate_bundle(bundle, architecture):
    from .networking import _validate_profile
    profile = bundle[f'profiles/{architecture}']
    _validate_profile(profile)
    defaults = profile.get('defaults', {})
    if profile.get('platform') == 'catalyst':
        if not _hostname(defaults['harbor_host']):
            raise ValueError('Harbor hostname: enter a valid DNS name without a URL or port.')
        group = defaults.get('ad_access_group', '')
        if group and (len(group) > 128 or any(ord(c) < 32 for c in group) or any(c in group for c in '<>{}')):
            raise ValueError('AD access group: enter a group name, maximum 128 characters, without markup.')
    if 'bde_cidr' in defaults:
        try:
            ipaddress.IPv4Network(defaults['bde_cidr'], strict=True)
        except ValueError:
            raise ValueError('BDE network: enter a valid IPv4 network CIDR, such as 10.48.32.0/19.') from None
        for key in ('bde_vault_host', 'bde_rancher_ingress_host', 'bde_rancher_egress_host'):
            if not _destination(defaults[key].replace('<env>', 'environment')):
                raise ValueError(f'{key}: enter a valid BDE service URL; <env> is the supported environment placeholder.')
    for key, label in [('directory_destinations', 'Active Directory destinations'), ('registry_destinations', 'Container registries')]:
        if key not in defaults:
            continue
        values = defaults[key]
        if not values:
            raise ValueError(f'{label}: enter at least one destination.')
        for line, value in enumerate(values, 1):
            if key == 'directory_destinations':
                try:
                    ipaddress.ip_address(value)
                except ValueError:
                    raise ValueError(f'{label}, line {line}: "{value}" is not a valid IPv4 or IPv6 address.') from None
            elif not _hostname(value, wildcard=True):
                raise ValueError(f'{label}, line {line}: "{value}" must be a DNS hostname, optionally starting with *. (no URL, path or port).')
    for key, item in profile.get('endpoints', {}).items():
        host = item['host'].replace('{ENV}', 'environment').replace('{DOMAIN}', 'example.com')
        if not _hostname(host, wildcard=True):
            raise ValueError(f'Application endpoint {item["name"]}: enter a valid hostname; only {{ENV}}, {{DOMAIN}} and a leading *. wildcard are supported.')
    for group in ('endpoints', 'services', 'egress'):
        for key, item in profile.get(group, {}).items():
            if group != 'endpoints' and not _destination(item['destination']):
                raise ValueError(f'{group.title()} / {key}: destination must be an IP address, DNS name, valid service URL, or a supported template reference.')
            protocol = item['protocol']
            if protocol == 'NEEDS_CONFIRMATION':
                continue
            match = re.fullmatch(r'(?:(?:[A-Za-z][A-Za-z0-9 /-]*)[ /])?(TCP|UDP)\s+(\d{1,5})', protocol)
            if not match or not 1 <= int(match[2]) <= 65535:
                raise ValueError(f'{group.title()} / {key}: protocol must include TCP or UDP and a port from 1 to 65535 (for example HTTPS/TCP 443).')


def _destination(value):
    from urllib.parse import urlsplit
    references = {
        '{{ defaults.harbor_host }}', '{{ harbor }}', '{{ rancher }}', '{{ defaults.vault_url }}', '{{ defaults.ad_url }}',
        "{{ defaults.registry_destinations | join('\\n') }}",
        "{{ defaults.directory_destinations | join(', ') }}",
        'AWS public service endpoints', 'ECR endpoints in {{ region }}', 'GHCR and approved CI/CD endpoints',
    }
    if value in references:
        return True
    for destination in re.split(r'[,\n]', value):
        destination = destination.strip()
        try:
            ipaddress.ip_address(destination)
            continue
        except ValueError:
            pass
        if _hostname(destination, wildcard=True):
            continue
        try:
            parsed = urlsplit(destination)
            if parsed.scheme not in ('http', 'https', 'ldap', 'ldaps') or not parsed.hostname or parsed.username or parsed.password:
                return False
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                return False
            try:
                ipaddress.ip_address(parsed.hostname)
            except ValueError:
                if not _hostname(parsed.hostname):
                    return False
            if any(ch.isspace() for ch in destination):
                return False
        except ValueError:
            return False
    return True
