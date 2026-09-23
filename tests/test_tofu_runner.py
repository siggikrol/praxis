import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hcl2
from services.tofu_runner import server
from modules.terraform_module_builder import store, testing

class RunnerTests(unittest.TestCase):
    def test_generated_mock_is_plan_only_with_assertions_and_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            server.prepare({'main.tf':'''terraform {
 required_providers {
  aws = { source = "hashicorp/aws", configuration_aliases = [aws.secondary] }
 }
}
output "name" { value = "demo" }
''', 'danger.tftest.hcl':'run "danger" {command=apply}'}, root)
            self.assertFalse((root/'danger.tftest.hcl').exists())
            source=server.mock_test(root, {'variables':{'region':'eu-west-1'},'data_defaults':{'aws_availability_zones':{'names':['eu-west-1a']}}, 'expected_outputs':{'name':'demo'}})
            doc=hcl2.loads(source)
            self.assertEqual(doc['run'][0]['praxis_mock_plan']['command'],'${plan}')
            self.assertNotIn('provider',doc)
            self.assertEqual(len(doc['mock_provider']),2)
            self.assertIn('output.name',source)
            with self.assertRaises(ValueError): server.prepare({'../escape.tf':''},root)
            with self.assertRaises(ValueError): server.mock_test(root,{'expected_outputs':{'missing':'x'}})
    def test_json_mock_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            server.prepare({'main.tf.json':json.dumps({'terraform':{'required_providers':{'google':{'source':'hashicorp/google'}}},'output':{'id':{'value':'x'}}})},root)
            self.assertIn('mock_provider "google"',server.mock_test(root,{}))
    def test_stages_block_network_and_stop_on_failure(self):
        client=server.app.test_client()
        payload={'files':{'main.tf':'output "name" {value="demo"}'},'mode':'mock','settings':{}}
        with patch.object(server,'command',return_value={'passed':True}) as execute:
            result=client.post('/run',json=payload)
            self.assertEqual(result.status_code,200)
            self.assertEqual(execute.call_count,3)
            self.assertIn('-backend=false',execute.call_args_list[0].args[1])
            self.assertTrue(execute.call_args_list[1].kwargs['offline'])
            self.assertTrue(execute.call_args_list[2].kwargs['offline'])
            self.assertIn('-filter=.praxis-tests/praxis.tftest.hcl',execute.call_args_list[2].args[1])
            self.assertNotIn('AWS_ACCESS_KEY_ID',execute.call_args.args[2])
        with patch.object(server,'command',return_value={'passed':False}) as execute:
            self.assertFalse(client.post('/run',json=payload).json['passed'])
            self.assertEqual(execute.call_count,1)
    def test_job_revision_owner_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,PRAXIS_TERRAFORM_MODULE_BUILDER_DB=directory+'/db',PRAXIS_TOFU_RUNNER_URL='http://runner'):
            key=store.create('alice','test',{'files':{'main.tf':''}})
            draft=store.get('alice',key)
            with patch.object(testing.threading,'Thread'):
                job=testing.start('alice',draft,'validate',{})
                self.assertIsNone(testing.latest('bob',key))
                self.assertEqual(testing.latest('alice',key)['revision'],1)
                with self.assertRaises(ValueError): testing.start('alice',draft,'mock',{})
            response=type('Response',(),{'ok':True,'json':lambda self:{'passed':True,'steps':[]}})()
            with patch.object(testing.requests,'post',return_value=response): testing._execute('http://runner',job,{})
            self.assertEqual(testing.latest('alice',key)['status'],'passed')
            self.assertTrue(store.delete('alice',key,1))
            self.assertIsNone(testing.latest('alice',key))
