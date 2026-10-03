import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from modules.terraform_module_builder import store as draft_store
from modules.terraform_stacks import lifecycle, stack_model, store
from modules.terraform_stacks.blueprint import bp


OWNER = 'alice'


class StackModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment_patch = patch.dict(os.environ, {
            'PRAXIS_TERRAFORM_DB': self.temp.name + '/stacks.sqlite3',
            'PRAXIS_TERRAFORM_MODULE_BUILDER_DB': self.temp.name + '/drafts.sqlite3',
        })
        self.environment_patch.start()

        self.customer = store.save(OWNER, 'customer', 'acme', {})
        self.environment = store.save(OWNER, 'environment', 'production', {
            'customer': self.customer,
            'type': 'Production',
        })
        self.account = store.save(OWNER, 'account', 'acme-production', {
            'customer': self.customer,
            'environment': self.environment,
            'provider': 'aws',
            'account_id': '123456789012',
        })

    def tearDown(self):
        self.environment_patch.stop()
        self.temp.cleanup()

    def release(self, name, version='1.2.0', files=None, layer='deployment-wrapper',
                status='published', commit_character='a'):
        files = files or {
            'variables.tf': '''
variable "cidr" {
  type = string
}

variable "availability_zones" {
  type = list(string)
}

variable "settings" {
  type = object({ enabled = bool, replicas = number })
}

variable "labels" {
  type    = map(string)
  default = {}
}
''',
            'outputs.tf': '''
output "vpc_id" {
  value = "test-vpc"
}

output "private_subnet_ids" {
  value = []
}
''',
            'main.tf': 'terraform { required_version = ">= 1.6.0" }\n',
        }
        draft_id = draft_store.create(OWNER, name, {
            'source': {'provider': 'aws'},
            'files': files,
        })
        draft = draft_store.get(OWNER, draft_id)
        release = draft_store.reserve_release(
            OWNER, draft, layer, version, 'v' + version,
            'acme/' + name, 'hashicorp/example/aws', '1.0.0',
        )
        if status == 'published':
            release = draft_store.update_release(
                OWNER, release['id'], 'published', commit_character * 40,
            )
        return draft_id, release

    def next_release(self, draft_id, version, commit_character='b'):
        draft = draft_store.get(OWNER, draft_id)
        self.assertTrue(draft_store.update(
            OWNER, draft_id, draft['revision'], draft['document'],
        ))
        draft = draft_store.get(OWNER, draft_id)
        release = draft_store.reserve_release(
            OWNER, draft, 'deployment-wrapper', version, 'v' + version,
            'acme/' + draft['name'], 'hashicorp/example/aws', '1.0.0',
        )
        return draft_store.update_release(
            OWNER, release['id'], 'published', commit_character * 40,
        )

    def stack_data(self, release, inputs, **overrides):
        data = {
            'customer': self.customer,
            'environment': self.environment,
            'account': self.account,
            'region': 'eu-west-1',
            'wrapper_release_id': release['id'],
            'inputs': inputs,
        }
        data.update(overrides)
        return data

    def valid_inputs(self):
        return {
            'cidr': {'kind': 'literal', 'value': '10.20.0.0/16'},
            'availability_zones': {
                'kind': 'literal',
                'value': ['eu-west-1a', 'eu-west-1b'],
            },
            'settings': {
                'kind': 'literal',
                'value': {'enabled': True, 'replicas': 3},
            },
        }

    def test_selects_only_an_exact_published_deployment_wrapper(self):
        _, published = self.release('praxis-vpc-wrapper')
        key = store.save(
            OWNER, 'stack', 'customer-a-prod-vpc',
            self.stack_data(published, self.valid_inputs()),
        )
        saved = store.get(OWNER, key)['data']
        self.assertEqual(saved['wrapper_release_id'], published['id'])
        self.assertEqual(saved['wrapper_version'], '1.2.0')
        self.assertEqual(saved['wrapper_tag'], 'v1.2.0')
        self.assertEqual(saved['wrapper_commit'], 'a' * 40)

        _, pending = self.release('pending-wrapper', status='pending')
        with self.assertRaisesRegex(ValueError, 'exact published deployment wrapper'):
            store.save(OWNER, 'stack', 'pending-stack',
                       self.stack_data(pending, self.valid_inputs()))

        _, root = self.release('organization-root', layer='organization-root')
        with self.assertRaisesRegex(ValueError, 'exact published deployment wrapper'):
            store.save(OWNER, 'stack', 'root-stack',
                       self.stack_data(root, self.valid_inputs()))

    def test_validates_required_unknown_and_typed_literal_inputs(self):
        _, release = self.release('typed-wrapper')
        inputs = self.valid_inputs()
        inputs.pop('cidr')
        with self.assertRaisesRegex(ValueError, 'Required wrapper input.*cidr'):
            store.save(OWNER, 'stack', 'missing-input', self.stack_data(release, inputs))

        inputs = self.valid_inputs()
        inputs['undeclared'] = {'kind': 'literal', 'value': 'no'}
        with self.assertRaisesRegex(ValueError, 'does not declare input undeclared'):
            store.save(OWNER, 'stack', 'unknown-input', self.stack_data(release, inputs))

        inputs = self.valid_inputs()
        inputs['availability_zones'] = {'kind': 'literal', 'value': 'eu-west-1a'}
        with self.assertRaisesRegex(ValueError, 'availability_zones.*list\(string\)'):
            store.save(OWNER, 'stack', 'wrong-list', self.stack_data(release, inputs))

        inputs = self.valid_inputs()
        inputs['settings'] = {'kind': 'literal', 'value': {'enabled': 'yes', 'replicas': 3}}
        with self.assertRaisesRegex(ValueError, 'settings.enabled.*bool'):
            store.save(OWNER, 'stack', 'wrong-object', self.stack_data(release, inputs))

    def test_stack_output_binding_builds_an_explicit_dependency(self):
        _, network_release = self.release('praxis-network-wrapper')
        network_id = store.save(
            OWNER, 'stack', 'network',
            self.stack_data(network_release, self.valid_inputs()),
        )
        _, eks_release = self.release('praxis-eks-wrapper', files={
            'variables.tf': 'variable "vpc_id" { type = string }\n',
            'outputs.tf': 'output "cluster_endpoint" { value = "test" }\n',
        })
        eks_id = store.save(OWNER, 'stack', 'eks', self.stack_data(eks_release, {
            'vpc_id': {
                'kind': 'stack_output',
                'stack_id': network_id,
                'output': 'vpc_id',
            },
        }))
        eks = store.get(OWNER, eks_id)
        self.assertEqual(eks['data']['dependencies'], [network_id])
        self.assertEqual(eks['data']['inputs']['vpc_id'], {
            'kind': 'stack_output',
            'stack_id': network_id,
            'output': 'vpc_id',
        })

        deployment = store.definition(OWNER, eks)
        self.assertEqual(deployment['dependencies'], [
            {'stack_id': network_id, 'stack': 'network'},
        ])
        self.assertEqual(deployment['inputs']['vpc_id']['stack_output']['output'], 'vpc_id')

    def test_rejects_missing_output_cross_context_and_cycles(self):
        files = {
            'variables.tf': '''
variable "peer_id" {
  type    = string
  default = null
}
''',
            'outputs.tf': 'output "id" { value = "test" }\n',
        }
        _, release = self.release('dependency-wrapper', files=files)
        first_id = store.save(OWNER, 'stack', 'first', self.stack_data(release, {}))

        with self.assertRaisesRegex(ValueError, 'does not declare output missing'):
            store.save(OWNER, 'stack', 'bad-output', self.stack_data(release, {
                'peer_id': {'kind': 'stack_output', 'stack_id': first_id, 'output': 'missing'},
            }))

        second_customer = store.save(OWNER, 'customer', 'other', {})
        second_environment = store.save(OWNER, 'environment', 'other-production', {
            'customer': second_customer,
            'type': 'Production',
        })
        second_account = store.save(OWNER, 'account', 'other-production', {
            'customer': second_customer,
            'environment': second_environment,
            'provider': 'aws',
        })
        other_id = store.save(OWNER, 'stack', 'other-stack', self.stack_data(
            release, {}, customer=second_customer,
            environment=second_environment, account=second_account,
        ))
        with self.assertRaisesRegex(ValueError, 'same customer and environment'):
            store.save(OWNER, 'stack', 'cross-context', self.stack_data(release, {
                'peer_id': {'kind': 'stack_output', 'stack_id': other_id, 'output': 'id'},
            }))

        second_id = store.save(OWNER, 'stack', 'second', self.stack_data(release, {
            'peer_id': {'kind': 'stack_output', 'stack_id': first_id, 'output': 'id'},
        }))
        first = store.get(OWNER, first_id)
        with self.assertRaisesRegex(ValueError, 'dependency cycle'):
            store.save(OWNER, 'stack', first['name'], self.stack_data(release, {
                'peer_id': {'kind': 'stack_output', 'stack_id': second_id, 'output': 'id'},
            }), first_id, first['revision'])

    def test_enforces_customer_environment_account_hierarchy(self):
        _, release = self.release('hierarchy-wrapper')
        other_environment = store.save(OWNER, 'environment', 'staging', {
            'customer': self.customer,
            'type': 'Staging',
        })
        other_account = store.save(OWNER, 'account', 'acme-staging', {
            'customer': self.customer,
            'environment': other_environment,
            'provider': 'aws',
        })
        with self.assertRaisesRegex(ValueError, 'different environment'):
            store.save(OWNER, 'stack', 'wrong-environment', self.stack_data(
                release, self.valid_inputs(), account=other_account,
            ))

        other_customer = store.save(OWNER, 'customer', 'other-customer', {})
        with self.assertRaisesRegex(ValueError, 'different customer'):
            store.save(OWNER, 'stack', 'wrong-customer', self.stack_data(
                release, self.valid_inputs(), customer=other_customer,
            ))

    def test_newer_semantic_release_is_detected_without_upgrading_stack(self):
        draft_id, current_release = self.release('upgradable-wrapper')
        stack_id = store.save(
            OWNER, 'stack', 'upgradable',
            self.stack_data(current_release, self.valid_inputs()),
        )
        newer_release = self.next_release(draft_id, '1.3.0')
        before = store.get(OWNER, stack_id)
        status = stack_model.release_status(OWNER, before)
        after = store.get(OWNER, stack_id)

        self.assertEqual(status, {
            'current': '1.2.0',
            'available': '1.3.0',
            'update_available': True,
            'latest_release_id': newer_release['id'],
        })
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(after['data']['wrapper_release_id'], current_release['id'])
        self.assertEqual(after['data']['wrapper_version'], '1.2.0')

        store.save(OWNER, 'stack', after['name'],
                   dict(after['data'], wrapper_release_id=newer_release['id']),
                   after['id'], after['revision'])
        upgraded = store.get(OWNER, stack_id)
        self.assertEqual(upgraded['revision'], 2)
        self.assertEqual(upgraded['data']['wrapper_release_id'], newer_release['id'])
        self.assertEqual(upgraded['data']['wrapper_version'], '1.3.0')

    def test_generated_deployment_is_configuration_only_and_commit_pinned(self):
        _, release = self.release('deployment-wrapper')
        stack_id = store.save(
            OWNER, 'stack', 'network',
            self.stack_data(release, self.valid_inputs()),
        )
        stack = store.get(OWNER, stack_id)
        deployment = store.definition(OWNER, stack)

        self.assertEqual(deployment['schema_version'], 2)
        self.assertEqual(deployment['kind'], 'praxis-stack-deployment')
        self.assertEqual(deployment['wrapper']['version'], '1.2.0')
        self.assertEqual(deployment['wrapper']['tag'], 'v1.2.0')
        self.assertEqual(deployment['wrapper']['commit'], 'a' * 40)
        self.assertIn('?ref=' + 'a' * 40, deployment['wrapper']['source'])
        self.assertEqual(deployment['context'], {
            'customer': 'acme',
            'environment': 'production',
        })
        self.assertEqual(deployment['target']['account_id'], '123456789012')
        self.assertEqual(deployment['inputs']['cidr'], {
            'literal': '10.20.0.0/16',
        })

        encoded = json.dumps(deployment).lower()
        self.assertNotIn('files', encoded)
        self.assertNotIn('terraform.tfstate', encoded)
        self.assertNotIn('credentials', encoded)
        stored = json.dumps(stack['data'])
        self.assertNotIn('variable "cidr"', stored)
        self.assertNotIn('terraform {', stored)
        with self.assertRaisesRegex(ValueError, 'not part of the released-wrapper Stack model'):
            lifecycle.start(OWNER, stack, 'apply')

    def test_ui_builds_wrapper_inputs_and_shows_available_release(self):
        draft_id, current_release = self.release('ui-wrapper')
        stack_id = store.save(
            OWNER, 'stack', 'existing-stack',
            self.stack_data(current_release, self.valid_inputs()),
        )
        self.next_release(draft_id, '1.3.0')
        root = Path(__file__).resolve().parents[1]
        app = Flask(__name__, template_folder=str(root / 'templates'))
        app.secret_key = 'test'
        app.register_blueprint(bp)
        app.add_url_rule('/terraform', 'terraform', lambda: '')
        app.jinja_env.globals.update(has_endpoint=lambda name: False,
                                     csrf_token=lambda: 'test')
        from jinja2 import ChoiceLoader, DictLoader
        app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '{% block content %}{% endblock %}'}),
            app.jinja_loader,
        ])
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = OWNER

        path = (f'/terraform/workspace/new/stack?customer={self.customer}'
                f'&environment={self.environment}'
                f'&wrapper_release_id={current_release["id"]}')
        form = client.get(path)
        self.assertEqual(form.status_code, 200)
        self.assertIn(b'Released deployment wrapper', form.data)
        self.assertIn(b'input_cidr_kind', form.data)
        self.assertIn(b'input_availability_zones_kind', form.data)
        self.assertIn(b'Output from another Stack', form.data)

        response = client.post(path, data={
            'name': 'ui-stack',
            'stack_model': 'released-wrapper',
            'wrapper_release_id': current_release['id'],
            'account': self.account,
            'region': 'eu-west-1',
            'input_cidr_kind': 'literal',
            'input_cidr_literal': '"10.30.0.0/16"',
            'input_availability_zones_kind': 'literal',
            'input_availability_zones_literal': '["eu-west-1a", "eu-west-1b"]',
            'input_settings_kind': 'literal',
            'input_settings_literal': '{"enabled": true, "replicas": 2}',
        })
        self.assertEqual(response.status_code, 302)
        created = next(item for item in store.objects(OWNER, 'stack')
                       if item['name'] == 'ui-stack')
        self.assertEqual(created['data']['wrapper_release_id'], current_release['id'])

        detail = client.get('/terraform/workspace/objects/' + stack_id)
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'Current:</strong> 1.2.0', detail.data)
        self.assertIn(b'Available:</strong> 1.3.0', detail.data)
        self.assertIn(b'Generated deployment', detail.data)
        self.assertNotIn(b'Request plan', detail.data)


if __name__ == '__main__':
    unittest.main()
