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

    def test_wrapper_mock_overrides_released_parent_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            server.prepare({'main.tf': '''
module "pds_network" {
  source = "git::https://github.com/acme/pds-network.git?ref=v2.1.0"
}
output "vpc_id" { value = module.pds_network.vpc_id }
'''}, root)
            source=server.mock_test(root, {}, ['pds_network'])
            document=hcl2.loads(source)
            self.assertEqual(document['override_module'][0]['target'],
                             '${module.pds_network}')
            with self.assertRaisesRegex(ValueError, 'not declared'):
                server.mock_test(root, {}, ['another_module'])
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
            self.assertEqual(execute.call_count,4)
            self.assertIn('-backend=false',execute.call_args_list[1].args[1])
            self.assertTrue(execute.call_args_list[2].kwargs['offline'])
            self.assertTrue(execute.call_args_list[3].kwargs['offline'])
            self.assertIn('-filter=.praxis-tests/praxis.tftest.hcl',execute.call_args_list[3].args[1])
            self.assertNotIn('AWS_ACCESS_KEY_ID',execute.call_args.args[2])
        with patch.object(server,'command',return_value={'passed':False}) as execute:
            self.assertFalse(client.post('/run',json=payload).json['passed'])
            self.assertEqual(execute.call_count,1)

        invalid=dict(payload, override_modules=['missing'])
        with patch.object(server,'command',return_value={'passed':True}):
            response=client.post('/run',json=invalid)
        self.assertEqual(response.status_code,400)

    def test_private_git_credential_exists_only_during_init(self):
        token = 'github_pat_private-module'
        payload = {
            'files': {'main.tf': 'output "name" {value="demo"}'},
            'mode': 'validate', 'settings': {},
            'git_auth': {'host': 'github.com', 'token': token},
        }
        credential_files = []

        def execute(root, args, environment, timeout, offline=False):
            if args[0] == 'init':
                self.assertEqual(environment['GIT_CONFIG_KEY_0'],
                                 'credential.https://github.com.helper')
                self.assertNotIn(token, repr(environment))
                helper = Path(environment['GIT_CONFIG_VALUE_0'].removeprefix('!'))
                token_file = Path(environment['PRAXIS_GIT_TOKEN_FILE'])
                self.assertTrue(helper.is_file())
                self.assertEqual(token_file.read_text(), token)
                credential_files.extend((helper, token_file))
            else:
                self.assertNotIn('PRAXIS_GIT_TOKEN_FILE', environment)
            return {'passed': True, 'command': 'tofu ' + ' '.join(args), 'output': ''}

        with patch.object(server, 'command', side_effect=execute):
            response = server.app.test_client().post('/run', json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(token, response.get_data(as_text=True))
        self.assertTrue(credential_files)
        self.assertTrue(all(not path.exists() for path in credential_files))

        invalid = dict(payload, git_auth={'host': 'example.com', 'token': token})
        with patch.object(server, 'command', return_value={'passed': True}):
            response = server.app.test_client().post('/run', json=invalid)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(token, response.get_data(as_text=True))
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

    def test_release_uses_passing_mock_for_exact_revision(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, PRAXIS_TERRAFORM_MODULE_BUILDER_DB=directory+'/db',
                PRAXIS_TOFU_RUNNER_URL='http://runner'):
            key=store.create('alice','test',{'files':{'main.tf':''}})
            draft=store.get('alice',key)
            responses = {
                'mock': {'passed':True,'steps':[],'mode':'mock','check_suite':2},
                'validate': {'passed':True,'steps':[],'mode':'validate','check_suite':2},
            }
            for mode in ('mock','validate'):
                with patch.object(testing.threading,'Thread'):
                    job=testing.start('alice',draft,mode,{})
                response=type('Response',(),{
                    'ok':True, 'json':lambda self, value=responses[mode]:value,
                })()
                with patch.object(testing.requests,'post',return_value=response):
                    testing._execute('http://runner',job,{})
            self.assertEqual(testing.latest('alice',key)['mode'],'validate')
            self.assertEqual(testing.latest('alice',key,mode='mock',revision=1)['mode'],
                             'mock')
            self.assertTrue(testing.release_ready('alice',key,1))
            self.assertFalse(testing.release_ready('alice',key,2))

    def test_format_result_saves_revision_and_rejects_concurrent_edit(self):
        for changed in (False, True):
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, PRAXIS_TERRAFORM_MODULE_BUILDER_DB=directory+'/db', PRAXIS_TOFU_RUNNER_URL='http://runner', PRAXIS_TEST_BACKEND='http'):
                key = store.create('alice', 'test', {'files': {'main.tf': 'old'}})
                draft = store.get('alice', key)
                with patch.object(testing.threading, 'Thread'):
                    job = testing.start('alice', draft, 'format', {})
                if changed:
                    store.update('alice', key, 1, {'files': {'main.tf': 'user edit'}})
                response = type('Response', (), {'ok': True, 'json': lambda self: {'passed': True, 'formatted_files': {'main.tf': 'formatted'}}})()
                with patch.object(testing.requests, 'post', return_value=response):
                    testing._execute('http://runner', job, {})
                item = store.get('alice', key)
                self.assertEqual(item['revision'], 2)
                self.assertEqual(item['document']['files']['main.tf'], 'user edit' if changed else 'formatted')
                self.assertEqual(testing.latest('alice', key)['status'], 'failed' if changed else 'passed')

    def test_format_mode_does_not_initialize_or_validate(self):
        with patch.object(server, 'command', return_value={'passed': True}) as execute:
            result = server.app.test_client().post('/run', json={'mode': 'format', 'files': {'main.tf': ''}})
            self.assertTrue(result.json['passed'])
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[1], ['fmt', '-recursive', '-no-color'])
            self.assertTrue(execute.call_args.kwargs['offline'])
            self.assertNotIn('check_suite', result.json)
