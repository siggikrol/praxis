from __future__ import annotations

import os
from constants import RANCHER_CLUSTER_TO_URL
from typing import Any

from modules.ncr.networking import NetworkingContentError, applies, fingerprint, load_presentation, load_profile, profile_name, render


def build_connectivity_manifest(data: dict[str, Any], *, include_details: bool = False, content_bundle=None) -> dict[str, Any]:
    from modules.ncr.template_admin import content_overlay, published_bundle
    bundle = content_bundle if content_bundle is not None else published_bundle(profile_name(data))
    with content_overlay(bundle):
        return _build_connectivity_manifest(data, include_details=include_details)


def _build_connectivity_manifest(data: dict[str, Any], *, include_details: bool = False) -> dict[str, Any]:
    """Resolve technical data once, then render the external presentation."""
    loaded = load_profile(profile_name(data))
    profile = loaded['profile']
    for group in ('defaults', 'platform', 'vpcs', 'allocation', 'endpoints', 'egress'):
        if group not in profile:
            raise NetworkingContentError(f'Resolved networking profile requires {group}.')
    if not {'harbor_host', 'allow_ips', 'ad_url', 'vault_url'} <= set(profile['defaults']) or 'source_placeholder' not in profile['allocation']:
        raise NetworkingContentError('Resolved networking defaults or allocation placeholder are incomplete.')
    presentation = load_presentation(profile.get('presentation', 'networking'))
    defaults = dict(profile['defaults'])
    rancher_url = RANCHER_CLUSTER_TO_URL.get(str(data.get('rancher_target') or '').strip(), '')
    # Resolve even historical published BDE presentations through the selected target.
    if rancher_url and 'bde_cidr' in defaults:
        defaults['bde_rancher_ingress_host'] = rancher_url
        defaults['bde_rancher_egress_host'] = rancher_url
    def text(key, fallback=''):
        return str(data.get(key) or fallback).strip()
    environment = text('environment_name', '<environment>')
    vpcs = [dict(item, name=name.upper(), requested_prefix=item.get('requested_prefix', text('requested_network_size')),
                 allocated_cidr=text(item['cidr_field']))
            for name, item in profile['vpcs'].items() if applies(item, data)]
    context = {
        'data': data, 'profile': profile, 'defaults': defaults,
        'environment': environment, 'manifest_environment': environment,
        'domain': text('dns_domain', '<base-domain>'), 'region': text('aws_region', '<aws-region>'),
        'rancher': text('rancher_target', '<selected Rancher target>'),
        'rancher_url': rancher_url,
        'harbor': defaults['harbor_host'] if profile['platform'] == 'catalyst' else os.getenv('PS_HARBOR_REGISTRY_HOST', defaults['harbor_host']).strip(),
        'platform': profile['platform'], 'vpcs': vpcs,
        'subdomain': text('subdomain', environment),
        'root_domain': text('root_domain', text('dns_domain', '<root-domain>')),
        'include_details': include_details,
    }
    prefix = text('requested_network_size')
    context['requested_prefix'] = '/' + prefix.lstrip('/') if prefix.lstrip('/').isdigit() and 0 <= int(prefix.lstrip('/')) <= 32 else (prefix or 'NEEDS_CONFIRMATION')
    source = ', '.join(vpc['allocated_cidr'] for vpc in vpcs if vpc['allocated_cidr']) or profile['allocation']['source_placeholder']
    endpoints = []
    for item in profile['endpoints'].values():
        if not applies(item, data):
            continue
        host = render(item['host'].replace('{ENV}', '{{ subdomain }}').replace('{DOMAIN}', '{{ root_domain }}'), context)
        endpoints.append({
            'namespace': item['namespace'], 'name': item['name'], 'host': host,
            'exposure': item.get('exposure') or ('Internal' if '.int.' in host else 'External'),
            'protocol': render(item['protocol'], context),
            'category': item.get('category', 'Additional endpoints'),
            **{key: item[key] for key in ('category', 'attribution') if key in item},
        })
    context['source'] = source
    service_dns = [{k: v for k, v in item.items() if k != 'protocol'}
                   for item in endpoints if item.get('category') == 'service_dns']
    endpoints = [item for item in endpoints if item.get('category') != 'service_dns']
    egress = [{'source': render(item.get('source', '{{ source }}'), context), **{key: render(item[key], context) for key in ('destination', 'protocol', 'purpose')}}
              for item in profile['egress'].values() if applies(item, data)]
    context.update(endpoints=endpoints, egress=egress, service_dns=service_dns)
    for result, item in zip(egress, (item for item in profile['egress'].values() if applies(item, data))):
        if 'attribution' in item:
            result['attribution'] = item['attribution']
    context['services'] = [{key: render(item[key], context) for key in ('source', 'destination', 'protocol', 'purpose', 'attribution')}
                           for item in profile.get('services', {}).values() if applies(item, data)]
    context['cross_vpc'] = ({**profile['cross_vpc'], 'host': render(profile['cross_vpc']['host'], context)}
                            if profile.get('cross_vpc') else None)
    for group in ('routing', 'conditional'):
        context[group] = [{**item, 'requirement': render(item['requirement'], context)}
                          for item in profile.get(group, {}).values() if applies(item, data)]
    sections = {}
    context['sections'] = sections
    for name, template in presentation['definition']['sections'].items():
        if name != 'manifest':
            sections[name] = render(template, context)
    sections['manifest'] = render(presentation['definition']['sections']['manifest'], context).rstrip('\n')
    return {
        'platform': profile['platform'], 'environment': environment,
        'endpoints': endpoints, 'egress': egress, 'allow_ips': list(defaults['allow_ips']),
        'text': sections['manifest'], 'source': 'Praxis NCR canonical profile',
        'sections': sections, 'context': context, 'presentation': presentation['definition'],
        'profile_hash': fingerprint({'profile': profile, 'harbor': context['harbor'], 'rancher_url': rancher_url}), 'template_hash': presentation['template_hash'],
    }
