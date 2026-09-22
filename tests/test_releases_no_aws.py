"""Release imports and disabled listings must not consult AWS credentials."""
import importlib
import os
import unittest
from unittest.mock import patch


class ReleaseConfigurationTests(unittest.TestCase):
    def test_disabled_provider_never_creates_aws_client(self):
        with patch.dict(os.environ, {"PS_RELEASES_PROVIDER": "disabled", "PS_RELEASES_S3_BUCKET": "unused-bucket"}):
            with patch("boto3.session.Session.client") as client:
                service = importlib.import_module("modules.praxisrelease.service")
                service = importlib.reload(service)
                for listing in (service.list_customers, service.list_customer_releases,
                                service.list_all_release_keys, service.list_all_release_objects):
                    self.assertEqual(listing(), [])
                with self.assertRaisesRegex(RuntimeError, "not configured"):
                    service.fetch_release_manifest("unused-bucket", "bundles/example.tar.gz")
                client.assert_not_called()

    def test_s3_client_is_created_only_for_requested_operation(self):
        with patch.dict(os.environ, {"PS_RELEASES_PROVIDER": "s3", "PS_RELEASES_S3_BUCKET": "example-bucket"}):
            with patch("boto3.session.Session.client") as client:
                service = importlib.reload(importlib.import_module("modules.praxisrelease.service"))
                client.assert_not_called()
                client.return_value.get_paginator.return_value.paginate.return_value = []
                self.assertEqual(service.list_customers(), [])
                client.assert_called_once_with("s3")


if __name__ == "__main__":
    unittest.main()
