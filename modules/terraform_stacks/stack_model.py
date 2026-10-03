"""Configuration model for deployable instances of released wrapper modules."""
import json
import math
import re
from urllib.parse import quote

from modules.terraform_module_builder import draft_wrappers
from modules.terraform_module_builder import store as draft_store


SEMVER = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
REPOSITORY = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
INPUT_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
RESERVED_INPUTS = {'source', 'version', 'providers', 'count', 'for_each', 'depends_on'}


def _version(value):
    match = SEMVER.fullmatch(str(value or ''))
    return tuple(map(int, match.groups())) if match else (-1, -1, -1)


def wrapper_releases(owner):
    """Return published deployment wrapper releases in semantic-version order."""
    result = [release for release in draft_store.releases(owner)
              if release.get('layer') == 'deployment-wrapper'
              and release.get('status') == 'published']
    return sorted(result, key=lambda release: (_version(release.get('version')),
                                                release.get('created', 0)), reverse=True)


def require_wrapper_release(owner, release_id):
    release = draft_store.get_release(owner, str(release_id or ''))
    if (not release or release.get('status') != 'published'
            or release.get('layer') != 'deployment-wrapper'):
        raise ValueError('Choose an exact published deployment wrapper version.')
    if not SEMVER.fullmatch(str(release.get('version') or '')):
        raise ValueError('The selected wrapper release has an invalid semantic version.')
    if (release.get('tag') != 'v' + release['version']
            or not REPOSITORY.fullmatch(str(release.get('repository') or ''))):
        raise ValueError('The selected wrapper release has invalid Git metadata.')
    if not re.fullmatch(r'[a-f0-9]{40}', str(release.get('commit_sha') or '')):
        raise ValueError('The selected wrapper release is missing its immutable Git commit.')
    files = release.get('source_metadata', {}).get('files')
    if not isinstance(files, dict):
        raise ValueError('The selected wrapper release is missing its source snapshot.')
    return release


def _variable_details(files):
    details = {}
    for _, document in draft_wrappers.root_documents(files):
        for entry in draft_wrappers.blocks(document.get('variable', [])):
            for name, spec in entry.items():
                if name in details:
                    raise ValueError(f'The wrapper declares variable {name} more than once.')
                details[name] = {
                    'sensitive': spec.get('sensitive') is True,
                    'nullable': spec.get('nullable') is not False,
                    'has_default': 'default' in spec,
                    'default_value': spec.get('default'),
                }
    return details


def interface(release):
    """Read a wrapper's immutable variable and output interface from its release snapshot."""
    files = release.get('source_metadata', {}).get('files', {})
    try:
        result = draft_wrappers.interface(files)
        details = _variable_details(files)
    except Exception as exc:
        raise ValueError('The released wrapper interface could not be inspected: ' + str(exc)) from exc
    seen = set()
    inputs = []
    for item in result['inputs']:
        name = item['name']
        if name in seen or not INPUT_NAME.fullmatch(name) or name in RESERVED_INPUTS:
            raise ValueError(f'The wrapper contains an unsupported input name: {name}.')
        seen.add(name)
        extra = details.get(name, {})
        inputs.append(dict(item,
                           sensitive=extra.get('sensitive', False),
                           nullable=extra.get('nullable', True),
                           has_default=extra.get('has_default', not item['required']),
                           default_value=extra.get('default_value')))
    outputs = []
    seen.clear()
    for item in result['outputs']:
        name = item['name']
        if name in seen or not INPUT_NAME.fullmatch(name):
            raise ValueError(f'The wrapper contains an unsupported output name: {name}.')
        seen.add(name)
        outputs.append(item)
    return {'inputs': inputs, 'outputs': outputs}


def _strip_expression(value):
    text = str(value or 'any').strip()
    if text.startswith('${') and text.endswith('}'):
        text = text[2:-1].strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                text = decoded
        except json.JSONDecodeError:
            pass
    return text


def _inside(text, name):
    prefix = name + '('
    if not text.startswith(prefix) or not text.endswith(')'):
        return None
    depth = 0
    quoted = False
    escaped = False
    for index, char in enumerate(text[len(name):], start=len(name)):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return None
            if depth < 0:
                return None
    return text[len(prefix):-1].strip() if depth == 0 and not quoted else None


def _split_top_level(text, delimiter=','):
    result, start, depth, quoted, escaped = [], 0, 0, False, False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
        elif char == delimiter and depth == 0:
            result.append(text[start:index].strip())
            start = index + 1
    result.append(text[start:].strip())
    return [part for part in result if part]


