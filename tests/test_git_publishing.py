import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Flask, session
from flask_wtf import CSRFProtect
from modules.terraform_stacks import settings, github, publishing, store
from modules.terraform_stacks.blueprint import bp
from modules.terraform_module_builder import store as drafts


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'PRAXIS_TERRAFORM_DB': self.temp.name+'/foundation.sqlite3',
            'PRAXIS_TERRAFORM_MODULE_BUILDER_DB': self.temp.name+'/drafts.sqlite3'})
        self.env.start()
        self.release_setup = patch.object(publishing.releases, 'setup_files', return_value={'.github/workflows/praxis-module.yml': 'workflow'})
        self.release_setup.start()
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1]/'templates'))
        self.app.secret_key = 'test-only-key'
        self.app.register_blueprint(bp)
        self.app.add_url_rule('/terraform', 'terraform', lambda: '')
        self.app.add_url_rule('/draft/<key>', 'terraform_module_builder.draft', lambda key: '')
        self.app.jinja_env.globals.update(has_endpoint=lambda name: False)
        from jinja2 import ChoiceLoader, DictLoader
        self.app.jinja_loader = ChoiceLoader([DictLoader({'base.html': '{% block content %}{% endblock %}'}), self.app.jinja_loader])
        CSRFProtect(self.app)
        self.context = self.app.test_request_context()
        self.context.push()
        session['user'] = 'alice'
        self.draft = drafts.create('alice', 'platform-vpc', {'source': {'address': 'terraform-aws-modules/vpc/aws'}, 'files': {'main.tf': ''}})

    def tearDown(self):
        self.release_setup.stop()
        self.context.pop()
        self.env.stop()
        self.temp.cleanup()

    def configure(self, token='test-token'):
        settings.save('alice', {'owner': 'another-org', 'repository': 'another-org/environments', 'auth_mode': 'token'}, token)

    def test_token_encryption_user_isolation_and_removal(self):
        self.configure()
        self.assertEqual(settings.token(), 'test-token')
        self.assertEqual(settings.token('alice'), 'test-token')
        self.assertEqual(github.access_token('alice'), 'test-token')
        self.assertEqual(github.repository(), 'another-org/environments')
        with store.connection() as db:
            value = db.execute('SELECT data FROM tf_git_settings').fetchone()[0]
            self.assertNotIn('test-token', value)
        session['user'] = 'bob'
        self.assertIsNone(settings.token())
        session['user'] = 'alice'
        settings.save('alice', settings.load(), '')
        self.assertEqual(settings.token(), 'test-token')
        settings.save('alice', settings.load(), remove=True)
        self.assertIsNone(settings.token())
        self.assertFalse(github.connected())

    def test_token_transport_and_app_selection(self):
        self.configure()
        response = Mock(status_code=200, content=b'{}')
        response.json.return_value = {'login': 'alice'}
        with patch.object(github.requests, 'request', return_value=response) as send:
            self.assertEqual(github.request('GET','/user')['login'], 'alice')
            self.assertEqual(send.call_args.kwargs['headers']['Authorization'], 'Bearer test-token')
        settings.save('alice', dict(settings.load(), auth_mode='app'))
        self.assertIsNone(settings.token())
        with patch.dict(os.environ, {'GITHUB_APP_ID':'1','GITHUB_INSTALLATION_ID':'2',
                                     'GITHUB_PRIVATE_KEY':'key'}), \
                patch.object(github.github_api,'get_token',return_value='installation-token'):
            self.assertEqual(github.access_token('alice'),'installation-token')

    def test_precise_errors_do_not_reflect_remote_content(self):
        for code, text in [(401,'rejected'),(403,'denied'),(404,'not found'),(409,'conflict'),(422,'could not create')]:
            response = Mock(status_code=code, text='private token must not be shown')
            with patch.object(github, 'transport', return_value=response):
                with self.assertRaisesRegex(ValueError, text) as error:
                    github.request('GET','/repos/example/test')
                self.assertNotIn('private token', str(error.exception))

    def test_release_commit_updates_default_branch_and_tags_exact_commit(self):
        created = 'c' * 40
        with patch.object(github, 'head', return_value=('main', 'a' * 40)), \
                patch.object(github, 'request', side_effect=[
                    {'tree': {'sha': 'b' * 40}}, {'sha': 'd' * 40},
                    {'sha': created}, {},
                ]) as api:
            self.assertEqual(github.commit_files(
                'acme/module', {'main.tf': 'new'}, 'release', ['old.tf']), created)
        self.assertEqual(api.call_args_list[-1].args,
                         ('PATCH', '/repos/acme/module/git/refs/heads/main'))
        self.assertEqual(api.call_args_list[-1].kwargs['json'],
                         {'sha': created, 'force': False})
        tree = api.call_args_list[1].kwargs['json']['tree']
        self.assertIn({'path': 'main.tf', 'mode': '100644', 'type': 'blob',
                       'content': 'new'}, tree)
        self.assertIn({'path': 'old.tf', 'mode': '100644', 'type': 'blob',
                       'sha': None}, tree)
        with patch.object(github, 'versions', return_value=[]), \
                patch.object(github, 'request') as api:
            github.create_tag('acme/module', 'v1.2.3', created)
        api.assert_called_once_with('POST', '/repos/acme/module/git/refs',
                                    json={'ref': 'refs/tags/v1.2.3', 'sha': created})

    def test_cloud_labels_work_for_existing_drafts(self):
        google = drafts.create('alice', 'gke', {'source': {'address': 'terraform-google-modules/kubernetes-engine/google'}})
        unknown = drafts.create('alice', 'unknown', {'source': {}})
        items = {d['id']: d for d in drafts.list_drafts('alice')}
        self.assertEqual(items[self.draft]['provider'], 'aws')
        self.assertEqual(items[google]['provider'], 'google')
        self.assertEqual(items[unknown]['provider'], 'unknown')
        self.assertEqual(drafts.get('alice', google)['provider'], 'google')

    def test_submission_registers_and_links_module_once(self):
        self.configure()
        pr = {'number': 12, 'state': 'open', 'html_url': 'https://github.com/another-org/vpc/pull/12'}
        with patch.object(publishing.testing, 'latest', return_value={'status':'passed','revision':1,'result':{'check_suite':2}}), patch.object(github, 'head', return_value=('main','a'*40)), patch.object(github, 'request', return_value=pr), patch.object(github, 'pull_request', return_value=pr) as send:
            key, link = publishing.submit('alice', self.draft, 1, 'platform-vpc', 'another-org/vpc', True)
            self.assertEqual(publishing.submit('alice', self.draft, 1, 'platform-vpc', 'another-org/vpc', True), (key, link))
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], {'main.tf': '', '.github/workflows/praxis-module.yml': 'workflow'})
        item = store.get('alice', key, 'module')
        self.assertEqual(item['data']['provider'], 'aws')
        self.assertEqual(item['data']['submission']['number'], 12)
        self.assertEqual(len(store.objects('alice', 'module')), 1)

    def test_submission_rejects_wrong_owner_stale_or_untested_draft(self):
        self.configure()
        with patch.object(github, 'pull_request') as send:
            with self.assertRaises(ValueError):
                publishing.submit('bob', self.draft, 1, 'vpc', 'another-org/vpc', True)
            with self.assertRaises(ValueError):
                publishing.submit('alice', self.draft, 2, 'vpc', 'another-org/vpc', True)
            with patch.object(publishing.testing, 'latest', return_value={'status':'passed','revision':0}):
                with self.assertRaisesRegex(ValueError,'sanity'):
                    publishing.submit('alice', self.draft, 1, 'vpc', 'another-org/vpc', True)
            send.assert_not_called()

    def test_repository_creation_is_explicit(self):
        self.configure()
        pr = {'number':1, 'html_url':'https://github.com/another-org/vpc/pull/1'}
        with patch.object(publishing.testing,'latest',return_value={'status':'passed','revision':1,'result':{'check_suite':2}}), patch.object(github,'create_repository') as create, patch.object(github,'pull_request',return_value=pr):
            publishing.submit('alice',self.draft,1,'vpc','another-org/vpc',True,create=True)
            create.assert_called_once_with('another-org/vpc')

    def test_existing_release_config_is_not_added_to_module_update(self):
        self.configure()
        pr = {'number': 2, 'html_url': 'https://github.com/another-org/vpc/pull/2'}
        with patch.object(publishing.testing, 'latest', return_value={'status': 'passed', 'revision': 1, 'result': {'check_suite': 2}}), patch.object(github, 'head', return_value=('main', 'a'*40)), patch.object(publishing.releases, 'setup_files', return_value={}) as setup, patch.object(github, 'pull_request', return_value=pr) as send:
            publishing.submit('alice', self.draft, 1, 'vpc', 'another-org/vpc', True)
            setup.assert_called_once_with('another-org/vpc', preserve_existing=True)
            self.assertEqual(send.call_args.args[2], {'main.tf': ''})

    def test_draft_cannot_reset_release_version(self):
        self.configure()
        draft = {'provider': 'aws', 'document': {'files': {'main.tf': '', 'version.txt': '0.0.0'}}}
        with patch.object(publishing, 'checked_draft', return_value=draft), patch.object(github, 'create_repository') as create, patch.object(github, 'pull_request') as send:
            with self.assertRaisesRegex(ValueError, 'managed in Git'):
                publishing.submit('alice', self.draft, 1, 'vpc', 'another-org/vpc', True, create=True)
            create.assert_not_called()
            send.assert_not_called()

    def test_deleted_repository_recreation_bypasses_cached_submission(self):
        self.configure()
        old = {'status': 'Development', 'repository': 'another-org/vpc', 'versions': [{'tag': 'v1.0.0'}],
               'submission': {'draft': self.draft, 'revision': 1, 'number': 1, 'url': 'old'},
               'release_workflow_pr': {'number': 2, 'url': 'old-setup'}, 'checks': [{'name': 'old'}]}
        key = store.save('alice', 'module', 'vpc', old)
        pr = {'id': 123, 'number': 1, 'html_url': 'new-pr'}
        with patch.object(publishing.testing, 'latest', return_value={'status': 'passed', 'revision': 1, 'result': {'check_suite': 2}}), patch.object(github, 'create_repository') as create, patch.object(github, 'pull_request', return_value=pr) as send:
            self.assertEqual(publishing.submit('alice', self.draft, 1, 'vpc', 'another-org/vpc', True, create=True), (key, 'new-pr'))
            create.assert_called_once()
            send.assert_called_once()
        data = store.get('alice', key)['data']
        self.assertNotIn('release_workflow_pr', data)
        self.assertEqual(data['versions'], [])
        self.assertEqual(data['checks'], [])
        self.assertEqual(data['submission']['github_id'], 123)

    def test_deleted_repository_does_not_report_cached_success(self):
        self.configure()
        store.save('alice', 'module', 'vpc', {'status': 'Development', 'repository': 'another-org/vpc', 'submission': {
            'draft': self.draft, 'revision': 1, 'number': 1, 'url': 'old'}})
        with patch.object(publishing.testing, 'latest', return_value={'status': 'passed', 'revision': 1, 'result': {'check_suite': 2}}), patch.object(github, 'request', side_effect=ValueError('not found (404)')), patch.object(github, 'create_repository') as create:
            with self.assertRaisesRegex(ValueError, 'not found'):
                publishing.submit('alice', self.draft, 1, 'vpc', 'another-org/vpc', True)
            create.assert_not_called()

    def test_repository_health_preserves_history_and_recovers(self):
        from modules.terraform_stacks import repositories
        key = store.save('alice', 'module', 'vpc', {'status': 'Development',
            'repository': 'another-org/vpc', 'versions': [{'tag': 'v1.0.0'}],
            'submission': {'url': 'historical-pr'}})
        with patch.object(github, 'request', side_effect=github.ResourceNotFound('404')):
            with self.assertRaisesRegex(ValueError, 'missing or inaccessible'):
                repositories.check('alice', store.get('alice', key))
        item = store.get('alice', key)
        self.assertEqual(item['data']['repository_health']['state'], 'unavailable')
        self.assertEqual(item['data']['versions'], [{'tag': 'v1.0.0'}])
        self.assertEqual(item['data']['submission']['url'], 'historical-pr')
        with patch.object(github, 'request', side_effect=ValueError('Network error')):
            with self.assertRaisesRegex(ValueError, 'Network error'):
                repositories.check('alice', item)
        item = store.get('alice', key)
        self.assertEqual(item['data']['repository_health']['state'], 'check_failed')
        with patch.object(github, 'request', return_value={'id': 1}):
            item = repositories.check('alice', item)
        self.assertEqual(item['data']['repository_health']['state'], 'available')
        self.assertEqual(item['data']['versions'], [{'tag': 'v1.0.0'}])

    def test_old_validation_pass_does_not_allow_submission(self):
        self.configure()
        with patch.object(publishing.testing, 'latest', return_value={'status': 'passed', 'revision': 1, 'result': {}}), patch.object(github, 'pull_request') as send:
            with self.assertRaisesRegex(ValueError, 'sanity check'):
                publishing.submit('alice', self.draft, 1, 'vpc', 'another-org/vpc', True)
            send.assert_not_called()

    def test_new_repository_configures_and_verifies_release_permission(self):
        with patch.object(github, 'request', side_effect=[{'type':'User'}, {'login':'alice'}, {'id':123}, {}, {'can_approve_pull_request_reviews':True}]) as api:
            result = github.create_repository('alice/module')
        self.assertTrue(result['praxis_release_permissions']['configured'])
        self.assertEqual(api.call_args_list[3].args, ('PUT', '/repos/alice/module/actions/permissions/workflow'))
        self.assertEqual(api.call_args_list[3].kwargs['json'], {'can_approve_pull_request_reviews': True})

    def test_permission_failure_preserves_created_repository_with_warning(self):
        with patch.object(github, 'request', side_effect=[{'type':'User'}, {'login':'alice'}, {'id':123}, ValueError('denied (403)')]):
            result = github.create_repository('alice/module')
        self.assertEqual(result['id'], 123)
        self.assertFalse(result['praxis_release_permissions']['configured'])
        self.assertIn('Administration write', result['praxis_release_permissions']['message'])

    def test_settings_dont_render_token_and_posts_require_csrf(self):
        self.configure('secret-do-not-render')
        client = self.app.test_client()
        with client.session_transaction() as s:
            s['user']='alice'
        response = client.get('/terraform/workspace/github')
        self.assertEqual(response.status_code,200)
        self.assertNotIn(b'secret-do-not-render',response.data)
        self.assertIn(b'another-org',response.data)
        self.assertEqual(client.post('/terraform/workspace/github',data={'owner':'changed'}).status_code,400)
        self.assertEqual(client.get('/terraform/workspace/submit-draft/'+self.draft).status_code,200)

if __name__ == '__main__':
    unittest.main()
