import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from modules.terraform_stacks import store, lifecycle, github
from modules.terraform_stacks.blueprint import bp

SHA = 'a' * 40


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'PRAXIS_TERRAFORM_DB': self.temp.name + '/catalog.sqlite3', 'PRAXIS_CLOUD_EXECUTION_ENABLED': '0'})
        self.env.start()
        self.customer = store.save('alice', 'customer', 'acme', {})
        self.environment = store.save('alice', 'environment', 'acme-dev', {'customer': self.customer, 'type': 'Development'})
        self.account = store.save('alice', 'account', 'acme-nonprod', {'customer': self.customer, 'provider': 'aws'})
        self.module = store.save('alice', 'module', 'praxis-vpc', {'repository': 'siggikrol/praxis-vpc', 'status': 'Approved', 'versions': [{'tag': 'v1.0.0', 'commit': SHA}]})
        self.data = {'customer': self.customer, 'environment': self.environment, 'account': self.account, 'module': self.module, 'version': 'v1.0.0', 'region': 'eu-central-1', 'inputs': {'cidr': '10.0.0.0/16'}}
        self.key = store.save('alice', 'stack', 'acme-dev-vpc', dict(self.data))
        self.stack = store.get('alice', self.key)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_relations_and_ownership(self):
        with self.assertRaises(ValueError):
            store.get('bob', self.key)
        other = store.save('alice', 'customer', 'other', {})
        with self.assertRaisesRegex(ValueError, 'different customer'):
            store.save('alice', 'stack', 'invalid', dict(self.data, customer=other))
        # Multiple targets for the same customer; shared target has no customer restriction.
        store.save('alice', 'account', 'acme-prod', {'customer': self.customer, 'provider': 'aws'})
        shared = store.save('alice', 'account', 'shared', {'provider': 'aws'})
        store.save('alice', 'stack', 'shared-vpc', dict(self.data, account=shared))

    def test_definition_separates_configuration_and_state(self):
        definition = store.definition('alice', self.stack)
        self.assertEqual(definition['module']['commit'], SHA)
        self.assertEqual(definition['state']['key'], f'stacks/{self.key}/terraform.tfstate')
        self.assertNotIn('bucket', json.dumps(definition))
        self.assertNotIn('cidr', store.get('alice', self.module)['data'])
        self.assertEqual(store.config_path('alice', self.stack), 'acme/acme-dev/acme-dev-vpc/stack.yaml')

    def test_revisions_and_moved_tags(self):
        module = store.get('alice', self.module)
        store.save('alice', 'module', module['name'], dict(module['data'], versions=[{'tag': 'v1.0.0', 'commit': 'b' * 40}]), self.module, 1)
        store.save('alice', 'stack', self.stack['name'], dict(self.data), self.key, 1)
        self.assertEqual(store.get('alice', self.key)['data']['module_commit'], SHA)
        with self.assertRaisesRegex(ValueError, 'changed'):
            store.save('alice', 'stack', self.stack['name'], dict(self.data), self.key, 1)

    def test_reserved_inputs_and_unsynced_versions(self):
        with self.assertRaises(ValueError):
            store.save('alice', 'stack', 'bad', dict(self.data, inputs={'source': 'bad'}))
        with self.assertRaisesRegex(ValueError, 'Sync module versions'):
            store.save('alice', 'stack', 'bad', dict(self.data, version='v9'))

    def test_cloud_execution_disabled(self):
        with patch.object(github, 'connected', return_value=True), patch.object(github, 'dispatch') as dispatch:
            with self.assertRaisesRegex(ValueError, 'disabled'):
                lifecycle.start('alice', self.stack, 'plan')
            dispatch.assert_not_called()

    def test_validation_dispatch_and_duplicate_guard(self):
        with patch.object(github, 'connected', return_value=True), patch.object(lifecycle, 'merged_revision', return_value=('main', SHA)), patch.object(github, 'dispatch') as dispatch:
            key = lifecycle.start('alice', self.stack, 'validate')
            self.assertEqual(dispatch.call_args.args[2]['request_id'], key)
            with self.assertRaisesRegex(ValueError, 'already active'):
                lifecycle.start('alice', self.stack, 'validate')
        self.assertEqual(store.runs('alice', self.key)[0]['status'], 'queued')

    def test_changed_definition_blocks_dispatch(self):
        with patch.object(github, 'head', return_value=('main', SHA)), patch.object(github, 'contents', return_value='{}'):
            with self.assertRaisesRegex(ValueError, 'differs'):
                lifecycle.merged_revision('alice', self.stack)

    def test_empty_stack_repository_explains_prerequisite_without_dispatch(self):
        with patch.object(github, 'connected', return_value=True), patch.object(github, 'head', side_effect=github.RepositoryConflict('409')), patch.object(github, 'dispatch') as dispatch:
            with self.assertRaisesRegex(ValueError, 'Submit configuration PR'):
                lifecycle.start('alice', self.stack, 'validate')
            dispatch.assert_not_called()
            self.assertEqual(store.runs('alice', self.key), [])

    def test_stack_publish_bootstraps_empty_repository_and_bundles_workflow(self):
        pr={'number':1,'html_url':'https://github.com/example/env/pull/1'}
        with patch.object(github, 'head', side_effect=[github.RepositoryConflict('409'), ('main',SHA)]), patch.object(github, 'request', side_effect=[github.RepositoryConflict('409'), {}, {'tree':[]}]) as api, patch.object(github, 'pull_request', return_value=pr) as submit:
            lifecycle.publish('alice', self.stack)
        self.assertEqual(api.call_args_list[1].args[0], 'PUT')
        self.assertTrue(api.call_args_list[1].args[1].endswith('/contents/README.md'))
        files=submit.call_args.args[2]
        self.assertIn('.github/workflows/praxis-stack.yml', files)
        self.assertIn('scripts/praxis_stack.py', files)
        self.assertIn(store.config_path('alice',self.stack), files)

    def test_stack_publish_preserves_existing_workflow(self):
        paths=['.github/workflows/praxis-stack.yml','scripts/praxis_stack.py']
        with patch.object(github, 'head', return_value=('main',SHA)), patch.object(github, 'request', return_value={'tree':[{'path':p} for p in paths]}), patch.object(github, 'pull_request', return_value={'number':1,'html_url':'https://github.com/pr'}) as submit:
            lifecycle.publish('alice',self.stack)
        self.assertEqual(list(submit.call_args.args[2]),[store.config_path('alice',self.stack)])

    def test_result_correlated_to_stack_commit_and_request(self):
        data = {'commit': SHA, 'repository': github.repository()}
        key = store.record('alice', self.key, 'validate', data)
        remote = {'id': 42, 'display_title': 'praxis-' + key, 'event': 'workflow_dispatch', 'path': '.github/workflows/praxis-stack.yml', 'status': 'completed', 'conclusion': 'success', 'html_url': 'https://github.com/run/42'}
        result = {'request_id': key, 'commit': SHA, 'operation': 'validate', 'stack_id': self.key}
        with patch.object(github, 'workflow_runs', return_value=[remote]), patch.object(github, 'report', return_value=dict(result, commit='b' * 40)):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                lifecycle.refresh('alice', self.stack)
        with patch.object(github, 'workflow_runs', return_value=[remote]), patch.object(github, 'report', return_value=result):
            lifecycle.refresh('alice', self.stack)
        self.assertEqual(store.runs('alice', self.key)[0]['status'], 'passed')

    def test_exact_plan_approval_and_consumption(self):
        key = store.record('alice', self.key, 'plan', {})
        data = {'commit': SHA, 'definition_hash': lifecycle.digest(store.definition('alice', self.stack)), 'github_run': 42, 'result': {'plan_sha256': 'c' * 64}}
        store.update_run('alice', key, 'planned', data)
        with patch.object(lifecycle, 'execution_ready'), patch.object(lifecycle, 'merged_revision', return_value=('main', SHA)), patch.object(github, 'connected', return_value=True), patch.object(github, 'dispatch'):
            lifecycle.approve('alice', self.stack, key)
            plan = store.runs('alice', self.key)[0]
            self.assertEqual(plan['data']['approval']['plan_sha256'], 'c' * 64)
            lifecycle.start('alice', self.stack, 'apply', plan)
            with self.assertRaises(ValueError):
                lifecycle.start('alice', self.stack, 'apply', plan)

    def test_stale_plan_not_approved(self):
        key = store.record('alice', self.key, 'plan', {})
        store.update_run('alice', key, 'planned', {'commit': SHA, 'definition_hash': 'outdated'})
        with patch.object(lifecycle, 'execution_ready'), patch.object(lifecycle, 'merged_revision', return_value=('main', SHA)):
            with self.assertRaisesRegex(ValueError, 'stale'):
                lifecycle.approve('alice', self.stack, key)

    def test_ui_and_workflow_export(self):
        root = Path(__file__).resolve().parents[1]
        app = Flask(__name__, template_folder=str(root / 'templates'))
        app.secret_key = 'test'
        app.register_blueprint(bp)
        app.add_url_rule('/terraform', 'terraform', lambda: '')
        app.jinja_env.globals.update(has_endpoint=lambda name: False, csrf_token=lambda: 'test')
        # Render the foundation against a minimal base, without unrelated app integrations.
        from jinja2 import ChoiceLoader, DictLoader
        app.jinja_loader = ChoiceLoader([DictLoader({'base.html': '{% block content %}{% endblock %}'}), app.jinja_loader])
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = 'alice'
        with patch.dict(os.environ, {'PRAXIS_TERRAFORM_MODULE_BUILDER_DB': self.temp.name + '/drafts.sqlite3'}):
            for path in ['/', '/new/stack', '/objects/' + self.key, '/objects/' + self.module, '/workflow-kit.zip']:
                response = client.get('/terraform/workspace' + path)
                self.assertEqual(response.status_code, 200, (path, response.data[:300]))

    def test_customer_navigation_and_scoped_creation(self):
        root = Path(__file__).resolve().parents[1]
        app = Flask(__name__, template_folder=str(root / 'templates'))
        app.secret_key = 'test'
        app.register_blueprint(bp)
        app.add_url_rule('/terraform', 'terraform', lambda: '')
        app.jinja_env.globals.update(has_endpoint=lambda name: False, csrf_token=lambda: 'test')
        from jinja2 import ChoiceLoader, DictLoader
        app.jinja_loader = ChoiceLoader([DictLoader({'base.html': '{% block content %}{% endblock %}'}), app.jinja_loader])
        client = app.test_client()
        with client.session_transaction() as session:
            session['user'] = 'alice'
        other = store.save('alice', 'customer', 'different-customer', {})
        other_env = store.save('alice', 'environment', 'other-dev', {'customer': other, 'type': 'Development'})
        other_account = store.save('alice', 'account', 'other-account', {'customer': other, 'provider': 'aws'})
        shared = store.save('alice', 'account', 'shared-account', {'provider': 'aws'})
        foreign = store.save('bob', 'customer', 'private-customer', {})
        prefix = '/terraform/workspace'
        customer_page = client.get(prefix + '/objects/' + self.customer)
        self.assertEqual(customer_page.status_code, 200)
        self.assertIn(b'acme-dev', customer_page.data)
        self.assertNotIn(b'other-dev', customer_page.data)
        env_page = client.get(prefix + '/objects/' + self.environment)
        self.assertIn(b'acme-dev-vpc', env_page.data)
        path = prefix + '/new/stack?customer=' + self.customer + '&environment=' + self.environment
        form = client.get(path)
        self.assertEqual(form.status_code, 200)
        self.assertIn(b'shared-account', form.data)
        self.assertNotIn(b'other-account', form.data)
        self.assertNotIn(b'other-dev', form.data)
        payload = dict(self.data, name='second-vpc', inputs='{}', customer=other, environment=other_env)
        response = client.post(path, data=payload)
        self.assertEqual(response.status_code, 302)
        created = next(s for s in store.objects('alice', 'stack') if s['name']=='second-vpc')
        self.assertEqual(created['data']['customer'], self.customer)
        self.assertEqual(created['data']['environment'], self.environment)
        payload.update(name='blocked-vpc', account=other_account)
        response = client.post(path, data=payload)
        self.assertIn(b'different customer', response.data)
        self.assertEqual(client.get(prefix + '/new/environment?customer=' + foreign).status_code, 404)
        self.assertEqual(client.get(prefix + '/objects/' + foreign).status_code, 404)
        self.assertEqual(client.get(prefix + '/new/stack?customer='+other+'&environment='+self.environment).status_code,404)
        self.assertEqual(client.get(prefix + '/catalog/module').status_code, 200)
        self.assertIn(b'shared-account', client.get(prefix + '/catalog/account').data)
        for n in range(13):
            store.save('alice','customer','customer-' + str(n),{})
        listing = client.get(prefix + '/')
        self.assertEqual(listing.data.count(b'mb-panel tf-customer-card'), 12)
        self.assertIn(b'Next', listing.data)
        filtered = client.get(prefix + '/?q=acme')
        self.assertEqual(filtered.data.count(b'mb-panel tf-customer-card'), 1)
        self.assertNotIn(b'different-customer', filtered.data)

    def test_workflow_schema_and_literal_inputs(self):
        path = Path(__file__).resolve().parents[1] / 'modules/terraform_stacks/workflows/environments/scripts/praxis_stack.py'
        spec = importlib.util.spec_from_file_location('workflow', path)
        workflow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(workflow)
        definition = store.definition('alice', self.stack)
        definition['inputs']['text'] = '${file("secret")}'
        root = workflow.root_config(definition)
        self.assertEqual(root['module']['stack']['text'], '$${file("secret")}')
        with patch.object(workflow, 'command') as command:
            command.return_value.stdout = json.dumps(definition)
            self.assertEqual(workflow.load_definition(SHA, 'acme/dev/vpc/stack.yaml'), definition)
            with self.assertRaises(ValueError):
                workflow.load_definition(SHA, '../../bad.yaml')


if __name__ == '__main__':
    unittest.main()
