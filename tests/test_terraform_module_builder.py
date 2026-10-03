import io
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import hcl2
from flask import Flask, session
from flask_wtf import CSRFProtect
from pathlib import Path
from modules.terraform_module_builder import authoring, registry, store
from modules.terraform_module_builder.blueprint import bp

META = {'address':'acme/eks/aws','namespace':'acme','name':'eks','provider':'aws','version':'1.2.3','versions':['1.2.3'], 'registry_url':'https://registry.terraform.io/modules/acme/eks/aws/1.2.3', 'root':{'inputs':[{'name':'name','type':'string','required':True,'description':'Name'}, {'name':'tags','type':'map(string)','required':False,'default':'{}'}], 'outputs':[{'name':'id','description':'ID'}], 'provider_dependencies':[{'name':'aws','source':'hashicorp/aws','version':'>= 6.0','aliases':[]}]},'examples':[{'path':'examples/basic'}]}

class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, PRAXIS_TERRAFORM_MODULE_BUILDER_DB=self.temp.name+'/drafts.sqlite3')
        self.env.start()
    def tearDown(self):
        self.env.stop(); self.temp.cleanup()
    def test_wrapper(self):
        files = authoring.wrapper(META, 'platform-eks', {'tags':('fixed','{"team":"${literal}"}')}, ['id'])
        parsed = hcl2.loads(files['main.tf'])['module'][0]['upstream']
        self.assertEqual(parsed['version'], '1.2.3')
        self.assertIn('$${literal}', files['main.tf'])
        self.assertNotIn('default', files['variables.tf'])
        self.assertTrue(hcl2.loads(files['outputs.tf'])['output'][0]['id']['sensitive'])
        with self.assertRaises(ValueError): authoring.wrapper(META, 'test', {'name':('default','')}, [])
        with self.assertRaises(ValueError): authoring.wrapper(META, 'test', {'name':('fixed','plain text')}, [])
        with self.assertRaises(ValueError): authoring.expression('string\nresource "x" "y" {}')
        with self.assertRaises(ValueError): authoring.validate_files({'../bad.tf':''})
    def test_both_cloud_providers(self):
        import copy
        for provider in ('aws', 'google'):
            metadata = copy.deepcopy(META)
            metadata.update(address=f'acme/cluster/{provider}', provider=provider)
            metadata['root']['provider_dependencies'] = [{'name':provider, 'source':f'hashicorp/{provider}', 'version':'>= 6.0'}]
            with patch.object(registry, '_json', return_value=metadata) as fetch:
                self.assertEqual(registry.details(metadata['address'])['provider'], provider)
                registry.search('cluster', 12, provider)
                self.assertEqual(fetch.call_args.args[1]['provider'], provider)
                self.assertEqual(fetch.call_args.args[1]['offset'], 12)
            files = authoring.wrapper(metadata, 'platform-cluster', {}, [])
            self.assertIn(metadata['address'], files['main.tf'])
            self.assertIn(f'hashicorp/{provider}', files['versions.tf'])
        with self.assertRaises(registry.RegistryError): registry.search('test', provider='azurerm')
        self.assertEqual(registry.module_address('https://registry.terraform.io/modules/terraform-google-modules/network/google/latest'), 'terraform-google-modules/network/google')

    def test_store_ownership_and_revision(self):
        key = store.create('alice','test',{'files':{}})
        self.assertIsNone(store.get('bob',key))
        self.assertFalse(store.update('bob',key,1,{}))
        self.assertTrue(store.update('alice',key,1,{}))
        self.assertFalse(store.update('alice',key,1,{}))
    def archive(self, files):
        out = io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            for name, text in files.items(): z.writestr(name,text)
        return out.getvalue()
    def test_example_rewrites_only_module_sources(self):
        archive = self.archive({'repo/examples/basic/main.tf':'module "eks" {\n source = "../.."\n}\nresource "local_file" "x" {source="./data.txt"}\n', 'repo/examples/basic/data.txt':'hello', 'repo/LICENSE':'license'})
        with patch.object(registry,'_get',side_effect=[(b'',{'X-Terraform-Get':'git::https://github.com/acme/eks?ref=abc123'}),(archive,{})]):
            files, warnings, origin = registry.import_example(META,'examples/basic')
        self.assertIn('source = "acme/eks/aws"', files['main.tf'])
        self.assertIn('source="./data.txt"',files['main.tf'])
        self.assertEqual(files['UPSTREAM-LICENSE'],'license')
        self.assertEqual(origin['revision'],'abc123')
        authoring.validate_files(files)
        archive = self.archive({'repo/../bad.tf':''})
        with patch.object(registry,'_get',side_effect=[(b'',{'X-Terraform-Get':'git::https://github.com/acme/eks?ref=abc123'}),(archive,{})]):
            with self.assertRaises(registry.RegistryError): registry.import_example(META,'examples/basic')
    def test_routes_csrf_save_download_and_conflict(self):
        app=Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1]/'templates'))
        app.secret_key='test'; app.config['TESTING']=True
        CSRFProtect(app)
        app.config['WTF_CSRF_ENABLED']=False
        app.register_blueprint(bp)
        app.add_url_rule('/', 'index', lambda:'home')
        app.add_url_rule('/info', 'info', lambda:'info')
        app.add_url_rule('/terraform', 'terraform', lambda:'terraform')
        app.jinja_env.globals.update(has_endpoint=lambda name:name=='terraform_module_builder.index',home_links=[],csrf_token=lambda:'token')
        client=app.test_client()
        with client.session_transaction() as s: s['user']='alice'
        with patch.object(registry,'details',return_value=META), patch.object(registry,'search',return_value={'modules':[]}):
            for url in ['/modules/','/modules/?q=eks','/modules/?q=network&provider=google','/modules/?view=drafts','/modules/choose?source=acme/eks/aws','/modules/customize?source=acme/eks/aws&mode=wrapper','/modules/customize?source=acme/eks/aws&mode=example']:
                self.assertEqual(client.get(url).status_code,200,url)
            response=client.post('/modules/customize',data={'source':META['address'],'version':'1.2.3','name':'platform-eks','mode':'wrapper'})
        self.assertEqual(response.status_code,302)
        location=response.location
        versions=patch.object(registry,'available_versions',return_value=(['1.3.0','1.2.3'],'1.3.0'))
        versions.start(); self.addCleanup(versions.stop)
        draft_page=client.get(location)
        self.assertEqual(draft_page.status_code,200)
        self.assertIn(b'<select class="form-select form-select-sm font-monospace"',draft_page.data)
        self.assertIn(b'1.3.0 (latest)',draft_page.data)
        key=location.rsplit('/',1)[-1]
        item=store.get('alice',key)
        unreleased=client.get(location+'/wrap')
        self.assertEqual(unreleased.status_code,200)
        self.assertIn(b'platform-eks has not been released.',unreleased.data)
        self.assertIn(b'Test and release the module before creating a deployment wrapper.',unreleased.data)
        self.assertNotIn(b'Published source',unreleased.data)
        self.assertNotIn(b'Registry source',unreleased.data)
        self.assertNotIn(b'Spacelift source',unreleased.data)
        release=store.reserve_release('alice',item,'organization-root','1.2.3','v1.2.3',
            'acme/platform-eks',META['address'],META['version'])
        release=store.update_release('alice',release['id'],'published','a'*40)
        released_page=client.get(location+'/wrap')
        self.assertEqual(released_page.status_code,200)
        for value in (b'Parent module',b'platform-eks',b'Release',b'v1.2.3',
                      b'Source',b'Praxis managed',b'Git tag',b'Upstream',
                      META['address'].encode(),b'Upstream version',b'1.2.3'):
            self.assertIn(value,released_page.data)
        self.assertNotIn(b'Published source',released_page.data)
        self.assertNotIn(b'Registry source',released_page.data)
        self.assertNotIn(b'Spacelift source',released_page.data)
        wrapped=client.post(location+'/wrap',data={'revision':'1','name':'eks_deployment',
            'release_id':release['id']})
        self.assertEqual(wrapped.status_code,302)
        deployment=store.get('alice',wrapped.location.rsplit('/',1)[-1])
        self.assertEqual(deployment['document']['source']['wrapped_draft']['id'],key)
        self.assertEqual(deployment['document']['source']['module_name'],'platform_eks')
        self.assertEqual(deployment['document']['source']['repository_url'],
                         'https://github.com/acme/platform-eks')
        self.assertIn('module "platform_eks"',deployment['document']['files']['main.tf'])
        self.assertIn('git::https://github.com/acme/platform-eks.git?ref=v1.2.3',deployment['document']['files']['main.tf'])
        deployment_location=wrapped.location
        updated=client.post(deployment_location+'/modules/upstream/version',data={'revision':'1','module_version':'1.2.4'})
        self.assertEqual(updated.status_code,400)
        deployment=store.get('alice',deployment_location.rsplit('/',1)[-1])
        self.assertEqual(deployment['revision'],1)
        self.assertEqual(deployment['document']['source']['version'],'1.2.3')
        with patch('modules.terraform_module_builder.testing.start', return_value='job') as start:
            self.assertEqual(client.post(location+'/test',data={'revision':'0','mode':'validate'}).status_code,400)
            start.assert_not_called()
            generated=client.post(location+'/test',data={'revision':'1','mode':'generate'})
            self.assertEqual(generated.status_code,200)
            self.assertIn(b'Mock setup generated',generated.data)
            self.assertIn(b'example-name',generated.data)
            start.assert_not_called()
            self.assertEqual(client.post(location+'/test',data={'revision':'1','mode':'mock','variables':'{"name":"demo"}'}).status_code,302)
            self.assertEqual(start.call_args.args[3]['variables'], {'name':'demo'})
        self.assertEqual(client.get(location+'/tests').status_code,200)
        data={'revision':'1', 'name':'renamed-eks', **{f'file_{i}':v.replace('\n', '\r\n') for i,(k,v) in enumerate(sorted(item['document']['files'].items()))}}
        self.assertEqual(client.post(location,data=data).status_code,302)
        self.assertEqual(client.post(location,data=data).status_code,409)
        self.assertEqual(client.post(location,data=data).status_code,409)
        current=store.get('alice',key)
        latest=store.reserve_release('alice',current,'organization-root','1.3.0','v1.3.0',
            'acme/platform-eks',META['address'],META['version'])
        latest=store.update_release('alice',latest['id'],'published','b'*40)
        releases_page=client.get(location+'/wrap').data.decode()
        self.assertRegex(releases_page,rf'<option value="{latest["id"]}"[^>]*selected')
        self.assertIn(f'<option value="{release["id"]}"',releases_page)
        self.assertIn('v1.3.0 (latest)',releases_page)
        older_wrapper=client.post(location+'/wrap',data={
            'revision':'2','name':'eks_previous_release','release_id':release['id']})
        self.assertEqual(older_wrapper.status_code,302)
        older=store.get('alice',older_wrapper.location.rsplit('/',1)[-1])
        self.assertIn('?ref=v1.2.3',older['document']['files']['main.tf'])
        download=client.get(location+'/download')
        with zipfile.ZipFile(io.BytesIO(download.data)) as z:
            self.assertIn('renamed-eks/praxis-source.json', z.namelist())
        self.assertEqual(store.get('alice', key)['name'], 'renamed-eks')
        self.assertEqual(client.get(location+'/delete').status_code,200)
        self.assertIsNotNone(store.get('alice',key))
        self.assertEqual(client.post(location+'/delete',data={'revision':'1','confirm':'delete'}).status_code,409)
        with client.session_transaction() as s: s['user']='bob'
        self.assertEqual(client.get(location).status_code,404)
        self.assertEqual(client.get(location+'/wrap').status_code,404)
        self.assertEqual(client.get(location+'/download').status_code,404)
        self.assertEqual(client.get(location+'/tests').status_code,404)
        self.assertEqual(client.post(location+'/test',data={'revision':'2','mode':'validate'}).status_code,404)
        self.assertEqual(client.post(location+'/delete',data={'revision':'2','confirm':'delete'}).status_code,404)
        with client.session_transaction() as s: s['user']='alice'
        self.assertEqual(client.post(location+'/delete',data={'revision':'2'}).status_code,400)
        app.config['WTF_CSRF_ENABLED']=True
        self.assertEqual(client.post(location+'/delete',data={'revision':'2','confirm':'delete'}).status_code,400)
        app.config['WTF_CSRF_ENABLED']=False
        self.assertEqual(client.post(location+'/delete',data={'revision':'2','confirm':'delete'}).status_code,302)
        self.assertEqual(client.get(location).status_code,404)
        self.assertEqual(client.get(location+'/download').status_code,404)
        app.config['WTF_CSRF_ENABLED']=True
        self.assertEqual(client.post('/modules/customize',data={}).status_code,400)

    def test_available_versions_puts_latest_first(self):
        metadata=dict(META,version='1.3.0',versions=['1.0.0','invalid','1.2.3','1.3.0'])
        with patch.object(registry,'details',return_value=metadata):
            versions,latest=registry.available_versions(META['address'])
        self.assertEqual(versions,['1.3.0','1.2.3','1.0.0'])
        self.assertEqual(latest,'1.3.0')
