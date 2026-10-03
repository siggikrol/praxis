import base64
import gzip
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modules.terraform_module_builder import kubernetes_jobs as jobs, testing, store

class KubernetesJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,PRAXIS_TERRAFORM_MODULE_BUILDER_DB=self.temp.name+'/db',
                            PRAXIS_TEST_BACKEND='kubernetes',PRAXIS_TOFU_IMAGE='runner:test',
                            PRAXIS_TOFU_IMAGE_PULL_POLICY='Never')
        self.env.start()
    def tearDown(self):
        self.env.stop();self.temp.cleanup()
    def test_manifest_is_one_isolated_job(self):
        doc=jobs.manifest('abc','draft')
        self.assertNotIn('ttlSecondsAfterFinished',doc['spec'])
        self.assertEqual(doc['spec']['backoffLimit'],0)
        spec=doc['spec']['template']['spec']
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertEqual(spec['restartPolicy'],'Never')
        self.assertEqual(spec['containers'][0]['command'],['python','/runner/job.py'])
        self.assertEqual(spec['containers'][0]['imagePullPolicy'],'Never')
        self.assertTrue(spec['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(any('hostPath' in v or 'persistentVolumeClaim' in v for v in spec['volumes']))
        with self.assertRaises(ValueError): jobs.payload_bytes({'files':{'file':'x'*3_000_001}})
    def test_snapshot_is_owned_by_job(self):
        batch,core=Mock(),Mock()
        batch.create_namespaced_job.return_value=SimpleNamespace(metadata=SimpleNamespace(uid='job-uid'))
        payload={'files':{'main.tf':'output "x" {value=1}'},'mode':'validate'}
        with patch.object(jobs,'apis',return_value=(batch,core)):
            jobs.submit('abc','draft',payload)
        secret=core.create_namespaced_secret.call_args.args[1]
        self.assertEqual(secret['metadata']['ownerReferences'][0]['uid'],'job-uid')
        self.assertEqual(json.loads(gzip.decompress(base64.b64decode(secret['data']['payload.json.gz']))),payload)

    def test_private_wrapper_token_is_ephemeral_job_input(self):
        key=store.create('alice','wrapper',{
            'files':{'main.tf':'module "root" { source = "git::https://github.com/acme/root.git?ref=v1.0.0" }'},
            'source':{'mode':'draft-wrapper'},
        })
        with patch('modules.terraform_stacks.github.access_token',return_value='saved-token'), \
                patch.object(jobs,'submit') as submit:
            testing.start('alice',store.get('alice',key),'validate',{'variables':{'name':'demo'}})
        payload=submit.call_args.args[2]
        self.assertEqual(payload['git_auth'],{'host':'github.com','token':'saved-token'})
        with store.connection() as connection:
            row=dict(connection.execute(
                'SELECT settings,result FROM module_test_runs WHERE draft_id=?',(key,)
            ).fetchone())
        self.assertEqual(json.loads(row['settings']),{'variables':{'name':'demo'}})
        self.assertNotIn('saved-token',json.dumps(row))
    def test_restart_reconciliation_persists_before_cleanup(self):
        key=store.create('alice','test',{'files':{'main.tf':''}})
        with patch.object(jobs,'submit'):
            run=testing.start('alice',store.get('alice',key),'validate',{})
        def check_persisted(_):
            with store.connection() as conn:
                row=conn.execute('SELECT status,result FROM module_test_runs WHERE id=?',(run,)).fetchone()
                self.assertEqual(row['status'],'passed')
                self.assertTrue(json.loads(row['result'])['passed'])
        with patch.object(jobs,'result',return_value={'passed':True,'steps':[]}), patch.object(jobs,'schedule_cleanup',side_effect=check_persisted) as cleanup:
            testing.reconcile()
            cleanup.assert_called_once_with(run)
        self.assertEqual(testing.latest('alice',key)['status'],'passed')
        self.assertIsNone(testing.latest('bob',key))
    def test_cleanup_retried_and_deleted_draft_cancels_job(self):
        key=store.create('alice','test',{'files':{'main.tf':''}})
        with patch.object(jobs,'submit'):
            run=testing.start('alice',store.get('alice',key),'validate',{})
        self.assertTrue(store.delete('alice',key,1))
        with patch.object(jobs,'delete') as remove:
            testing.reconcile()
            remove.assert_called_once_with(run)
        with store.connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM module_test_runs').fetchone()[0],0)
    def test_submit_failure_is_visible(self):
        key=store.create('alice','test',{'files':{'main.tf':''}})
        with patch.object(jobs,'submit',side_effect=RuntimeError('API unavailable')):
            testing.start('alice',store.get('alice',key),'validate',{})
        with patch.object(jobs,'schedule_cleanup'):
            result=testing.latest('alice',key)
        self.assertEqual(result['status'],'failed')
        self.assertIn('Could not create',result['result']['error'])
