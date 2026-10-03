"""Inspect a saved module and build a deployment wrapper around its public interface."""
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

import hcl2

from . import authoring, registry

REPOSITORY = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
RELEASE_TAG = re.compile(r'^v(\d+\.\d+\.\d+)$')
COMMIT_SHA = re.compile(r'^[0-9a-fA-F]{40}$')


def blocks(value):
    return [value] if isinstance(value, dict) else (value or [])


def root_documents(files):
    result=[]
    for name,content in files.items():
        path=PurePosixPath(name)
        if path.parent != PurePosixPath('.') or not name.endswith(('.tf','.tf.json','.tofu','.tofu.json')):
            continue
        document=json.loads(content) if name.endswith(('.tf.json','.tofu.json')) else hcl2.loads(content)
        result.append((name,document))
    return result


def expression(value):
    if isinstance(value,str) and value.startswith('${') and value.endswith('}'):
        return value[2:-1]
    return json.dumps(value,ensure_ascii=False,allow_nan=False)


def type_expression(value):
    if value is None:
        return 'any'
    text=str(value)
    if text.startswith('${') and text.endswith('}'):
        return text[2:-1]
    if text in ('string','number','bool','any'):
        return text
    return text


def interface(files):
    inputs={}
    outputs={}
    dependencies=[]
    aliases=[]
    for filename,document in root_documents(files):
        for entry in blocks(document.get('variable',[])):
            for name,spec in entry.items():
                inputs[name]={
                    'name':name,
                    'type':type_expression(spec.get('type')),
                    'description':str(spec.get('description') or ''),
                    'required':'default' not in spec,
                    'default':expression(spec['default']) if 'default' in spec else None,
                }
        for entry in blocks(document.get('output',[])):
            for name,spec in entry.items():
                outputs[name]={
                    'name':name,
                    'description':str(spec.get('description') or ''),
                    'sensitive':spec.get('sensitive') is True,
                }
        for entry in blocks(document.get('module',[])):
            for name,spec in entry.items():
                source=spec.get('source') if isinstance(spec,dict) else None
                if isinstance(source,str) and '${' not in source:
                    version=spec.get('version')
                    tag_match=re.search(r'[?&]ref=([^&]+)',source)
                    tag=tag_match.group(1) if tag_match else ''
                    release_match=RELEASE_TAG.fullmatch(tag)
                    dependencies.append({'name':name,'source':source,
                        'version':version or (release_match.group(1) if release_match else None),
                        'release_tag':tag if release_match else '', 'file':filename})
        for terraform in blocks(document.get('terraform',[])):
            for required in blocks(terraform.get('required_providers',[])):
                for provider,spec in required.items():
                    if not isinstance(spec,dict):
                        continue
                    for alias in spec.get('configuration_aliases',[]) or []:
                        address=str(alias).removeprefix('${').removesuffix('}')
                        if address not in aliases:
                            aliases.append(address)
    return {'inputs':list(inputs.values()),'outputs':list(outputs.values()),
            'dependencies':dependencies,'provider_aliases':aliases}


def raw_blocks(files,kinds):
    found=[]
    for name,content in files.items():
        path=PurePosixPath(name)
        if path.parent != PurePosixPath('.') or not name.endswith(('.tf','.tofu')):
            continue
        tree=hcl2.parses(content)
        for block in tree.find_data('block'):
            kind=str(block.children[0].children[0])
            if kind in kinds:
                found.append((name,block.meta.start_pos,content[block.meta.start_pos:block.meta.end_pos].strip()))
    return [text for _,_,text in sorted(found)]


