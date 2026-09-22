"""Readiness milestones, dependency tracking, and actionable blockers."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .requirements import (
    ALLOCATED_FIELDS, CATALYST_TECHNICAL_FIELDS, CONDITIONAL_REQUIREMENTS,
    IDENTITY_FIELDS, MULTI_VPC_CIDR_FIELDS, TECHNICAL_FIELDS, all_requirements,
    validate_readiness,
)

from .camunda import FIELDS as CAMUNDA_FIELDS

COMMON_INPUTS = {'customer', 'environment_name', 'setup_type', 'aws_region',
                 'aws_account_id', 'environment_type', 'production_classification',
                 'component_profile'}
VERIFICATION_INPUTS = {
    'networking': COMMON_INPUTS | {'requested_network_size', 'dns_domain', 'rancher_target',
                                  'firewall_zone', 'vpc_cidr'} | {key for key, _ in MULTI_VPC_CIDR_FIELDS},
    'aws_platform': COMMON_INPUTS | {key for key, _ in TECHNICAL_FIELDS + CATALYST_TECHNICAL_FIELDS + CAMUNDA_FIELDS},
}
for item in CONDITIONAL_REQUIREMENTS:
    VERIFICATION_INPUTS[item.get('group', 'aws_platform')].add(item['flag'])
FIELD_LABELS = dict((*IDENTITY_FIELDS, *TECHNICAL_FIELDS, *CATALYST_TECHNICAL_FIELDS,
                     *ALLOCATED_FIELDS, *MULTI_VPC_CIDR_FIELDS))
FIELD_LABELS.update(dict(CAMUNDA_FIELDS))
FIELD_LABELS.update({item['flag']: item['label'] + ' applicability' for item in CONDITIONAL_REQUIREMENTS})


def invalidate_dependencies(data: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Invalidate only affected verification; keep prior evidence available for review."""
    data = deepcopy(data)
    if not previous:
        return data
    changed = {key for fields in VERIFICATION_INPUTS.values() for key in fields
               if str(data.get(key) or '') != str(previous.get(key) or '')}
    impacts = list(previous.get('_change_impacts') or [])
    for group, fields in VERIFICATION_INPUTS.items():
        affected = sorted(changed & fields)
        if not affected:
            continue
        for suffix in ('attested', 'attested_by', 'attested_at'):
            data[f'{group}_{suffix}'] = ''
        for item in all_requirements(data):
            if item['group'] != group:
                continue
            row = data.get('requirements', {}).get(item['key'], {})
            if row.get('status') == 'confirmed':
                row['status'] = 'requested'
            row['verified_by'] = ''
            row['verified_at'] = ''
        label = 'Networking' if group == 'networking' else 'AWS and Platform'
        message = f"{label}: re-verify after changes to {', '.join(FIELD_LABELS.get(k, k) for k in affected)}."
        if message not in impacts:
            impacts.append(message)
    if changed:
        for template in data.get('servicenow_templates', {}).values():
            template['needs_review'] = True
            template.pop('review_acknowledged_by', None)
            template.pop('review_acknowledged_at', None)
        message = 'Review existing ServiceNow requests and saved templates; publish a revision before refreshing readiness values in a linked provisioning wizard.'
        if message not in impacts:
            impacts.append(message)
    data['_change_impacts'] = impacts[-20:]
    return data


def readiness_summary(data: dict[str, Any]) -> dict[str, Any]:
    request_errors = validate_readiness(data, request_only=True)
    provision_errors = validate_readiness(data)
    groups = []
    for group, label, step, ticket in (
        ('networking', 'Waiting on Networking', 5, 'ncr_ticket'),
        ('aws_platform', 'Waiting on AWS and Platform', 6, 'aws_service_request'),
    ):
        rows = []
        for item in all_requirements(data):
            if item['group'] != group:
                continue
            values = data.get('requirements', {}).get(item['key'], {})
            missing = []
            for key, action in [('owner', 'Assign an owner'), ('evidence', 'Record a ticket or evidence link')]:
                if not str(values.get(key) or '').strip():
                    missing.append(action)
            if values.get('status') != 'confirmed':
                missing.append('Resolve the blocker and verify' if values.get('status') == 'blocked' else 'Verify the prerequisite')
            if missing:
                evidence = str(values.get('evidence') or data.get(ticket) or '')
                url = evidence if evidence.lower().startswith(('https://', 'http://')) else str(data.get(ticket + '_url') or '')
                rows.append(dict(label=item['label'], owner=values.get('owner') or 'Unassigned',
                                 evidence=evidence, url=url if url.lower().startswith(('https://', 'http://')) else '',
                                 action='; '.join(missing), notes=values.get('notes') or ''))
        if data.get(f'{group}_attested') != 'yes':
            rows.append(dict(label=f'{label.removeprefix("Waiting on ")} attestation', owner='Responsible verifier',
                             evidence='', url='', action='Confirm the section after every applicable check passes', notes=''))
        groups.append(dict(label=label, step=step, rows=rows))
    return dict(request_errors=request_errors, provision_errors=provision_errors, groups=groups,
                ready_for_requests=not request_errors, ready_for_devops=not provision_errors)
