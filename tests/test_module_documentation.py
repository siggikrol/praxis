import os
import tempfile
import unittest
from unittest.mock import patch
from modules.terraform_stacks import documentation as docs

class DocumentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'PRAXIS_TERRAFORM_DB': self.temp.name + '/catalog.sqlite3',
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_wrapper_interface_and_safe_markdown(self):
        inputs, outputs, warnings = docs.describe({'variables.tf': 'variable "region" { type = string }\nvariable "enabled" { type = bool\n default = false }\nvariable "secret" { sensitive = true\n default = "do-not-show" }', 'outputs.tf': 'output "id" { description = "Identifier"\n value = module.upstream.id }'})
        self.assertTrue(inputs[0]['required'])
        self.assertEqual(inputs[0]['name'], 'region')
        self.assertFalse(next(v for v in inputs if v['name']=='enabled')['required'])
        self.assertNotIn('do-not-show', str(inputs))
        self.assertEqual(outputs[0]['description'], 'Identifier')
        html = str(docs.markdown('# Hello\n<script>alert(1)</script>\n[x](javascript:alert(1))'))
        self.assertIn('<h1>Hello</h1>', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('href="javascript:', html)
        self.assertEqual(warnings, [])

    def test_selected_version_is_pinned_and_never_reads_draft_or_main(self):
        sha='a'*40
        item={'owner':'alice','name':'network','data':{'repository':'example/network','versions':[{'tag':'v1.0.0','commit':sha}]}}
        with patch.object(docs.github, 'versions') as versions, patch.object(docs.github, 'request', return_value={'tree':[{'type':'blob','path':'README.md','size':10},{'type':'blob','path':'variables.tf','size':20}]}) as tree, patch.object(docs.github, 'contents', side_effect=['# Released docs','variable "region" {type=string}']) as read:
            result=docs.load(item,'v1.0.0')
        self.assertTrue(all(c.args[2]==sha for c in read.call_args_list))
        self.assertIn('?ref='+sha, result['usage'])
        self.assertIn('region = var.region', result['usage'])
        self.assertNotIn('version =', result['usage'])
        versions.assert_not_called()
        tree.assert_called_once_with(
            'GET', '/repos/example/network/git/trees/' + sha,
            catalog_owner='alice',
        )
        self.assertTrue(all(c.kwargs['catalog_owner'] == 'alice' for c in read.call_args_list))

        with patch.object(docs.github, 'request') as tree, patch.object(docs.github, 'contents') as read:
            cached=docs.load(item,'v1.0.0')
        self.assertEqual(cached['usage'], result['usage'])
        self.assertEqual(str(cached['readme']), str(result['readme']))
        tree.assert_not_called()
        read.assert_not_called()

    def test_cache_is_isolated_by_catalog_owner(self):
        sha='b'*40
        release={'tag':'v2.0.0','commit':sha}
        tree={'tree':[{'type':'blob','path':'README.md','size':10}]}
        alice={'owner':'alice','name':'network','data':{'repository':'example/private','versions':[release]}}
        bob={'owner':'bob','name':'network','data':{'repository':'example/private','versions':[release]}}
        with patch.object(docs.github, 'request', return_value=tree) as request, patch.object(docs.github, 'contents', side_effect=['Alice docs','Bob docs']) as read:
            self.assertIn('Alice docs', str(docs.load(alice)['readme']))
            self.assertIn('Bob docs', str(docs.load(bob)['readme']))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(read.call_count, 2)

    def test_no_releases_has_no_usage(self):
        with patch.object(docs.github, 'versions', return_value=[]), patch.object(docs.github, 'contents') as read:
            result=docs.load({'data':{'repository':'example/network'}})
        self.assertIsNone(result['selected'])
        self.assertEqual(result['usage'],'')
        read.assert_not_called()
