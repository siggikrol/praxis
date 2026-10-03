import unittest

import hcl2

from modules.terraform_module_builder import draft_wrappers


class DraftWrapperTests(unittest.TestCase):
    def setUp(self):
        self.files={
            'variables.tf': '''
variable "REGION" {
  type = string
  validation {
    condition = length(var.REGION) > 0
    error_message = "Choose a region."
  }
}
variable "TAGS" {
  type = map(string)
  default = {}
}
''',
            'providers.tf': '''provider "aws" { region = var.REGION }''',
            'versions.tf': '''terraform { required_providers { aws = { source = "hashicorp/aws", version = "~> 5.0" } } }''',
            'main.tf': '''module "vpc" {
  source = "terraform-aws-modules/vpc/aws"
  version = "5.7.0"
}''',
            'outputs.tf': '''output "vpc_id" {
  description = "VPC ID"
  value = module.vpc.vpc_id
}''',
        }

    def release(self, **changes):
        release = {
            'status': 'published',
            'version': '1.0.0',
            'tag': 'v1.0.0',
            'repository': 'acme/pds-network',
            'repository_url': 'https://github.com/acme/pds-network',
            'commit_sha': 'a' * 40,
            'draft_revision': 4,
        }
        release.update(changes)
        return release

    def test_inspects_child_modules_and_builds_complete_deployment_wrapper(self):
        metadata=draft_wrappers.interface(self.files)
        self.assertEqual(metadata['dependencies'][0]['source'],'terraform-aws-modules/vpc/aws')
        self.assertEqual([item['name'] for item in metadata['inputs']],['REGION','TAGS'])
        files,warnings,generated=draft_wrappers.build_released(
            self.files,'network_wrapper',self.release(),'pds_network')
        self.assertFalse(warnings)
        self.assertIn('module "pds_network"',files['main.tf'])
        self.assertIn('git::https://github.com/acme/pds-network.git?ref=v1.0.0',files['main.tf'])
        self.assertNotIn('version =',files['main.tf'])
        self.assertIn('TAGS = var.TAGS',files['main.tf'])
        self.assertIn('validation {',files['variables.tf'])
        self.assertIn('region = var.REGION',files['providers.tf'])
        self.assertEqual(generated['wrapper_module_name'],'pds_network')
        self.assertEqual(hcl2.loads(files['outputs.tf'])['output'][0]['vpc_id']['value'],'${module.pds_network.vpc_id}')
        self.assertIn('terraform-aws-modules/vpc/aws',self.files['main.tf'])
        self.assertNotIn('terraform-aws-modules/vpc/aws',files['main.tf'])

    def test_updates_only_the_selected_module_version(self):
        files=dict(self.files)
        files['other.tf']='''module "logs" {\n  source = "acme/logs/aws"\n  version = "2.0.0"\n}\n'''
        updated,version=draft_wrappers.update_module_version(files,'vpc','5.8.1')
        self.assertEqual(version,'5.8.1')
        self.assertIn('version = "5.8.1"',updated['main.tf'])
        self.assertEqual(updated['other.tf'],files['other.tf'])
        with self.assertRaises(ValueError):
            draft_wrappers.update_module_version(files,'vpc','latest')

    def test_only_accepts_immutable_praxis_managed_releases(self):
        for changes in (
                {'status': 'pending'},
                {'tag': 'main'},
                {'tag': 'v1.0.1'},
                {'commit_sha': ''},
                {'repository_url': 'https://example.com/acme/pds-network'},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'invalid'):
                draft_wrappers.build_released(
                    self.files,'network_wrapper',self.release(**changes),'pds_network')


if __name__ == '__main__':
    unittest.main()
