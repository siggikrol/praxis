#!/usr/bin/env python3
"""Build and deploy only to the explicit local Rancher Desktop context."""
import argparse
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
KUBE = ['kubectl', '--context', 'rancher-desktop']
DOCKER = ['docker', '--context', 'rancher-desktop']


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def apply(doc):
    run(KUBE+['apply','-f','-'], input=json.dumps(doc), text=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--auth-from-compose', action='store_true', help='Copy the existing local Compose login settings into the Kubernetes Secret.')
    args=parser.parse_args()
    run(KUBE+['get','nodes'])
    tag='local-'+str(int(time.time()))
    web='praxis-web:'+tag
    runner='praxis-tofu-runner:'+tag
    run(DOCKER+['build','-t',web,'.'])
    run(DOCKER+['build','-f','services/tofu_runner/Dockerfile','-t',runner,'.'])
    run(KUBE+['apply','-f','deploy/kubernetes/platform.yaml'])
    if args.auth_from_compose:
        # Read values through Compose's parser without echoing credentials.
        data=json.loads(run(['docker','--context','desktop-linux','compose','config','--format','json'], capture_output=True, text=True).stdout)
        env=data['services']['web']['environment']
        apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':'praxis-auth','namespace':'praxis'},
               'type':'Opaque','stringData':{key:env[key] for key in ('FLASK_SECRET','BASIC_USER','BASIC_PASSWORD')}})
    # PyYAML is already an app dependency; require the project Python environment.
    import yaml
    doc=yaml.safe_load((ROOT/'deploy/kubernetes/app.yaml').read_text())
    container=doc['spec']['template']['spec']['containers'][0]
    container['image']=web
    next(e for e in container['env'] if e['name']=='PRAXIS_TOFU_IMAGE')['value']=runner
    apply(doc)
    run(KUBE+['-n','praxis','rollout','status','deployment/praxis','--timeout=180s'])
    print('Praxis deployed. Open http://praxis.localhost:8088 (Rancher Desktop Traefik port).')

if __name__=='__main__':
    main()
