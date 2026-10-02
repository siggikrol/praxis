import json
import unittest
from unittest.mock import patch
from modules.terraform_stacks import releases


class ReleaseSetupTests(unittest.TestCase):
    def test_seeds_existing_version_and_requires_successful_main_validation(self):
        with patch.object(releases.github,'head',return_value=('main','a'*40)), patch.object(releases.github,'request',return_value={'tree':[]}), patch.object(releases.github,'versions',return_value=[{'tag':'v1.2.3'},{'tag':'v2.0.0'},{'tag':'v3.0.0-rc1'}]):
            files=releases.setup_files('example/module')
        self.assertEqual(json.loads(files['.release-please-manifest.json']),{'.':'2.0.0'})
        self.assertEqual(files['version.txt'],'2.0.0\n')
        import yaml
        workflow=yaml.safe_load(files['.github/workflows/praxis-module.yml'])
        job=workflow['jobs']['release']
        self.assertEqual(job['needs'],'validate')
        self.assertIn("github.event_name == 'push'",job['if'])
        self.assertIn('refs/heads/main',job['if'])
        self.assertEqual(workflow['permissions'],{'contents':'read'})
        self.assertNotIn('auto-merge',files['.github/workflows/praxis-module.yml'])

    def test_does_not_overwrite_existing_release_files(self):
        with patch.object(releases.github,'head',return_value=('main','a'*40)), patch.object(releases.github,'request',return_value={'tree':[{'path':'version.txt'}]}):
            with self.assertRaisesRegex(ValueError,'manual review'):
                releases.setup_files('example/module')

    def test_install_reuses_existing_setup_pr(self):
        previous={'number':3,'url':'https://github.com/example/module/pull/3'}
        with patch.object(releases.github,'pull_request') as create:
            self.assertEqual(releases.install({'data':{'release_workflow_pr':previous}}),previous)
            create.assert_not_called()

    def test_submission_preserves_complete_existing_release_setup(self):
        paths = ['.github/workflows/praxis-module.yml', 'release-please-config.json',
                 '.release-please-manifest.json', 'version.txt']
        with patch.object(releases.github, 'head', return_value=('main', 'a'*40)), patch.object(releases.github, 'request', return_value={'tree': [{'path': p} for p in paths]}), patch.object(releases.github, 'versions') as versions:
            self.assertEqual(releases.setup_files('example/module', preserve_existing=True), {})
            versions.assert_not_called()

    def test_submission_refuses_partial_release_setup(self):
        with patch.object(releases.github, 'head', return_value=('main', 'a'*40)), patch.object(releases.github, 'request', return_value={'tree': [{'path': '.release-please-manifest.json'}]}):
            with self.assertRaisesRegex(ValueError, 'manual review'):
                releases.setup_files('example/module', preserve_existing=True)

    def test_format_failure_shows_diff_summary_and_still_fails(self):
        import os
        import subprocess
        import tempfile
        from pathlib import Path
        import yaml
        workflow = yaml.safe_load((Path(releases.__file__).parent / 'workflows/module/.github/workflows/praxis-module.yml').read_text())
        steps = workflow['jobs']['validate']['steps']
        step = next(s for s in steps if s.get('id') == 'fmt')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tofu = root / 'tofu'
            tofu.write_text('#!/bin/sh\necho "main.tf: required formatting diff"\nexit 3\n')
            tofu.chmod(0o755)
            summary = root / 'summary'
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step['run']], env=dict(os.environ, PATH=folder+os.pathsep+os.environ['PATH'], RUNNER_TEMP=folder, GITHUB_STEP_SUMMARY=str(summary)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 3)
            self.assertIn('::error title=', result.stdout)
            self.assertIn('main.tf: required formatting diff', summary.read_text())
            self.assertIn('tofu fmt -recursive', summary.read_text())
        self.assertEqual(steps[-1]['if'], 'always()')
