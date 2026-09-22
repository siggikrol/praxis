"""Versioned networking content. Files are read afresh; reviewed snapshots are separate.

Mappings merge recursively; lists and scalars replace. Named endpoint/rule mappings
allow architecture overrides without copying common lists. Conditions are data, not code.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

import yaml
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment


class NetworkingContentError(ValueError):
    pass


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise NetworkingContentError("YAML mapping keys must be unique strings.")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
_JINJA = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def content_root() -> Path:
    return Path(os.getenv("PS_NETWORKING_CONTENT_DIR") or Path(__file__).with_name("content"))


def fingerprint(value) -> str:
    # Mapping order affects endpoint/section output, so reordering is a revision too.
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _read(folder: str, name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise NetworkingContentError("Invalid networking content name.")
    from .template_admin import BUNDLES
    overlay = BUNDLES.get()
    if overlay is not None and f"{folder}/{name}" in overlay:
        return deepcopy(overlay[f"{folder}/{name}"])
    path = content_root() / folder / f"{name}.yaml"
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise NetworkingContentError(f"Cannot load networking content {path}: {exc}") from exc
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        raise NetworkingContentError(f"{path}: expected schema_version: 1 and a YAML mapping.")
    return value


def _merge(base, extra):
    result = deepcopy(base)
    for key, value in extra.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


def _condition(condition):
    if condition is None:
        return
    if not isinstance(condition, dict) or set(condition) != {"field", "equals"} or not isinstance(condition["field"], str):
        raise NetworkingContentError("Conditions must contain field and equals only.")
    if not isinstance(condition["equals"], (str, bool, int)):
        raise NetworkingContentError("Condition equals must be a scalar.")


def applies(item: dict, data: dict) -> bool:
    condition = item.get("condition")
    _condition(condition)
    return item.get('enabled', True) and (not condition or data.get(condition["field"]) == condition["equals"])


def _validate_profile(profile):
    allowed = {"schema_version", "extends", "platform", "defaults", "endpoints", "egress", "routing", "conditional", "vpcs", "allocation", "builder", "requires_confirmation", "presentation", "services", "attribution_defaults", "cross_vpc"}
    if set(profile) - allowed:
        raise NetworkingContentError(f"Unknown profile keys: {sorted(set(profile) - allowed)}")
    for group, required in {
        "endpoints": ("namespace", "name", "gateways", "host", "protocol"),
        "egress": ("destination", "protocol", "purpose"),
        "routing": ("requirement",), "conditional": ("requirement",),
        "vpcs": ("cidr_field",),
        "services": ("source", "destination", "protocol", "purpose", "attribution"),
    }.items():
        rows = profile.get(group, {})
        if not isinstance(rows, dict):
            raise NetworkingContentError(f"{group} must be a mapping keyed by stable identifier.")
        for key, row in rows.items():
            if not isinstance(row, dict) or any(not isinstance(row.get(field), str) or not row[field] for field in required):
                raise NetworkingContentError(f"{group}.{key} requires text fields: {', '.join(required)}")
            _condition(row.get("condition"))
            permitted = {
                'endpoints': {'namespace', 'name', 'gateways', 'host', 'protocol', 'exposure', 'condition'},
                'egress': {'destination', 'protocol', 'purpose', 'source', 'section', 'condition'},
                'routing': {'requirement', 'review_requirement', 'condition'}, 'conditional': {'requirement', 'condition'},
                'vpcs': {'cidr_field', 'requested_prefix', 'condition'},
                'services': {'source', 'destination', 'protocol', 'purpose', 'attribution', 'condition'},
            }[group]
            permitted |= {'enabled', 'category', 'attribution'}
            if set(row) - permitted:
                raise NetworkingContentError(f'Unknown {group}.{key} properties: {sorted(set(row) - permitted)}')
            if 'enabled' in row and type(row['enabled']) is not bool:
                raise NetworkingContentError(f'{group}.{key}.enabled must be boolean.')
            for field in set(row) - {'condition', 'requested_prefix', 'enabled'}:
                if not isinstance(row[field], str):
                    raise NetworkingContentError(f'{group}.{key}.{field} must be text.')
            prefix = row.get("requested_prefix")
            if prefix is not None and (type(prefix) is not int or not 0 <= prefix <= 32):
                raise NetworkingContentError(f"{group}.{key}.requested_prefix must be an IPv4 prefix length.")
    for key in ("defaults", "allocation", "builder"):
        if key in profile and not isinstance(profile[key], dict):
            raise NetworkingContentError(f"{key} must be a mapping.")
    if "requires_confirmation" in profile and (not isinstance(profile["requires_confirmation"], list) or not all(isinstance(x, str) for x in profile["requires_confirmation"])):
        raise NetworkingContentError("requires_confirmation must be a list of text.")
    if "platform" in profile and profile["platform"] not in {"catalyst", "rgs", "loyalty"}:
        raise NetworkingContentError("Unsupported platform.")
    defaults = profile.get('defaults', {})
    for key, value in defaults.items():
        if key in {'allow_ips', 'directory_destinations', 'registry_destinations'}:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise NetworkingContentError(f'defaults.{key} must be a list of strings.')
        elif not isinstance(value, str):
            raise NetworkingContentError(f'defaults.{key} must be text.')
    if not all(isinstance(v, str) for v in profile.get('allocation', {}).values()):
        raise NetworkingContentError('Allocation values must be text.')
    if 'cross_vpc' in profile:
        cross = profile['cross_vpc']
        if (not isinstance(cross, dict) or set(cross) - {'ilp_egress'} != {'host', 'protocol', 'port', 'tcp_ports'}
                or not isinstance(cross['host'], str) or not isinstance(cross['protocol'], str)
                or type(cross['port']) is not int or not 1 <= cross['port'] <= 65535
                or not isinstance(cross['tcp_ports'], list)
                or not all(type(port) is int and 1 <= port <= 65535 for port in cross['tcp_ports'])):
            raise NetworkingContentError('Cross-VPC access requires host, protocol, port and valid tcp_ports.')
        if 'ilp_egress' in cross:
            rule = cross['ilp_egress']
            if (not isinstance(rule, dict) or set(rule) not in ({'source', 'destination', 'protocol', 'port', 'attribution'}, {'source', 'destination', 'protocol', 'ports', 'attribution'})
                    or not all(isinstance(rule[key], str) and rule[key].strip() for key in ('source', 'destination', 'attribution'))
                    or rule['protocol'] not in {'TCP', 'UDP'}
                    or not isinstance(rule.get('ports', [rule.get('port')]), list)
                    or not rule.get('ports', [rule.get('port')])
                    or not all(type(port) is int and 1 <= port <= 65535 for port in rule.get('ports', [rule.get('port')]))):
                raise NetworkingContentError('ILP cross-VPC egress requires source, destination, protocol, valid port and attribution.')

    builder = profile.get('builder', {})
    for key in ('partner_choices', 'east_west_choices'):
        if key in builder and (not isinstance(builder[key], list) or not all(isinstance(row, list) and len(row) == 2 and all(isinstance(x, str) for x in row) for row in builder[key])):
            raise NetworkingContentError(f'builder.{key} must contain value/label pairs.')
    if 'egress_rules' in builder:
        rules = builder['egress_rules']
        labels = builder.get('labels', {})
        if not isinstance(rules, dict) or not isinstance(labels, dict):
            raise NetworkingContentError('Builder rules and labels must be mappings.')
        for rule in rules.values():
            if not isinstance(rule, dict) or rule.get('kind') not in {'flag', 'list', 'notes'} or not isinstance(rule.get('field'), str) or not isinstance(labels.get(rule['field']), str):
                raise NetworkingContentError('Builder rules require kind, field and corresponding label.')


def load_profile(name: str, _seen=()) -> dict:
    if name in _seen:
        raise NetworkingContentError("Networking profile inheritance cycle.")
    own = _read("profiles", name)
    parent = own.get("extends")
    if parent is not None and not isinstance(parent, str):
        raise NetworkingContentError("extends must name one profile.")
    base = load_profile(parent, (*_seen, name))["profile"] if parent else {}
    profile = _merge(base, {key: value for key, value in own.items() if key != "extends"})
    _validate_profile(profile)
    return {"profile": profile, "profile_hash": fingerprint(profile)}


def load_presentation(name="networking", _seen=()) -> dict:
    if name in _seen:
        raise NetworkingContentError('Networking presentation inheritance cycle.')
    own = _read("presentations", name)
    parent = own.get('extends')
    if parent is not None and not isinstance(parent, str):
        raise NetworkingContentError('Presentation extends must name one presentation.')
    base = load_presentation(parent, (*_seen, name))['definition'] if parent else {}
    definition = _merge(base, {k: v for k, v in own.items() if k not in {'extends', 'field_overrides'}})
    if parent and isinstance(own.get('sections'), dict):
        definition['sections'] = {**own['sections'], **{k: v for k, v in definition['sections'].items() if k not in own['sections']}}
    overrides = own.get('field_overrides', {})
    if not isinstance(overrides, dict) or any(not isinstance(v, dict) for v in overrides.values()):
        raise NetworkingContentError('Presentation field_overrides must map field keys to properties.')
    known_fields = {f.get('key') for f in definition.get('fields', []) if isinstance(f, dict)}
    if set(overrides) - known_fields:
        raise NetworkingContentError('Presentation overrides reference an unknown field.')
    if overrides:
        definition['fields'] = [_merge(f, overrides.get(f['key'], {})) for f in definition['fields']]
    fields = definition.get("fields")
    if not isinstance(fields, list):
        raise NetworkingContentError("Presentation fields must be a list.")
    seen = set()
    for field in fields:
        if not isinstance(field, dict) or any(not isinstance(field.get(k), str) for k in ("key", "label", "value", "type")):
            raise NetworkingContentError("Presentation fields require key, label, value and type strings.")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", field["key"]) or field["key"] in seen or field['key'] == 'request_content' or field["type"] not in {"text", "textarea", "url", "date"}:
            raise NetworkingContentError("Invalid or duplicate presentation field.")
        seen.add(field["key"])
        _condition(field.get("condition"))
        if set(field) - {'key', 'label', 'value', 'type', 'condition'}:
            raise NetworkingContentError('Unknown presentation field property.')
    for key in ("title", "description"):
        if not isinstance(definition.get(key), str):
            raise NetworkingContentError(f"Presentation {key} must be text.")
    if not isinstance(definition.get("sections"), dict) or not all(isinstance(v, str) for v in definition["sections"].values()):
        raise NetworkingContentError("Presentation sections must map names to Jinja text.")
    if name == 'networking' and 'manifest' not in definition['sections']:
        raise NetworkingContentError('Networking presentation requires a manifest section.')
    for value in [definition["title"], definition["description"], *definition["sections"].values(), *(f["value"] for f in fields)]:
        try:
            _JINJA.parse(value)
        except TemplateError as exc:
            raise NetworkingContentError(f"Invalid Jinja presentation: {exc}") from exc
    return {"definition": definition, "template_hash": fingerprint(definition)}


def render(text: str, context: dict) -> str:
    try:
        return _JINJA.from_string(text).render(**context)
    except TemplateError as exc:
        raise NetworkingContentError(f"Cannot render networking content: {exc}") from exc


def profile_name(data: dict) -> str:
    setup = str(data.get("setup_type") or "").lower()
    if setup in {"catalyst-single-vpc", "catalyst-multi-vpc", "rgs", "loyalty"}:
        return setup
    product = str(data.get("product") or "").lower()
    return product if product in {"rgs", "loyalty"} else "catalyst-single-vpc"
