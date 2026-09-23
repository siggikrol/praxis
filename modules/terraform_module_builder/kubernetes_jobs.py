"""Namespaced Kubernetes Jobs. No host kubeconfig or cloud credentials are used."""
import base64
import gzip
import json
import os

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

TIMEOUT = (5, 20)


def namespace():
    return os.getenv('PRAXIS_TOFU_NAMESPACE', 'praxis-runs')


def apis():
    config.load_incluster_config()
    return client.BatchV1Api(), client.CoreV1Api()


def job_name(key):
    return 'praxis-test-' + key


def payload_bytes(payload):
    raw = json.dumps(payload).encode()
    if len(raw) > 3_000_000:
        raise ValueError('Draft and test settings exceed the 3 MB runner limit.')
    compressed = gzip.compress(raw)
    if len(compressed) > 700_000:
        raise ValueError('This draft snapshot is too large for a Kubernetes test Job (700 KB compressed). Reduce included files.')
    return compressed


def manifest(key, draft_id):
    name = job_name(key)
    labels = {'app.kubernetes.io/name': 'praxis-tofu-test', 'praxis-run': key, 'praxis-draft': draft_id}
    return {
        'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': name, 'namespace': namespace(), 'labels': labels},
        'spec': {
            'backoffLimit': 0, 'activeDeadlineSeconds': 600,
            # TTL is added only AFTER the result has been committed to SQLite.
            'template': {'metadata': {'labels': labels}, 'spec': {
                'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                'serviceAccountName': 'praxis-test', 'enableServiceLinks': False,
                'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532, 'runAsGroup': 65532,
                                    'fsGroup': 65532, 'seccompProfile': {'type': 'RuntimeDefault'}},
                'containers': [{
                    'name': 'runner', 'image': os.environ['PRAXIS_TOFU_IMAGE'], 'imagePullPolicy': 'IfNotPresent',
                    'command': ['python', '/runner/job.py'],
                    'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                        'capabilities': {'drop': ['ALL']}},
                    'resources': {'requests': {'cpu': '250m', 'memory': '256Mi'},
                                  'limits': {'cpu': '2', 'memory': '3Gi', 'ephemeral-storage': '3Gi'}},
                    'volumeMounts': [{'name': 'snapshot', 'mountPath': '/input', 'readOnly': True},
                                     {'name': 'scratch', 'mountPath': '/tmp'}],
                }],
                'volumes': [{'name': 'snapshot', 'secret': {'secretName': name, 'defaultMode': 0o440}},
                            {'name': 'scratch', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '2Gi'}}],
            }},
        },
    }


def submit(key, draft_id, payload):
    compressed = payload_bytes(payload)
    batch, core = apis()
    name = job_name(key)
    job = batch.create_namespaced_job(namespace(), manifest(key, draft_id), _request_timeout=TIMEOUT)
    try:
        core.create_namespaced_secret(namespace(), {
            'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
            'metadata': {'name': name, 'labels': {'app.kubernetes.io/name': 'praxis-tofu-test'},
                         'ownerReferences': [{'apiVersion': 'batch/v1', 'kind': 'Job', 'name': name,
                                              'uid': job.metadata.uid, 'controller': True}]},
            'immutable': True, 'data': {'payload.json.gz': base64.b64encode(compressed).decode()},
        }, _request_timeout=TIMEOUT)
    except Exception:
        batch.delete_namespaced_job(name, namespace(), propagation_policy='Background', _request_timeout=TIMEOUT)
        raise


def result(key):
    batch, core = apis()
    name = job_name(key)
    job = batch.read_namespaced_job(name, namespace(), _request_timeout=TIMEOUT)
    terminal = any(c.type in ('Complete', 'Failed') and c.status == 'True' for c in (job.status.conditions or []))
    if not terminal:
        return None
    pods = core.list_namespaced_pod(namespace(), label_selector='job-name='+name, _request_timeout=TIMEOUT).items
    for pod in pods:
        if pod.status.phase not in ('Succeeded', 'Failed'):
            continue
        try:
            log = core.read_namespaced_pod_log(pod.metadata.name, namespace(), container='runner',
                                               limit_bytes=1_000_000, _request_timeout=TIMEOUT)
            for line in reversed(log.splitlines()):
                if line.startswith('PRAXIS_RESULT='):
                    parsed = json.loads(line.removeprefix('PRAXIS_RESULT='))
                    if isinstance(parsed, dict):
                        return parsed
        except (ValueError, ApiException):
            pass
    reasons = '; '.join(c.message or c.reason or c.type for c in (job.status.conditions or []) if c.status == 'True')
    return {'passed': False, 'error': 'Test Pod ended without a result. ' + reasons}


def schedule_cleanup(key):
    batch, _ = apis()
    try:
        batch.patch_namespaced_job(job_name(key), namespace(),
                                   {'spec': {'ttlSecondsAfterFinished': 60}}, _request_timeout=TIMEOUT)
    except ApiException as exc:
        if exc.status != 404:
            raise


def delete(key):
    batch, _ = apis()
    try:
        batch.delete_namespaced_job(job_name(key), namespace(), propagation_policy='Background', _request_timeout=TIMEOUT)
    except ApiException as exc:
        if exc.status != 404:
            raise
