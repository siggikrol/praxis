"""Exercise module configurations in separate processes, with isolated storage."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DisabledModulesTests(unittest.TestCase):
    def run_app(self, enabled, checks):
        with tempfile.TemporaryDirectory(prefix="praxis-modules-") as directory:
            env = dict(os.environ, FLASK_SECRET="isolated-test", BASIC_USER="test", BASIC_PASSWORD="test-password",
                       FLASK_SECURE_COOKIES="0", PS_VALIDATE_CACHES_ON_STARTUP="0",
                       PS_AWS_CATALOG_REFRESHER_ENABLED="0", PS_PC_SOURCE_REFRESHER_ENABLED="0",
                       PS_RELEASES_PROVIDER="disabled", AWS_EC2_METADATA_DISABLED="true",
                       PRAXIS_TERRAFORM_DB=directory+"/foundation.sqlite3", PRAXIS_TERRAFORM_MODULE_BUILDER_DB=directory+"/drafts.sqlite3",
                       SESSION_FILE_DIR=directory+"/sessions", PS_ACTIVE_SESSIONS_REGISTRY=directory+"/active.json",
                       PS_WIZARD_WORKSPACES_DB_PATH=directory+"/workspaces.sqlite3",
                       PS_ENVIRONMENT_READINESS_DB=directory+"/readiness.sqlite3", PYTHONDONTWRITEBYTECODE="1")
            if enabled is None:
                env.pop("ENABLED_MODULES", None)
            else:
                env["ENABLED_MODULES"] = enabled
            script = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
with patch("boto3.session.Session.client") as aws:
    from app import app
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    client = app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 302
    assert client.get("/auth/login").status_code == 200
    assert client.post("/auth/login", data={"username":"test", "password":"test-password"}).status_code == 302
    response = client.get("/")
    assert response.status_code == 200, response.status_code
    assert client.get("/info").status_code == 200
    assert client.get("/tools").status_code == 200
'''
            script += "\n".join("    "+line if line else "" for line in checks.splitlines())
            script += '\n    aws.assert_not_called()\n    assert not Path(os.environ["PS_ENVIRONMENT_READINESS_DB"]).exists()\n'
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                    env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:]+result.stderr[-3000:])

    def test_empty_and_unset_load_only_core(self):
        for value in ("", None):
            with self.subTest(enabled=value):
                self.run_app(value, '''assert set(app.blueprints) == {"auth"}, app.blueprints
assert b"No modules are enabled" in response.data
assert b"Create Environment" not in response.data
assert b"Readiness Gate" not in response.data
assert client.get("/wizards").status_code == 404
assert client.get("/terraform").status_code == 404
assert not any(name.startswith("modules.") for name in sys.modules)
assert "services.aws_instance_types.refresher" not in sys.modules''')

    def test_builder_does_not_load_infrastructure_services(self):
        self.run_app("terraform_module_builder", '''assert set(app.blueprints) == {"auth", "terraform_module_builder"}
assert b"Terraform Module Builder" in response.data
assert client.get("/terraform").status_code == 200
assert "services.aws_instance_types.refresher" not in sys.modules
assert "services.cache_startup_validator" not in sys.modules''')

    def test_selected_module_with_readiness_disabled(self):
        self.run_app("status", '''assert "status" in app.blueprints
assert "environment_readiness" not in app.blueprints
assert "single-vpc_wizard" not in app.blueprints
assert b"Status &amp; Diagnostics" in response.data
assert b"Readiness Gate" not in response.data''')

    def test_saved_wizard_with_readiness_disabled(self):
        self.run_app("wizard_single_vpc", '''assert "single-vpc_wizard" in app.blueprints
assert "environment_readiness" not in app.blueprints
from services.wizard_workspaces import create_workspace
create_workspace(owner="test", env_slug="single-vpc", readiness_setup_id="old-setup")
create_workspace(owner="test", env_slug="multi-vpc")
assert client.get("/wizards").status_code == 200
assert client.get("/single-vpc/wizard/project_settings").status_code == 200
assert client.get("/wizards/workspaces/missing/resume").status_code == 404''')


    def test_draft_git_submission_visible_for_parameterized_endpoint(self):
        self.run_app("terraform_module_builder,terraform_stacks", '''
from modules.terraform_module_builder import store
from flask import session
with client.session_transaction() as signed_in:
    user = str(signed_in['user'])
key = store.create(user, 'draft-vpc', {'source': {'address':'acme/vpc/aws','version':'1','mode':'wrapper'}, 'files': {'main.tf':''}})
assert app.jinja_env.globals['has_endpoint']('terraform_stacks.submit_draft')
assert not app.jinja_env.globals['has_endpoint']('missing.endpoint')
for path in ['/modules/', '/modules/?view=drafts', '/modules/drafts/'+key]:
    page = client.get(path)
    assert page.status_code == 200
    assert ('/terraform/workspace/submit-draft/'+key).encode() in page.data, path
    assert b'Submit to Git' in page.data, path
assert client.get('/terraform/workspace/submit-draft/'+key).status_code == 200
''')
