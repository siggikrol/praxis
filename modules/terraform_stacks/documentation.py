"""Read catalog documentation from a selected, immutable Git commit."""
import json
import re
from markdown_it import MarkdownIt
from markupsafe import Markup
import hcl2
from . import github


def markdown(text):
    return Markup(MarkdownIt('commonmark', {'html': False}).enable('table').disable('image').render(text))


def entries(value):
    return [value] if isinstance(value, dict) else value


def describe(files):
    inputs, outputs, warnings = [], [], []
    for name, content in files.items():
        if not name.endswith(('.tf', '.tf.json', '.tofu', '.tofu.json')):
            continue
        try:
            doc = json.loads(content) if name.endswith('.json') else hcl2.loads(content)
            for entry in entries(doc.get('variable', [])):
                for key, spec in entry.items():
                    inputs.append({'name': key, 'type': str(spec.get('type', 'any')).removeprefix('${').removesuffix('}'),
                                   'required': 'default' not in spec, 'default': '[sensitive]' if spec.get('sensitive') else json.dumps(spec.get('default')),
                                   'description': spec.get('description', ''), 'sensitive': bool(spec.get('sensitive'))})
            for entry in entries(doc.get('output', [])):
                for key, spec in entry.items():
                    outputs.append({'name': key, 'description': spec.get('description', ''), 'sensitive': bool(spec.get('sensitive'))})
        except Exception:
            warnings.append(f'{name}: could not read Terraform documentation; see the source file.')
    return sorted(inputs, key=lambda v: (not v['required'], v['name'])), sorted(outputs, key=lambda v: v['name']), warnings


def load(item, selected=''):
    repo = item['data']['repository']
    versions = [v for v in github.versions(repo) if re.fullmatch(r'v?\d+\.\d+\.\d+', v['tag'])]
    versions.sort(key=lambda v: tuple(map(int, v['tag'].lstrip('v').split('.'))), reverse=True)
    result = dict(versions=versions, selected=None, inputs=[], outputs=[], warnings=[], readme='', changelog='', usage='')
    if not versions:
        return result
    version = next((v for v in versions if v['tag'] == selected), None) if selected else versions[0]
    if not version:
        raise ValueError('The selected version is not an available release tag.')
    sha = version['commit']
    if not re.fullmatch(r'[0-9a-fA-F]{40}', sha):
        raise ValueError('GitHub returned an invalid version commit.')
    tree = github.request('GET', github.repo_path(repo) + '/git/trees/' + sha)
    paths = [p for p in tree.get('tree', []) if p.get('type') == 'blob' and (p['path'].lower() in ('readme.md', 'changelog.md') or p['path'].endswith(('.tf','.tf.json','.tofu','.tofu.json')))]
    if tree.get('truncated') or len(paths) > 40 or sum(p.get('size', 0) for p in paths) > 2_000_000:
        raise ValueError('Module documentation exceeds the supported size. Read it in GitHub.')
    files = {p['path']: github.contents(repo, p['path'], sha) for p in paths}
    inputs, outputs, warnings = describe(files)
    lines = ['module "' + item['name'].replace('-', '_') + '" {', '  source = "git::https://github.com/' + repo + '.git?ref=' + sha + '"', '']
    for variable in inputs:
        if variable['required'] and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', variable['name']):
            lines.append('  ' + variable['name'] + ' = var.' + variable['name'])
    lines.append('}')
    result.update(selected=version, inputs=inputs, outputs=outputs, warnings=warnings, usage='\n'.join(lines),
                  readme=markdown(next((v for k,v in files.items() if k.lower()=='readme.md'), 'No README provided for this version.')),
                  changelog=markdown(next((v for k,v in files.items() if k.lower()=='changelog.md'), 'No changelog provided for this version.')))
    return result
