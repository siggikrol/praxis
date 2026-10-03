"""Derive editable mock-test settings from a saved Terraform module."""
import json
import posixpath
from pathlib import PurePosixPath
import re

import hcl2

DATA_REFERENCE = re.compile(r'\bdata\.([A-Za-z_][\w-]*)\.([A-Za-z_][\w-]*)\.([A-Za-z_][\w-]*)')


def blocks(value):
    return [value] if isinstance(value, dict) else (value or [])


def parse(name, content):
    if name.endswith(('.tf.json', '.tofu.json')):
        return json.loads(content)
    return hcl2.loads(content)


def root_documents(files):
    documents = []
    for name, content in files.items():
        path = PurePosixPath(name)
        if path.parent != PurePosixPath('.') or not name.endswith(('.tf', '.tf.json', '.tofu', '.tofu.json')):
            continue
        documents.append((name, parse(name, content)))
    return documents


def literal(value):
    if isinstance(value, str):
        return '${' not in value and '%{' not in value
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, list):
        return all(literal(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and literal(item) for key, item in value.items())
    return False


def example_inputs(files, variable_names):
    candidates = []
    ignored = {'source', 'version', 'providers', 'depends_on', 'count', 'for_each'}
    for name, content in files.items():
        path = PurePosixPath(name)
        if path.parent == PurePosixPath('.') or not name.endswith(('.tf', '.tf.json', '.tofu', '.tofu.json')):
            continue
        try:
            document = parse(name, content)
        except (ValueError, TypeError):
            continue
        for entry in blocks(document.get('module', [])):
            for _, spec in entry.items():
                source = spec.get('source') if isinstance(spec, dict) else None
                if not isinstance(source, str) or '${' in source:
                    continue
                target = posixpath.normpath(posixpath.join(str(path.parent), source))
                if target != '.':
                    continue
                values = {key:value for key,value in spec.items()
                          if key in variable_names and key not in ignored and literal(value)}
                if values:
                    candidates.append((len(values), name, values))
    if not candidates:
        return None, {}
    _, name, values = max(candidates, key=lambda item:(item[0], item[1]))
    return name, values


def sample_string(name):
    upper = name.upper()
    if 'REGION' in upper:
        return 'us-east-1'
    if 'CIDR' in upper:
        return '10.0.0.0/16'
    if 'TRANSIT_GATEWAY' in upper:
        return 'tgw-0123456789abcdef0'
    if upper.endswith('SUBNET_ID') or upper.startswith('SUBNET_'):
        return 'subnet-0123456789abcdef0'
    if upper.endswith('VPC_ID'):
        return 'vpc-0123456789abcdef0'
    if upper.endswith('AMI_ID') or upper == 'AMI':
        return 'ami-0123456789abcdef0'
    if 'ENVIRONMENT' in upper or upper == 'ENV':
        return 'development'
    if 'CUSTOMER' in upper:
        return 'example-customer'
    if 'PRODUCT' in upper:
        return 'example-product'
    if 'ORGANIZATION' in upper:
        return 'example-support'
    if 'NAME' in upper:
        return 'example-'+name.lower().replace('_', '-')
    return 'example-'+name.lower().replace('_', '-')


def split_arguments(value):
    result=[]
    start=0
    depth=0
    quoted=False
    escaped=False
    for index, character in enumerate(value):
        if escaped:
            escaped=False
        elif character == '\\' and quoted:
            escaped=True
        elif character == '"':
            quoted=not quoted
        elif not quoted and character in '([{':
            depth += 1
        elif not quoted and character in ')]}':
            depth -= 1
        elif not quoted and character == ',' and depth == 0:
            result.append(value[start:index].strip())
            start=index+1
    result.append(value[start:].strip())
    return [item for item in result if item]


def typed_value(name, type_expression):
    expression=str(type_expression or 'string').strip()
    if expression.startswith('${') and expression.endswith('}'):
        expression=expression[2:-1].strip()
    lower=expression.lower()
    for collection in ('list', 'set'):
        prefix=collection+'('
        if lower.startswith(prefix) and expression.endswith(')'):
            return [typed_value(name, expression[len(prefix):-1])]
    if lower.startswith('map(') and expression.endswith(')'):
        return {}
    if lower.startswith('tuple([') and expression.endswith('])'):
        return [typed_value(f'{name}_{index+1}', item)
                for index,item in enumerate(split_arguments(expression[7:-2]))]
    if lower.startswith('object(') and expression.endswith(')'):
        try:
            attributes=json.loads(expression[7:-1])
        except json.JSONDecodeError:
            return {}
        result={}
        for attribute, attribute_type in attributes.items():
            normalized=str(attribute_type)
            if normalized.startswith('${optional('):
                continue
            result[attribute]=typed_value(attribute, attribute_type)
        return result
    if lower.startswith('optional('):
        return None
    if lower == 'bool':
        return False
    if lower == 'number':
        return 1
    return sample_string(name)


def sample_value(name, type_expression):
    type_name=str(type_expression or 'string').lower()
    upper=name.upper()
    if 'FILTER_AZ_ZONE' in upper:
        return ['use1-az1', 'use1-az2', 'use1-az3']
    if 'SUBNET' in upper and ('list' in type_name or 'set' in type_name):
        return ['10.0.1.0/24', '10.0.2.0/24', '10.0.3.0/24']
    if ('CIDR' in upper or upper.endswith('_IPS')) and ('list' in type_name or 'set' in type_name):
        return ['10.0.0.0/24']
    if 'list' in type_name or 'set' in type_name or 'tuple' in type_name:
        return typed_value(name, type_expression)
    if 'map' in type_name or 'object' in type_name or 'bool' in type_name or 'number' in type_name:
        return typed_value(name, type_expression)
    return typed_value(name, type_expression)


def availability_zone_defaults(inputs, attributes):
    region=str(inputs.get('AWS_REGION') or inputs.get('aws_region') or inputs.get('region') or 'us-east-1')
    zone_ids=inputs.get('FILTER_AZ_ZONE_IDS') or inputs.get('filter_az_zone_ids')
    if not isinstance(zone_ids, list) or not zone_ids:
        zone_ids=['use1-az1','use1-az2','use1-az3']
    count=max(3, len(zone_ids))
    values={}
    if 'names' in attributes:
        values['names']=[region+chr(ord('a')+index) for index in range(count)]
    if 'zone_ids' in attributes:
        values['zone_ids']=zone_ids
    return values


def mock_attribute(resource_type, attribute, inputs):
    upper=attribute.upper()
    if attribute == 'id':
        if resource_type == 'aws_ec2_transit_gateway':
            return inputs.get('TRANSIT_GATEWAY_ID') or 'tgw-0123456789abcdef0'
        return 'mock-'+resource_type.replace('_', '-')+'-id'
    if attribute.endswith('_ids') or attribute == 'ids':
        return ['mock-'+attribute.removesuffix('s').replace('_', '-')]
    if attribute.endswith('names') or attribute == 'names':
        return ['mock-a', 'mock-b', 'mock-c']
    if 'cidr' in attribute:
        return inputs.get('CIDR_BLOCK') or '10.0.0.0/16'
    if attribute.startswith('is_') or attribute.endswith('_enabled'):
        return False
    if upper in inputs:
        return inputs[upper]
    return 'mock-'+attribute.replace('_', '-')


def data_defaults(files, documents, inputs):
    data_sources = set()
    for _, document in documents:
        for entry in blocks(document.get('data', [])):
            for resource_type, instances in entry.items():
                for instance in instances:
                    data_sources.add((resource_type, instance))
    attributes = {source:set() for source in data_sources}
    for name, content in files.items():
        if PurePosixPath(name).parent != PurePosixPath('.'):
            continue
        for resource_type, instance, attribute in DATA_REFERENCE.findall(content):
            if (resource_type, instance) in attributes:
                attributes[(resource_type, instance)].add(attribute)
    result = {}
    for (resource_type, _), names in sorted(attributes.items()):
        if not names:
            continue
        if resource_type == 'aws_availability_zones':
            values=availability_zone_defaults(inputs, names)
        else:
            values={name:mock_attribute(resource_type, name, inputs) for name in sorted(names)}
        result.setdefault(resource_type, {}).update(values)
    return result


def generate(files):
    documents=root_documents(files)
    variables={}
    outputs={}
    for _, document in documents:
        for entry in blocks(document.get('variable', [])):
            for name, spec in entry.items():
                variables[name]=spec
        for entry in blocks(document.get('output', [])):
            for name, spec in entry.items():
                value=spec.get('value') if isinstance(spec, dict) else None
                if literal(value):
                    outputs[name]=value
    example, inputs=example_inputs(files, set(variables))
    generated=[]
    inputs=dict(inputs)
    for name, spec in variables.items():
        if name not in inputs and 'default' not in spec:
            inputs[name]=sample_value(name, spec.get('type'))
            generated.append(name)
    return {
        'variables': inputs,
        'data_defaults': data_defaults(files, documents, inputs),
        'expected_outputs': outputs,
        'example': example,
        'generated_variables': generated,
    }
