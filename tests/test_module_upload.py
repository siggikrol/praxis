import io
import stat
import tempfile
import os
import unittest
import zipfile
from unittest.mock import patch
from flask import Flask
from flask_wtf import CSRFProtect
from modules.terraform_module_builder.uploads import import_zip
from modules.terraform_module_builder.blueprint import bp
from modules.terraform_module_builder import store


def archive(files):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as zip:
        for name, content in files:
            zip.writestr(name,content)
    return stream.getvalue()


class UploadTests(unittest.TestCase):
    def test_folder_and_submodules_preserved(self):
        files,warnings=import_zip(archive([('my-module/main.tf','module "child" { source = "./modules/child" }'),('my-module/modules/child/main.tf',''),('my-module/README.md','Hello'),('my-module/.terraform/provider','binary'),('my-module/terraform.tfstate','secret'),('my-module/.env','secret')]))
        self.assertIn('main.tf',files)
        self.assertIn('modules/child/main.tf',files)
        self.assertEqual(len(files),3)
        self.assertTrue(warnings)

    def test_rejects_unsafe_and_invalid_archives(self):
        link=zipfile.ZipInfo('module/link')
        link.create_system=3
        link.external_attr=(stat.S_IFLNK|0o777)<<16
        for raw in [b'not a zip',archive([('../main.tf','')]),archive([('/main.tf','')]),archive([(link,'../other')]),archive([('README.md','No code')]),archive([('main.tf','bad = {')]),archive([('main.tf',b'\xff')]),archive([('main.tf',''),('large.txt','x'*2_000_001)])]:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                import_zip(raw)

    def test_upload_creates_owned_draft_and_csrf_protected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,PRAXIS_TERRAFORM_MODULE_BUILDER_DB=tmp+'/drafts.sqlite3'):
            app=Flask(__name__)
            app.secret_key='test'
            app.register_blueprint(bp)
            CSRFProtect(app)
            client=app.test_client()
            with client.session_transaction() as session:
                session['user']='alice'
            data=lambda:{'name':'my-module','provider':'google','archive':(io.BytesIO(archive([('main.tf','variable "name" { type = string }')])),'module.zip')}
            self.assertEqual(client.post('/modules/upload',data=data()).status_code,400)
            app.config['WTF_CSRF_ENABLED']=False
            response=client.post('/modules/upload',data=data())
            self.assertEqual(response.status_code,302)
            key=response.location.rsplit('/',1)[-1]
            draft=store.get('alice',key)
            self.assertEqual(draft['provider'],'google')
            self.assertEqual(draft['document']['source']['mode'],'upload')
            self.assertIsNone(store.get('bob',key))
            self.assertIn('main.tf',draft['document']['files'])

    def test_editor_adds_and_removes_files_in_new_revision(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, PRAXIS_TERRAFORM_MODULE_BUILDER_DB=tmp+'/drafts.sqlite3'):
            app=Flask(__name__)
            app.secret_key='test'
            app.register_blueprint(bp)
            CSRFProtect(app)
            key=store.create('alice','module',{'files':{'main.tf':'','old.tf':''}})
            client=app.test_client()
            with client.session_transaction() as session:
                session['user']='alice'
            data={'name':'module','revision':'1','file_0':'','remove_1':'yes','new_file_name':'outputs.tf','new_file_content':'output "name" { value = "demo" }'}
            self.assertEqual(client.post('/modules/drafts/'+key,data=data).status_code,400)
            app.config['WTF_CSRF_ENABLED']=False
            self.assertEqual(client.post('/modules/drafts/'+key,data=data).status_code,302)
            draft=store.get('alice',key)
            self.assertEqual(draft['revision'],2)
            self.assertEqual(set(draft['document']['files']),{'main.tf','outputs.tf'})
            with client.session_transaction() as session:
                session['user']='bob'
            self.assertEqual(client.post('/modules/drafts/'+key,data=data).status_code,404)
