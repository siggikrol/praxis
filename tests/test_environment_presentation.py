import unittest
from engine.wizards.presentation import environment_review, ordered_steps, stage_for
from engine.wizards.factory.transforms import _transform_project_settings
from services.wizard_workspaces import _derive_title


class EnvironmentPresentationTests(unittest.TestCase):
    def test_metadata_does_not_change_generated_project_settings(self):
        original = {"environment_type": ["prod"], "environment_suffixes": '{"prod":"blue"}', "deployments": ["common"]}
        enriched = dict(original, environment_name="Production", environment_description="Services", environment_owner="Platform")
        self.assertEqual(_transform_project_settings(enriched), _transform_project_settings(original))
        self.assertEqual(enriched["environment_name"], "Production")

    def test_grouping_retains_every_existing_form(self):
        refs = [(key, object()) for key in ["project_settings", "repository_settings", "pc_source", "common", "vpc", "eks", "aurora"]]
        grouped = ordered_steps(refs)
        self.assertEqual({id(form) for _, form in refs}, {id(form) for _, form in grouped})
        self.assertEqual([stage_for(key) for key, _ in grouped], ["Environment", "Cloud", "Architecture", "Architecture", "Capabilities", "Capabilities", "Capabilities"])

    def test_review_reports_saved_values_without_claiming_deployment(self):
        state = {"multi-vpc:project_settings": {"environment_name": "Production", "environment_type": ["prod"], "environment_owner": "Platform"},
                 "multi-vpc:common": {"aws_region": "eu-west-1"},
                 "multi-vpc:aurora": {"engine": "aurora-postgresql"}, "multi-vpc:eks": {"prod": {"cluster_version": "1.32"}}}
        review = environment_review("multi-vpc", state)
        self.assertEqual(review["name"], "Production")
        self.assertEqual(review["types"], ["Production"])
        self.assertEqual(review["region"], "eu-west-1")
        self.assertEqual(dict(review["capabilities"])["Relational Database"], "PostgreSQL")
        self.assertEqual(dict(review["capabilities"])["Cache"], "Not configured")
        self.assertEqual(_derive_title("multi-vpc", state), "Production")

    def test_existing_workspaces_keep_fallback_names(self):
        state = {"single-vpc:common": {"environment": "acme-ctlst", "support_organization": "Platform"}}
        self.assertEqual(environment_review("single-vpc", state)["name"], "acme-ctlst")
        self.assertEqual(_derive_title("single-vpc", state), "acme-ctlst")
