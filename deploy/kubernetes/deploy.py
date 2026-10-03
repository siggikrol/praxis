#!/usr/bin/env python3
"""Build and deploy a complete local Praxis development environment."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
KUBE = ['kubectl', '--context', 'rancher-desktop']
DOCKER = ['docker', '--context', 'rancher-desktop']


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def apply(doc):
    run(KUBE+['apply','-f','-'], input=json.dumps(doc), text=True)


def require(command):
    if not shutil.which(command):
        raise SystemExit(f'{command} is required but was not found on PATH.')


def local_auth():
    path=ROOT/'.env'
    if not path.exists():
        path.write_text(
            'FLASK_SECRET='+secrets.token_urlsafe(48)+'\n'
            'BASIC_USER=admin\n'
            'BASIC_PASSWORD=change-me\n'
            'FLASK_SECURE_COOKIES=0\n'
            'PS_RUNTIME_ENV=dev\n'
            'ENABLED_MODULES=terraform_module_builder,terraform_stacks\n'
        )
        print('Created .env with local development login settings.')
    values={}
    for raw in path.read_text().splitlines():
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key,value=line.split('=',1)
        value=value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value=value[1:-1]
        values[key.strip()]=value
    values.update({key:os.environ[key] for key in ('FLASK_SECRET','BASIC_USER','BASIC_PASSWORD') if key in os.environ})
    auth={
        'FLASK_SECRET': values.get('FLASK_SECRET',''),
        'BASIC_USER': values.get('BASIC_USER','admin'),
        'BASIC_PASSWORD': values.get('BASIC_PASSWORD','change-me'),
    }
    if not auth['FLASK_SECRET'] or auth['FLASK_SECRET'] == 'replace-with-a-random-secret':
        raise SystemExit('Set FLASK_SECRET in .env to a random value, then run this command again.')
    return auth


def compose_auth():
    # Read values through Compose's parser without echoing credentials.
    data=json.loads(run(['docker','--context','desktop-linux','compose','config','--format','json'], capture_output=True, text=True).stdout)
    env=data['services']['web']['environment']
    return {key:env[key] for key in ('FLASK_SECRET','BASIC_USER','BASIC_PASSWORD')}


def auth_secret(values):
    apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':'praxis-auth','namespace':'praxis'},
           'type':'Opaque','stringData':values})


def load_yaml(path):
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit('PyYAML is required. Install the project requirements before starting Praxis.') from exc
    return yaml.safe_load((ROOT/path).read_text())


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--auth-from-compose', action='store_true', help='Copy the existing local Compose login settings into the Kubernetes Secret.')
    args=parser.parse_args()
    require('docker')
    require('kubectl')
    nodes=json.loads(run(KUBE+['get','nodes','-o','json'], capture_output=True, text=True).stdout)
    runtimes=[node['status']['nodeInfo']['containerRuntimeVersion'] for node in nodes['items']]
    if not runtimes or any(not runtime.startswith('docker://') for runtime in runtimes):
        raise SystemExit('Rancher Desktop Kubernetes must use the Moby (dockerd) container engine so it can run locally built images.')
    run(DOCKER+['info'], stdout=subprocess.DEVNULL)
    tag='local-'+str(int(time.time()))
    web='praxis-web:'+tag
    runner='praxis-tofu-runner:'+tag
    run(DOCKER+['build','-t',web,'.'])
    run(DOCKER+['build','-f','services/tofu_runner/Dockerfile','-t',runner,'.'])
    run(DOCKER+['image','inspect',web,runner], stdout=subprocess.DEVNULL)
    run(KUBE+['apply','-f','deploy/kubernetes/platform.yaml'])
    auth_secret(compose_auth() if args.auth_from_compose else local_auth())

    cache=load_yaml(Path('deploy/kubernetes/runner-cache.yaml'))
    cache['spec']['template']['spec']['containers'][0]['image']=runner
    apply(cache)
    run(KUBE+['-n','praxis-runs','rollout','status','deployment/praxis-runner-cache','--timeout=180s'])

    doc=load_yaml(Path('deploy/kubernetes/app.yaml'))
    container=doc['spec']['template']['spec']['containers'][0]
    container['image']=web
    container['imagePullPolicy']='Never'
    next(e for e in container['env'] if e['name']=='PRAXIS_TOFU_IMAGE')['value']=runner
    apply(doc)
    run(KUBE+['-n','praxis','rollout','status','deployment/praxis','--timeout=180s'])
    print(f'Praxis is ready with runner image {runner}.')
    print('Open http://praxis.localhost:8088 (Rancher Desktop Traefik port).')

if __name__=='__main__':
    main()
