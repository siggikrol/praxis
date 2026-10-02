#!/usr/bin/env python3
"""Runs only in GitHub Actions. The Praxis web app never invokes this script."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def load_definition(commit, path):
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('A full Git commit is required.')
    if not re.fullmatch(r'[a-z][a-z0-9-]*/[a-z][a-z0-9-]*/[a-z][a-z0-9-]*/stack.yaml', path):
        raise ValueError('Invalid stack path.')
    raw = command(['git', 'show', commit + ':' + path], capture_output=True).stdout
    d = yaml.safe_load(raw)
    if not isinstance(d, dict) or d.get('schema_version') != 1:
        raise ValueError('Unsupported stack definition.')
    if not re.fullmatch(r'[a-f0-9]{32}', d.get('id', '')):
        raise ValueError('Invalid stack identity.')
    if d.get('target', {}).get('provider') != 'aws':
        raise ValueError('Only AWS execution is supported.')
    module = d.get('module', {})
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', module.get('repository', '')) or not re.fullmatch(r'[a-f0-9]{40}', module.get('commit', '')):
        raise ValueError('Module source must be a Git repository pinned to a commit.')
    if d.get('state') != {'backend': 's3', 'key': 'stacks/' + d['id'] + '/terraform.tfstate'}:
        raise ValueError('State must have a dedicated stack key and no inline backend credentials.')
    inputs = d.get('inputs')
    if not isinstance(inputs, dict) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k) or k in ('source', 'version', 'providers', 'count', 'for_each', 'depends_on') for k in inputs):
        raise ValueError('Invalid module inputs.')
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', d['target'].get('region', '')):
        raise ValueError('Invalid AWS region.')
    return d


def literal(value):
    # Terraform JSON strings interpret interpolation unless escaped.
    if isinstance(value, str):
        return value.replace('${', '$${').replace('%{', '%%{')
    if isinstance(value, dict):
        return {k: literal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [literal(v) for v in value]
    return value


def root_config(d):
    return {'terraform': {'required_version': '= 1.12.0', 'required_providers': {'aws': {'source': 'hashicorp/aws'}}, 'backend': {'s3': {}}},
            'provider': {'aws': {'region': d['target']['region']}},
            'module': {'stack': dict(literal(d['inputs']), source='git::https://github.com/' + d['module']['repository'] + '.git?ref=' + d['module']['commit'])},
            'output': {'stack': {'value': '${module.stack}', 'sensitive': True}}}


def main():
    operation = os.environ['OPERATION']
    commit = os.environ['CONFIG_COMMIT']
    d = load_definition(commit, os.environ['STACK_PATH'])
    report = {'operation': operation, 'commit': commit, 'request_id': os.environ['REQUEST_ID'], 'stack_id': d['id']}
    Path('result').mkdir(exist_ok=True)
    if operation == 'validate':
        report['summary'] = 'Stack schema, module pin and state identity validated. No cloud plan was performed.'
    elif operation in ('plan', 'apply'):
        if os.environ.get('CLOUD_EXECUTION_ENABLED') != 'true':
            raise ValueError('Cloud execution is disabled in this GitHub environment.')
        if d['target']['execution_environment'] != os.environ['EXECUTION_ENVIRONMENT']:
            raise ValueError('Execution environment does not match the committed stack.')
        if d['target'].get('account_id') != os.environ.get('AWS_ACCOUNT_ID') or not re.fullmatch(r'\d{12}', d['target'].get('account_id', '')):
            raise ValueError('Execution account does not match the committed AWS target.')
        head = command(['git', 'ls-remote', 'origin', 'HEAD'], capture_output=True).stdout.split()[0]
        if head != commit:
            raise ValueError('Configuration commit is no longer the default branch head. Re-plan the current commit.')
        work = Path('workspace')
        work.mkdir(exist_ok=True)
        config = json.dumps(root_config(d), sort_keys=True)
        (work / 'main.tf.json').write_text(config)
        backend = {'bucket': os.environ['STATE_BUCKET'], 'region': os.environ['STATE_REGION'], 'key': d['state']['key'], 'use_lockfile': True, 'encrypt': True}
        if not backend['bucket'] or not backend['region']:
            raise ValueError('Configure S3 backend variables in the execution environment.')
        backend_hash = hashlib.sha256(json.dumps(backend, sort_keys=True).encode()).hexdigest()
        (work / 'backend.json').write_text(json.dumps(backend))
        if operation == 'apply':
            saved = json.loads(Path('approved-plan/manifest.json').read_text())
            plan = Path('approved-plan/tfplan').read_bytes()
            digest = hashlib.sha256(plan).hexdigest()
            if saved != {'commit': commit, 'stack_id': d['id'], 'config_sha256': hashlib.sha256(config.encode()).hexdigest(), 'backend_sha256': backend_hash, 'plan_sha256': digest} or digest != os.environ['PLAN_SHA256']:
                raise ValueError('Saved plan does not match this approval, commit and stack.')
            (work / '.terraform.lock.hcl').write_bytes(Path('approved-plan/.terraform.lock.hcl').read_bytes())
        init = ['tofu', 'init', '-input=false', '-no-color', '-backend-config=backend.json']
        if operation == 'apply':
            init.append('-lockfile=readonly')
        command(init, cwd=work)
        command(['tofu', 'validate', '-no-color'], cwd=work)
        if operation == 'plan':
            command(['tofu', 'plan', '-input=false', '-no-color', '-lock-timeout=60s', '-out=tfplan'], cwd=work)
            output = command(['tofu', 'show', '-no-color', 'tfplan'], cwd=work, capture_output=True).stdout
            digest = hashlib.sha256((work / 'tfplan').read_bytes()).hexdigest()
            (work / 'manifest.json').write_text(json.dumps({'commit': commit, 'stack_id': d['id'], 'config_sha256': hashlib.sha256(config.encode()).hexdigest(), 'backend_sha256': backend_hash, 'plan_sha256': digest}))
            report.update(plan_sha256=digest, summary=output[:80000], truncated=len(output) > 80000)
        else:
            command(['tofu', 'apply', '-input=false', '-no-color', '-lock-timeout=60s', '../approved-plan/tfplan'], cwd=work)
            report['summary'] = 'Applied the exact approved plan artifact.'
    else:
        raise ValueError('Unknown operation.')
    Path('result/result.json').write_text(json.dumps(report))


if __name__ == '__main__':
    main()
