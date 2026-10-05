import os
import tempfile
import unittest
from unittest.mock import patch

from modules.terraform_stacks import documentation_cache, store


class DocumentationCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'PRAXIS_TERRAFORM_DB': self.temp.name + '/catalog.sqlite3',
            'PRAXIS_MODULE_DOC_CACHE_REFRESH_SECONDS': '86400',
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def module(self, owner, name):
        key = store.save(owner, 'module', name, {
            'repository': f'example/{name}',
            'status': 'Approved',
            'versions': [{'tag': 'v1.0.0', 'commit': 'a' * 40}],
        })
        return store.get(owner, key)

    def test_refreshes_every_catalog_owners_modules_and_continues_after_failure(self):
        alice = self.module('alice', 'network')
        bob = self.module('bob', 'database')
        with patch.object(
                documentation_cache.documentation, 'load',
                side_effect=[{'selected': {'tag': 'v1.0.0'}}, ValueError('unavailable')]
        ) as load, patch.object(documentation_cache.logger, 'exception') as log_error:
            result = documentation_cache.refresh_all()
        self.assertEqual([call.args[0]['id'] for call in load.call_args_list], [alice['id'], bob['id']])
        self.assertEqual(result, {
            'modules': 2,
            'warmed': 1,
            'without_releases': 0,
            'failed': 1,
        })
        log_error.assert_called_once()

    def test_database_lease_runs_once_per_24_hours(self):
        result = {'modules': 0, 'warmed': 0, 'without_releases': 0, 'failed': 0}
        now = 200_000
        with patch.object(documentation_cache, 'refresh_all', return_value=result) as refresh:
            self.assertEqual(documentation_cache.refresh_if_due(now), result)
            self.assertIsNone(documentation_cache.refresh_if_due(now + 60))
            self.assertEqual(documentation_cache.refresh_if_due(now + 86_401), result)
        self.assertEqual(refresh.call_count, 2)


if __name__ == '__main__':
    unittest.main()