def _object_fields(text):
    text = text.strip()
    if not (text.startswith('{') and text.endswith('}')):
        raise ValueError('unsupported object type expression')
    result = {}
    for item in _split_top_level(text[1:-1]):
        depth = 0
        quoted = False
        split = None
        for index, char in enumerate(item):
            if char == '"':
                quoted = not quoted
            elif not quoted and char in '([{':
                depth += 1
            elif not quoted and char in ')]}':
                depth -= 1
            elif not quoted and depth == 0 and char in '=:':
                split = index
                break
        if split is None:
            raise ValueError('unsupported object type expression')
        raw_name, raw_type = item[:split].strip(), item[split + 1:].strip()
        if raw_name.startswith('"'):
            raw_name = json.loads(raw_name)
        result[str(raw_name)] = _strip_expression(raw_type)
    return result


def _type_error(path, expected):
    raise ValueError(f'{path} must match Terraform type {expected}.')


def validate_literal(value, type_expression, path='Value'):
    """Validate a JSON-compatible literal against common Terraform type constraints."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{path} must be a finite JSON value.') from exc
    text = _strip_expression(type_expression)
    if text == 'any':
        return
    if text == 'string':
        if not isinstance(value, str):
            _type_error(path, text)
        return
    if text == 'number':
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            _type_error(path, text)
        return
    if text == 'bool':
        if not isinstance(value, bool):
            _type_error(path, text)
        return
    optional = _inside(text, 'optional')
    if optional is not None:
        parts = _split_top_level(optional)
        return validate_literal(value, parts[0], path)
    for kind in ('list', 'set'):
        inner = _inside(text, kind)
        if inner is not None:
            if not isinstance(value, list):
                _type_error(path, text)
            for index, member in enumerate(value):
                validate_literal(member, inner, f'{path}[{index}]')
            return
    inner = _inside(text, 'map')
    if inner is not None:
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            _type_error(path, text)
        for key, member in value.items():
            validate_literal(member, inner, f'{path}.{key}')
        return
    inner = _inside(text, 'tuple')
    if inner is not None:
        types = _split_top_level(inner.strip()[1:-1]) if inner.strip().startswith('[') and inner.strip().endswith(']') else None
        if types is None or not isinstance(value, list) or len(value) != len(types):
            _type_error(path, text)
        for index, member in enumerate(value):
            validate_literal(member, types[index], f'{path}[{index}]')
        return
    inner = _inside(text, 'object')
    if inner is not None:
        fields = _object_fields(inner)
        if not isinstance(value, dict):
            _type_error(path, text)
        unknown = set(value) - set(fields)
        if unknown:
            raise ValueError(f'{path} contains unknown object attribute {sorted(unknown)[0]}.')
        for name, field_type in fields.items():
            is_optional = _inside(_strip_expression(field_type), 'optional') is not None
            if name not in value:
                if is_optional:
                    continue
                raise ValueError(f'{path} is missing object attribute {name}.')
            validate_literal(value[name], field_type, f'{path}.{name}')
        return
    raise ValueError(f'{path} uses unsupported Terraform type expression {text}.')


def _release_provider(release):
    provider = release.get('source_metadata', {}).get('source', {}).get('provider')
    return provider if provider in ('aws', 'google') else None


def _resolve_stack(owner, identifier, customer_id, environment_id, account_id, region):
    from . import store
    try:
        target = store.get(owner, identifier, 'stack')
    except ValueError:
        target = next((item for item in store.objects(owner, 'stack')
                       if item['name'] == identifier
                       and item['data'].get('customer') == customer_id
                       and item['data'].get('environment') == environment_id), None)
        if not target:
            raise ValueError(f'Referenced stack {identifier} was not found.')
    if (target['data'].get('customer') != customer_id
            or target['data'].get('environment') != environment_id):
        raise ValueError('Stack output references must stay within the same customer and environment.')
    if (target['data'].get('account') != account_id
            or target['data'].get('region') != region):
        raise ValueError('Stack output references must use the same cloud account and region.')
    if not target['data'].get('wrapper_release_id'):
        raise ValueError('Stack output references require a Stack backed by a released wrapper.')
    return target


def _dependencies(owner, data, inputs, stack_id=None):
    dependencies = []
    for name, binding in inputs.items():
        if binding['kind'] != 'stack_output':
            continue
        target = _resolve_stack(owner, binding['stack_id'], data['customer'], data['environment'],
                                data['account'], data['region'])
        if stack_id and target['id'] == stack_id:
            raise ValueError('A Stack cannot reference its own output.')
        release = require_wrapper_release(owner, target['data']['wrapper_release_id'])
        outputs = {item['name'] for item in interface(release)['outputs']}
        if binding['output'] not in outputs:
            raise ValueError(f'Stack {target["name"]} does not declare output {binding["output"]}.')
        binding['stack_id'] = target['id']
        if target['id'] not in dependencies:
            dependencies.append(target['id'])
    return dependencies


def _check_cycles(owner, environment_id, stack_id, candidate_dependencies):
    if not stack_id:
        return
    from . import store
    graph = {}
    for item in store.objects(owner, 'stack'):
        if item['data'].get('environment') == environment_id and item['data'].get('wrapper_release_id'):
            graph[item['id']] = list(item['data'].get('dependencies', []))
    graph[stack_id] = list(candidate_dependencies)
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError('Stack output references create a dependency cycle.')
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    visit(stack_id)


def _validate_consumers(owner, stack_id, data, outputs):
    if not stack_id:
        return
    from . import store
    for consumer in store.objects(owner, 'stack'):
        if consumer['id'] == stack_id or not consumer['data'].get('wrapper_release_id'):
            continue
        for binding in consumer['data'].get('inputs', {}).values():
            if (binding.get('kind') != 'stack_output'
                    or binding.get('stack_id') != stack_id):
                continue
            if any(consumer['data'].get(field) != data.get(field)
                   for field in ('customer', 'environment', 'account', 'region')):
                raise ValueError(f'Stack {consumer["name"]} depends on this Stack in its current context.')
            if binding.get('output') not in outputs:
                raise ValueError(f'Stack {consumer["name"]} depends on output {binding.get("output")}, which the selected wrapper does not declare.')


def prepare(owner, data, stack_id=None):
    """Validate and normalize a released-wrapper Stack before persistence."""
    from . import store
    customer = store.get(owner, data.get('customer'), 'customer')
    environment = store.get(owner, data.get('environment'), 'environment')
    account = store.get(owner, data.get('account'), 'account')
    if environment['data'].get('customer') != customer['id']:
        raise ValueError('The environment belongs to a different customer.')
    if account['data'].get('customer') != customer['id']:
        raise ValueError('The cloud account belongs to a different customer.')
    if account['data'].get('environment') != environment['id']:
        raise ValueError('The cloud account belongs to a different environment.')

    release_id = data.get('wrapper_release_id') or data.get('wrapper_release')
    release = require_wrapper_release(owner, release_id)
    provider = account['data'].get('provider')
    release_provider = _release_provider(release)
    if release_provider and provider != release_provider:
        raise ValueError('The wrapper provider does not match the selected cloud account.')
    region = str(data.get('region') or '').strip()
    if provider == 'aws' and not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', region):
        raise ValueError('Enter an AWS region, for example eu-central-1.')
    if provider == 'google' and not re.fullmatch(r'[a-z]+(?:-[a-z0-9]+)+', region):
        raise ValueError('Enter a Google Cloud region, for example europe-west1.')

    schema = interface(release)
    variables = {item['name']: item for item in schema['inputs']}
    raw_inputs = data.get('inputs')
    if not isinstance(raw_inputs, dict):
        raise ValueError('Stack inputs must be an object.')
    unknown = set(raw_inputs) - set(variables)
    if unknown:
        raise ValueError(f'The wrapper does not declare input {sorted(unknown)[0]}.')
    normalized = {}
    for name, variable in variables.items():
        if name not in raw_inputs:
            continue
        raw = raw_inputs[name]
        if isinstance(raw, dict) and raw.get('kind') in ('literal', 'stack_output'):
            binding = dict(raw)
        elif isinstance(raw, dict) and 'from_stack' in raw and 'output' in raw:
            binding = {'kind': 'stack_output', 'stack_id': raw['from_stack'], 'output': raw['output']}
        else:
            binding = {'kind': 'literal', 'value': raw}
        if binding['kind'] == 'literal':
            value = binding.get('value')
            if value is None and not variable['nullable']:
                raise ValueError(f'Input {name} cannot be null.')
            validate_literal(value, variable['type'], f'Input {name}')
            normalized[name] = {'kind': 'literal', 'value': value}
        else:
            stack_ref = str(binding.get('stack_id') or '').strip()
            output = str(binding.get('output') or '').strip()
            if not stack_ref or not INPUT_NAME.fullmatch(output):
                raise ValueError(f'Input {name} must select a Stack output.')
            normalized[name] = {'kind': 'stack_output', 'stack_id': stack_ref, 'output': output}
    missing = [item['name'] for item in schema['inputs']
               if item['required'] and item['name'] not in normalized]
    if missing:
        raise ValueError('Required wrapper input is missing: ' + missing[0] + '.')
    dependencies = _dependencies(owner, data, normalized, stack_id)
    _check_cycles(owner, environment['id'], stack_id, dependencies)
    _validate_consumers(owner, stack_id, data, {item['name'] for item in schema['outputs']})

    metadata = release.get('source_metadata', {})
    data.update({
        'wrapper_release_id': release['id'],
        'wrapper_draft_id': release['draft_id'],
        'wrapper_name': metadata.get('draft_name') or release['repository'].split('/')[-1],
        'wrapper_version': release['version'],
        'wrapper_tag': release['tag'],
        'wrapper_repository': release['repository'],
        'wrapper_commit': release['commit_sha'],
        'inputs': normalized,
        'dependencies': dependencies,
    })
    data.pop('wrapper_release', None)
    return data


def release_status(owner, stack):
    release = require_wrapper_release(owner, stack['data']['wrapper_release_id'])
    candidates = [item for item in wrapper_releases(owner)
                  if item['draft_id'] == release['draft_id']]
    latest = max(candidates, key=lambda item: _version(item['version']), default=release)
    available = latest['version'] if _version(latest['version']) > _version(release['version']) else None
    return {'current': release['version'], 'available': available,
            'update_available': available is not None, 'latest_release_id': latest['id']}


def source_address(release):
    return (f'git::https://github.com/{release["repository"]}.git'
            f'?ref={quote(release["commit_sha"], safe="")}')


def generated_deployment(owner, stack):
    """Build the configuration-only artifact intended for a later OpenTofu runner."""
    from . import store
    data = stack['data']
    customer = store.get(owner, data['customer'], 'customer')
    environment = store.get(owner, data['environment'], 'environment')
    account = store.get(owner, data['account'], 'account')
    release = require_wrapper_release(owner, data['wrapper_release_id'])
    schema = interface(release)
    inputs = {}
    dependencies = []
    for name, binding in data['inputs'].items():
        if binding['kind'] == 'literal':
            inputs[name] = {'literal': binding['value']}
            continue
        target = store.get(owner, binding['stack_id'], 'stack')
        inputs[name] = {'stack_output': {'stack_id': target['id'], 'stack': target['name'],
                                         'output': binding['output']}}
        if target['id'] not in [item['stack_id'] for item in dependencies]:
            dependencies.append({'stack_id': target['id'], 'stack': target['name']})
    return {
        'schema_version': 2,
        'kind': 'praxis-stack-deployment',
        'stack': {'id': stack['id'], 'name': stack['name'], 'revision': stack['revision']},
        'context': {'customer': customer['name'], 'environment': environment['name']},
        'target': {'cloud': account['data']['provider'], 'account': account['name'],
                   'account_id': account['data'].get('account_id', ''), 'region': data['region']},
        'wrapper': {
            'release_id': release['id'], 'name': data['wrapper_name'],
            'draft_id': release['draft_id'], 'draft_revision': release['draft_revision'],
            'version': release['version'], 'tag': release['tag'],
            'repository': release['repository'], 'commit': release['commit_sha'],
            'source': source_address(release),
        },
        'inputs': inputs,
        'dependencies': dependencies,
        'declared_outputs': [item['name'] for item in schema['outputs']],
    }


def parse_form_inputs(form, variables):
    result = {}
    for variable in variables:
        name = variable['name']
        kind = str(form.get(f'input_{name}_kind') or '').strip()
        if not kind:
            continue
        if kind == 'literal':
            raw = str(form.get(f'input_{name}_literal') or '').strip()
            if not raw:
                raise ValueError(f'Enter a JSON literal for input {name}.')
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f'Input {name} must be valid JSON: {exc.msg}.') from exc
            result[name] = {'kind': 'literal', 'value': value}
        elif kind == 'stack_output':
            reference = str(form.get(f'input_{name}_reference') or '')
            if '::' not in reference:
                raise ValueError(f'Select a Stack output for input {name}.')
            stack_id, output = reference.split('::', 1)
            result[name] = {'kind': 'stack_output', 'stack_id': stack_id, 'output': output}
        else:
            raise ValueError(f'Choose a valid source for input {name}.')
    return result
