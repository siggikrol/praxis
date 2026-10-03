import unittest

from modules.terraform_module_builder import mock_setup


class MockSetupTests(unittest.TestCase):
    def test_prefers_example_inputs_and_builds_referenced_data_defaults(self):
        files={
            'variables.tf': '''
variable "AWS_REGION" { type = string }
variable "CIDR_BLOCK" { type = string }
variable "FILTER_AZ_ZONE_IDS" { type = list(string) }
variable "OPTIONAL" {
  type = bool
  default = false
}
''',
            'data.tf': '''data "aws_availability_zones" "available" { state = "available" }''',
            'outputs.tf': '''
output "zones" { value = data.aws_availability_zones.available.names }
output "constant" { value = "known" }
''',
            'examples/default/main.tf': '''
module "network" {
  source = "../.."
  AWS_REGION = "eu-west-1"
  CIDR_BLOCK = "10.20.0.0/16"
  FILTER_AZ_ZONE_IDS = ["euw1-az1", "euw1-az2", "euw1-az3"]
}
''',
        }
        result=mock_setup.generate(files)
        self.assertEqual(result['example'],'examples/default/main.tf')
        self.assertEqual(result['variables']['CIDR_BLOCK'],'10.20.0.0/16')
        self.assertNotIn('OPTIONAL',result['variables'])
        self.assertEqual(result['data_defaults']['aws_availability_zones']['names'],
                         ['eu-west-1a','eu-west-1b','eu-west-1c'])
        self.assertEqual(result['expected_outputs'],{'constant':'known'})

    def test_infers_only_missing_required_values(self):
        result=mock_setup.generate({'variables.tf': '''
variable "CUSTOMER" { type = string }
variable "PRIVATE_SUBNETS" { type = list(string) }
variable "REPLICAS" { type = number }
variable "CONFIG" {
  type = object({ name = string, ports = list(number), note = optional(string) })
}
variable "ENABLED" {
  type = bool
  default = true
}
'''})
        self.assertEqual(result['variables']['CUSTOMER'],'example-customer')
        self.assertEqual(result['variables']['PRIVATE_SUBNETS'],
                         ['10.0.1.0/24','10.0.2.0/24','10.0.3.0/24'])
        self.assertEqual(result['variables']['REPLICAS'],1)
        self.assertEqual(result['variables']['CONFIG'],{'name':'example-name','ports':[1]})
        self.assertNotIn('ENABLED',result['variables'])
        self.assertEqual(set(result['generated_variables']),{'CUSTOMER','PRIVATE_SUBNETS','REPLICAS','CONFIG'})


if __name__ == '__main__':
    unittest.main()