def update_module_version(files,module_name,version):
    if not authoring.NAME.fullmatch(str(module_name or '')):
        raise ValueError('Choose a valid module dependency.')
    try:
        version=registry.version_number(str(version or '').strip())
    except registry.RegistryError as exc:
        raise ValueError('Enter an exact published version such as 1.3.3.') from exc
    updated=dict(files)
    matches=0
    for name,content in files.items():
        path=PurePosixPath(name)
        if path.parent != PurePosixPath('.'):
            continue
        if name.endswith(('.tf.json','.tofu.json')):
            document=json.loads(content)
            modules=document.get('module',{})
            if module_name in modules:
                modules[module_name]['version']=version
                updated[name]=json.dumps(document,indent=2,ensure_ascii=False)+'\n'
                matches += 1
            continue
        if not name.endswith(('.tf','.tofu')):
            continue
        tree=hcl2.parses(content)
        replacements=[]
        for block in tree.find_data('block'):
            if str(block.children[0].children[0]) != 'module':
                continue
            raw=content[block.meta.start_pos:block.meta.end_pos]
            label=re.match(r'\s*module\s+"([A-Za-z_][A-Za-z0-9_-]*)"',raw)
            if not label or label.group(1) != module_name:
                continue
            matches += 1
            body=next(child for child in block.children if getattr(child,'data',None) == 'body')
            attributes={str(child.children[0].children[0]):child for child in body.children
                        if getattr(child,'data',None) == 'attribute'}
            if 'version' in attributes:
                attribute=attributes['version']
                replacements.append((attribute.meta.start_pos,attribute.meta.end_pos,
                                     f'version = {authoring.literal(version)}'))
            elif 'source' in attributes:
                attribute=attributes['source']
                line_start=content.rfind('\n',0,attribute.meta.start_pos)+1
                indent=content[line_start:attribute.meta.start_pos]
                replacements.append((attribute.meta.end_pos,attribute.meta.end_pos,
                                     f'\n{indent}version = {authoring.literal(version)}'))
            else:
                raise ValueError(f'module.{module_name} has no source attribute.')
        for start,end,replacement in sorted(replacements,reverse=True):
            content=content[:start]+replacement+content[end:]
        if replacements:
            updated[name]=content
    if matches != 1:
        raise ValueError('The selected module dependency could not be updated uniquely.')
    authoring.validate_files(updated)
    return updated,version


def release_source(release):
    repository=str(release.get('repository') or '')
    repository_url=str(release.get('repository_url') or '')
    tag=str(release.get('tag') or '')
    version=str(release.get('version') or '')
    expected_url=f'https://github.com/{repository}'
    if (release.get('status') != 'published'
            or not REPOSITORY.fullmatch(repository)
            or repository_url.rstrip('/') not in (expected_url, expected_url + '.git')
            or not RELEASE_TAG.fullmatch(tag)
            or tag != 'v' + version
            or not COMMIT_SHA.fullmatch(str(release.get('commit_sha') or ''))):
        raise ValueError('The selected organization module release is invalid.')
    return f'git::{expected_url}.git?ref={quote(tag, safe="")}'


def wrapper_module_name(parent_name):
    """Return a stable Terraform reference label for the released parent module."""
    value=re.sub(r'[^A-Za-z0-9_]', '_', str(parent_name or ''))
    if not value or not re.match(r'^[A-Za-z_]', value):
        value='module_' + value
    return value


def update_module_release(files,module_name,release):
    if not authoring.NAME.fullmatch(str(module_name or '')):
        raise ValueError('Choose a valid module dependency.')
    source=release_source(release)
    updated=dict(files)
    matches=0
    for name,content in files.items():
        path=PurePosixPath(name)
        if path.parent != PurePosixPath('.'):
            continue
        if name.endswith(('.tf.json','.tofu.json')):
            document=json.loads(content)
            modules=document.get('module',{})
            if module_name in modules:
                modules[module_name]['source']=source
                modules[module_name].pop('version',None)
                updated[name]=json.dumps(document,indent=2,ensure_ascii=False)+'\n'
                matches += 1
            continue
        if not name.endswith(('.tf','.tofu')):
            continue
        tree=hcl2.parses(content)
        replacements=[]
        for block in tree.find_data('block'):
            if str(block.children[0].children[0]) != 'module':
                continue
            raw=content[block.meta.start_pos:block.meta.end_pos]
            label=re.match(r'\s*module\s+"([A-Za-z_][A-Za-z0-9_-]*)"',raw)
            if not label or label.group(1) != module_name:
                continue
            matches += 1
            body=next(child for child in block.children if getattr(child,'data',None) == 'body')
            attributes={str(child.children[0].children[0]):child for child in body.children
                        if getattr(child,'data',None) == 'attribute'}
            if 'source' not in attributes:
                raise ValueError(f'module.{module_name} has no source attribute.')
            attribute=attributes['source']
            replacements.append((attribute.meta.start_pos,attribute.meta.end_pos,
                                 f'source = {authoring.literal(source)}'))
            if 'version' in attributes:
                attribute=attributes['version']
                line_start=content.rfind('\n',0,attribute.meta.start_pos)+1
                line_end=content.find('\n',attribute.meta.end_pos)
                line_end=len(content) if line_end < 0 else line_end+1
                if not content[line_start:attribute.meta.start_pos].strip() and not content[attribute.meta.end_pos:line_end].strip():
                    replacements.append((line_start,line_end,''))
                else:
                    replacements.append((attribute.meta.start_pos,attribute.meta.end_pos,''))
        for start,end,replacement in sorted(replacements,reverse=True):
            content=content[:start]+replacement+content[end:]
        if replacements:
            updated[name]=content
    if matches != 1:
        raise ValueError('The selected module dependency could not be updated uniquely.')
    authoring.validate_files(updated)
    return updated


