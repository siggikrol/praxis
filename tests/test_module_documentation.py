import unittest
from unittest.mock import patch
from modules.terraform_stacks import documentation as docs

class DocumentationTests(unittest.TestCase):
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
        with patch.object(docs.github, 'versions', return_value=[{'tag':'v1.0.0','commit':sha}]), patch.object(docs.github, 'request', return_value={'tree':[{'type':'blob','path':'README.md','size':10},{'type':'blob','path':'variables.tf','size':20}]}), patch.object(docs.github, 'contents', side_effect=['# Released docs','variable "region" {type=string}']) as read:
            result=docs.load({'name':'network','data':{'repository':'example/network'}},'v1.0.0')
        self.assertTrue(all(c.args[2]==sha for c in read.call_args_list))
        self.assertIn('?ref='+sha, result['usage'])
        self.assertIn('region = var.region', result['usage'])
        self.assertNotIn('version =', result['usage'])

    def test_no_releases_has_no_usage(self):
        with patch.object(docs.github, 'versions', return_value=[]), patch.object(docs.github, 'contents') as read:
            result=docs.load({'data':{'repository':'example/network'}})
        self.assertIsNone(result['selected'])
        self.assertEqual(result['usage'],'')
        read.assert_not_called()
