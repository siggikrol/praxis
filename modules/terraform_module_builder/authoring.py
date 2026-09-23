"""Generate thin wrappers; validate syntax without executing Terraform."""
import json
import re
from pathlib import PurePosixPath

import hcl2

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def literal(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace('${', '$${').replace('%{', '%%{')


def expression(value):
    # Reject metadata that escapes its single attribute into additional HCL blocks.
    parsed = hcl2.loads('value = ' + value + '\n')
    if set(parsed) != {'value'}:
        raise ValueError('Invalid upstream type or default expression.')
    return value


def validate_files(files):
    if len(files) > 200 or sum(len(v.encode()) for v in files.values()) > 2_000_000:
        raise ValueError('A draft supports up to 200 text files and 2 MB total.')
    for name, content in files.items():
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        files[name] = content
        if name in {'praxis-source.json', 'PRAXIS-IMPORT-NOTES.txt'}:
            raise ValueError('An imported file conflicts with a reserved Praxis metadata filename.')
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or not name or '\x00' in name:
            raise ValueError('Invalid file path.')
        try:
            if name.endswith('.tf'):
                hcl2.loads(content)
            elif name.endswith('.tf.json'):
                json.loads(content)
        except Exception as exc:
            raise ValueError(f'{name}: Terraform syntax could not be parsed. Check this file before saving.') from exc


def wrapper(module, name, choices, outputs):
    if not NAME.fullmatch(name) or len(name) > 80:
        raise ValueError('Use a name starting with a letter or underscore, followed by letters, numbers, underscores or hyphens (up to 80).')
    arguments, variables = [], []
    for item in module['root'].get('inputs', []):
        key = item['name']
        if not NAME.fullmatch(key):
            raise ValueError('The upstream module contains an unsupported input name.')
        mode, value = choices.get(key, ('expose' if item.get('required') else 'default', ''))
        if mode == 'default':
            if item.get('required'):
                raise ValueError(f'{key} is required: expose it or provide a fixed value.')
            continue
        if mode not in ('fixed', 'expose'):
            raise ValueError(f'{key}: choose a valid input mode.')
        supplied = None
        if value.strip():
            try:
                supplied = literal(json.loads(value))
            except (ValueError, TypeError) as exc:
                raise ValueError(f'{key}: enter valid JSON, such as "text", true, 3, [], or {{}}.') from exc
        if mode == 'fixed':
            if supplied is None:
                raise ValueError(f'{key}: a fixed value is required.')
            arguments.append(f'  {key} = {supplied}')
        else:
            lines = [f'variable "{key}" {{', f'  description = {literal(item.get("description", ""))}',
                     f'  type = {expression(item.get("type") or "any")}']
            default = supplied if supplied is not None else (item.get('default') if not item.get('required') else None)
            if default is not None and str(default).strip():
                lines.append(f'  default = {expression(str(default))}')
            lines.append('}')
            variables.append('\n'.join(lines))
            arguments.append(f'  {key} = var.{key}')
    providers, aliases = [], []
    for dep in module['root'].get('provider_dependencies', []):
        key = dep['name']
        if not NAME.fullmatch(key):
            raise ValueError('Unsupported provider name.')
        lines = [f'    {key} = {{', f'      source = {literal(dep["source"])}']
        if dep.get('version'):
            lines.append(f'      version = {literal(dep["version"])}')
        names = dep.get('aliases') or []
        for alias in names:
            if not re.fullmatch(re.escape(key) + r'\.[A-Za-z_][\w-]*', alias):
                raise ValueError('This module has unsupported provider aliases. Start from an example instead.')
        if names:
            lines.append('      configuration_aliases = [' + ', '.join(names) + ']')
            aliases.extend(names)
        providers.append('\n'.join(lines + ['    }']))
    if aliases:
        arguments.append('  providers = {\n' + '\n'.join(f'    {a} = {a}' for a in aliases) + '\n  }')
    selected = []
    known_outputs = {item['name'] for item in module['root'].get('outputs', [])}
    if set(outputs) - known_outputs:
        raise ValueError('Choose outputs from the selected module.')
    for item in module['root'].get('outputs', []):
        if item['name'] in outputs:
            key = item['name']
            if not NAME.fullmatch(key):
                raise ValueError('Unsupported output name.')
            selected.append(f'output "{key}" {{\n  description = {literal(item.get("description", ""))}\n  value = module.upstream.{key}\n  sensitive = true\n}}')
    files = {
        'main.tf': f'module "upstream" {{\n  source = {literal(module["address"])}\n  version = {literal(module["version"])}\n' + '\n'.join(arguments) + '\n}\n',
        'variables.tf': '\n\n'.join(variables) + '\n',
        'outputs.tf': '\n\n'.join(selected) + '\n',
        'versions.tf': 'terraform {\n  required_providers {\n' + '\n'.join(providers) + '\n  }\n}\n',
        'README.md': f'# {name}\n\nA local wrapper for `{module["address"]}` version `{module["version"]}`.\n\nSource: {module["registry_url"]}\n\nConfigure providers in the calling project. Upstream Terraform version requirements still apply.\nInputs omitted here retain upstream defaults. Exposed required inputs must be supplied by the caller.\nOutputs are conservatively marked sensitive; review this setting before changing it.\n\nPraxis checks HCL syntax only; it does not execute Terraform or verify deployability.\n',
    }
    validate_files(files)
    return files
