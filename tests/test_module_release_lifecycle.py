import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from modules.terraform_module_builder import draft_wrappers, release_lifecycle
from modules.terraform_module_builder import store as drafts
from modules.terraform_stacks import github, releases


FILES = {
    'variables.tf': 'variable "name" { type = string }\n',
    'main.tf': 'output "name" { value = var.name }\n',
}


class ModuleReleaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'PRAXIS_TERRAFORM_DB': self.temp.name + '/catalog.sqlite3',
            'PRAXIS_TERRAFORM_MODULE_BUILDER_DB': self.temp.name + '/drafts.sqlite3',
        })
        self.env.start()
        self.root = drafts.create('alice', 'platform_vpc', {
            'files': FILES,
            'warnings': [],
            'source': {
                'address': 'terraform-aws-modules/vpc/aws',
                'version': '5.7.0',
                'mode': 'wrapper',
                'provider': 'aws',
            },
        })

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def publish_record(self, draft_id, version, repository='acme/platform-vpc', layer='organization-root'):
        draft = drafts.get('alice', draft_id)
        item = drafts.reserve_release(
            'alice', draft, layer, version, 'v' + version, repository,
            'terraform-aws-modules/vpc/aws', '5.7.0',
        )
        return drafts.update_release('alice', item['id'], 'published', 'a' * 40)

    def create_wrapper(self, release):
        root = drafts.get('alice', self.root)
        files, warnings, metadata = draft_wrappers.build_released(
            release['source_metadata']['files'], 'deployment', release, root['name'])
        return drafts.create('alice', 'deployment', {
            'files': files, 'warnings': warnings,
            'source': {
                'mode': 'draft-wrapper', 'provider': 'aws',
                'address': draft_wrappers.release_source(release),
                'version': release['version'], 'repository': release['repository'],
                'repository_url': release['repository_url'],
                'module_name': metadata['wrapper_module_name'],
                'dependencies': metadata['dependencies'],
                'wrapped_draft': {'id': self.root, 'name': root['name'],
                                  'revision': release['draft_revision']},
                'wrapped_release': {'id': release['id'], 'version': release['version'],
                    'tag': release['tag'], 'commit': release['commit_sha'],
                    'repository': release['repository'],
                    'repository_url': release['repository_url']},
            },
        })

    def test_promotes_passing_revision_with_semver_tag_and_provenance(self):
        passed = {'status': 'passed', 'revision': 1, 'mode': 'mock',
                  'result': {'check_suite': 2}}
        with patch.object(releases.testing, 'latest', return_value=passed), \
                patch.object(github, 'connected', return_value=True), \
                patch.object(github, 'head', return_value=('main', 'b' * 40)), \
                patch.object(github, 'versions', return_value=[{'tag': 'v1.2.3', 'commit': 'b' * 40}]), \
                patch.object(github, 'commit_files', return_value='c' * 40) as commit, \
                patch.object(github, 'create_tag') as tag:
            release = releases.promote('alice', self.root, 1, 'acme/platform-vpc', 'feat')
        self.assertEqual(release['version'], '1.3.0')
        self.assertEqual(release['tag'], 'v1.3.0')
        self.assertEqual(release['commit_sha'], 'c' * 40)
        self.assertEqual(release['repository_url'], 'https://github.com/acme/platform-vpc')
        self.assertEqual(release['upstream_source'], 'terraform-aws-modules/vpc/aws')
        self.assertEqual(release['upstream_version'], '5.7.0')
        self.assertEqual(release['draft_id'], self.root)
        self.assertEqual(release['draft_revision'], 1)
        self.assertEqual(release['source_metadata']['files'], FILES)
        commit.assert_called_once()
        tag.assert_called_once_with('acme/platform-vpc', 'v1.3.0', 'c' * 40)

    def test_existing_release_metadata_gains_repository_url(self):
        path = self.temp.name + '/drafts.sqlite3'
        next_draft = drafts.get('alice', self.root)
        next_draft['revision'] = 2
        with sqlite3.connect(path) as database:
            database.execute('DROP TABLE module_releases')
            database.execute('''CREATE TABLE module_releases (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, draft_id TEXT NOT NULL,
                draft_revision INTEGER NOT NULL, layer TEXT NOT NULL, status TEXT NOT NULL,
                version TEXT NOT NULL, tag TEXT NOT NULL, repository TEXT NOT NULL,
                commit_sha TEXT NOT NULL DEFAULT '', upstream_source TEXT NOT NULL,
                upstream_version TEXT NOT NULL, upstream_release_id TEXT,
                source_metadata TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(owner,draft_id,draft_revision), UNIQUE(owner,repository,tag))''')
            database.execute('''INSERT INTO module_releases(
                id,owner,draft_id,draft_revision,layer,status,version,tag,repository,
                commit_sha,upstream_source,upstream_version,source_metadata,snapshot_sha256,
                created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                'legacy-release', 'alice', self.root, 1, 'organization-root', 'published',
                '1.0.0', 'v1.0.0', 'acme/platform-vpc', 'a' * 40,
                'terraform-aws-modules/vpc/aws', '5.7.0', '{}', 'snapshot', 1, 1,
            ))
        reserved = drafts.reserve_release(
            'alice', next_draft, 'organization-root', '1.1.0', 'v1.1.0',
            'acme/platform-vpc', 'terraform-aws-modules/vpc/aws', '5.7.0',
        )
        self.assertEqual(reserved['repository_url'], 'https://github.com/acme/platform-vpc')
        release = drafts.get_release('alice', 'legacy-release')
        self.assertEqual(release['repository_url'], 'https://github.com/acme/platform-vpc')
        with sqlite3.connect(path) as database:
            stored = database.execute(
                'SELECT repository_url FROM module_releases WHERE id=?', ('legacy-release',)
            ).fetchone()[0]
        self.assertEqual(stored, 'https://github.com/acme/platform-vpc')

    def test_unpublished_revision_cannot_be_released_or_wrapped(self):
        with patch.object(releases.testing, 'latest', return_value=None), \
                patch.object(github, 'commit_files') as commit:
            with self.assertRaisesRegex(ValueError, 'successful mock test'):
                releases.promote('alice', self.root, 1, 'acme/platform-vpc')
            commit.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'invalid'):
            draft_wrappers.build_released(FILES, 'deployment', {}, 'platform_vpc')

    def test_validation_only_cannot_release_updated_root_revision(self):
        validation = {'status': 'passed', 'revision': 1, 'mode': 'validate',
                      'result': {'check_suite': 2}}
        with patch.object(releases.testing, 'latest', return_value=validation), \
                patch.object(github, 'commit_files') as commit:
            with self.assertRaisesRegex(ValueError, 'successful mock test'):
                releases.promote('alice', self.root, 1, 'acme/platform-vpc')
            commit.assert_not_called()

    def test_wrapper_upgrade_creates_revision_and_restarts_saved_test(self):
        first = self.publish_record(self.root, '0.1.0')
        root = drafts.get('alice', self.root)
        wrapper = self.create_wrapper(first)
        self.assertTrue(drafts.update('alice', self.root, 1, root['document']))
        second = self.publish_record(self.root, '0.2.0')
        previous = {'mode': 'mock', 'settings': {'variables': {'name': 'demo'}},
                    'status': 'passed', 'revision': 1, 'result': {'check_suite': 2}}
        with patch.object(release_lifecycle.testing, 'latest', return_value=previous), \
                patch.object(release_lifecycle.testing, 'start', return_value='job') as start:
            updated, job, error = release_lifecycle.upgrade_wrapper(
                'alice', wrapper, 1, second['id'])
        self.assertEqual((job, error), ('job', None))
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(updated['document']['source']['wrapped_release']['id'], second['id'])
        self.assertIn('module "platform_vpc"', updated['document']['files']['main.tf'])
        self.assertIn('?ref=v0.2.0', updated['document']['files']['main.tf'])
        self.assertNotIn('version =', updated['document']['files']['main.tf'])
        start.assert_called_once()
        self.assertEqual(start.call_args.args[1]['revision'], 2)
        self.assertEqual(start.call_args.args[2:], ('mock', previous['settings']))

    def test_wrapper_upgrade_always_retests_wiring_with_mock_plan(self):
        first = self.publish_record(self.root, '0.1.0')
        root = drafts.get('alice', self.root)
        wrapper = self.create_wrapper(first)
        self.assertTrue(drafts.update('alice', self.root, 1, root['document']))
        second = self.publish_record(self.root, '0.2.0')
        previous = {'mode': 'validate', 'settings': {'variables': {'name': 'demo'}},
                    'status': 'passed', 'revision': 1, 'result': {'check_suite': 2}}
        with patch.object(release_lifecycle.testing, 'latest', return_value=previous), \
                patch.object(release_lifecycle.testing, 'start', return_value='job') as start:
            release_lifecycle.upgrade_wrapper('alice', wrapper, 1, second['id'])
        self.assertEqual(start.call_args.args[2:], ('mock', previous['settings']))

    def test_releases_tested_wrapper_with_root_release_link(self):
        root_release = self.publish_record(self.root, '1.0.0')
        wrapper = self.create_wrapper(root_release)
        passed = {'status': 'passed', 'revision': 1, 'mode': 'mock',
                  'result': {'check_suite': 2}}
        with patch.object(releases.testing, 'latest', return_value=passed), \
                patch.object(github, 'connected', return_value=True), \
                patch.object(github, 'head', return_value=('main', 'b' * 40)), \
                patch.object(github, 'versions', return_value=[]), \
                patch.object(github, 'commit_files', return_value='d' * 40), \
                patch.object(github, 'create_tag'):
            wrapper_release = releases.promote(
                'alice', wrapper, 1, 'acme/deployment', 'fix')
        self.assertEqual(wrapper_release['layer'], 'deployment-wrapper')
        self.assertEqual(wrapper_release['version'], '0.0.1')
        self.assertEqual(wrapper_release['upstream_release_id'], root_release['id'])
        self.assertEqual(wrapper_release['upstream_source'],
                         'git::https://github.com/acme/platform-vpc.git')
        self.assertEqual(wrapper_release['upstream_version'], '1.0.0')

    def test_wrapper_release_rejects_parent_without_root_mock_evidence(self):
        root_release = self.publish_record(self.root, '1.0.0')
        wrapper = drafts.get('alice', self.create_wrapper(root_release))
        with patch.object(releases.testing, 'release_ready', return_value=False):
            with self.assertRaisesRegex(ValueError, 'released root revision'):
                releases._upstream('alice', wrapper)


if __name__ == '__main__':
    unittest.main()