def build_released(files,name,release,parent_name):
    version=str(release.get('version') or '')
    try:
        version=registry.version_number(version)
    except registry.RegistryError as exc:
        raise ValueError('The selected organization module release has an invalid version.') from exc
    return _build(files,name,release_source(release),parent_name,
                  release.get('draft_revision'),version)


def _build(files,name,source,parent_name,parent_revision,display_version):
    if not authoring.NAME.fullmatch(name) or len(name) > 80:
        raise ValueError('Use a wrapper name starting with a letter or underscore, followed by letters, numbers, underscores or hyphens.')
    metadata=interface(files)
    module_name=wrapper_module_name(parent_name)
    metadata['wrapper_module_name']=module_name
    variable_blocks=raw_blocks(files,{'variable'})
    provider_blocks=raw_blocks(files,{'provider'})
    terraform_blocks=raw_blocks(files,{'terraform'})
    if len(variable_blocks) != len(metadata['inputs']):
        raise ValueError('Deployment wrapper generation currently requires root variable declarations in HCL .tf files.')
    arguments=[f'  {item["name"]} = var.{item["name"]}' for item in metadata['inputs']]
    if metadata['provider_aliases']:
        mappings='\n'.join(f'    {alias} = {alias}' for alias in metadata['provider_aliases'])
        arguments.append('  providers = {\n'+mappings+'\n  }')
    main=f'module "{module_name}" {{\n  source = {authoring.literal(source)}\n\n'+'\n'.join(arguments)+'\n}\n'
    output_blocks=[]
    for item in metadata['outputs']:
        lines=[f'output "{item["name"]}" {{']
        if item['description']:
            lines.append(f'  description = {authoring.literal(item["description"])}')
        lines.append(f'  value = module.{module_name}.{item["name"]}')
        if item['sensitive']:
            lines.append('  sensitive = true')
        lines.append('}')
        output_blocks.append('\n'.join(lines))
    warnings=[]
    provider_text='\n\n'.join(provider_blocks)
    terraform_text='\n\n'.join(terraform_blocks)
    if re.search(r'\blocal\.',provider_text):
        warnings.append('Copied provider configuration references local values; add the required locals before validation.')
    if re.search(r'\b(?:backend|cloud)\s*(?:"[^"]+")?\s*\{',terraform_text):
        warnings.append('Review the copied Terraform backend or cloud configuration before using this wrapper.')
    result={
        'main.tf':main,
        'variables.tf':'\n\n'.join(variable_blocks)+'\n',
        'outputs.tf':'\n\n'.join(output_blocks)+'\n',
        'providers.tf':provider_text+'\n' if provider_text else '',
        'versions.tf':terraform_text+'\n' if terraform_text else '',
        'README.md':(
            f'# {name}\n\nDeployment wrapper for `{parent_name}` revision {parent_revision}.\n\n'
            f'This wrapper calls `{source}` release `{display_version}` and carries forward the saved module interface. '
            'Review provider configuration and environment-specific values before deployment.\n'
        ),
    }
    result={key:value for key,value in result.items() if value}
    authoring.validate_files(result)
    return result,warnings,metadata
