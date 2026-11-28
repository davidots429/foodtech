import os
import unittest
from unittest.mock import patch

from foodtech_business_api.serve_tonkatsu_business_api import main


class TonkatsuBusinessContainerTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "FOODTECH_TONKATSU_CUSTOMER_INVOCATION_ENABLED": "true",
            "FOODTECH_TONKATSU_GATEWAY_RECORDS_ENABLED": "true",
            "FOODTECH_PRINCIPAL_PROVISIONING_ENABLED": "true",
            "FOODTECH_COGNITO_ISSUER": "https://identity.example/pool",
            "FOODTECH_COGNITO_DOMAIN": "https://login.example",
            "FOODTECH_CUSTOMER_CLIENT_ID": "customer-fixture",
            "FOODTECH_ADMIN_CLIENT_ID": "admin-fixture",
            "FOODTECH_CUSTOMER_CALLBACK": "https://customer.example/callback",
            "FOODTECH_ADMIN_CALLBACK": "https://admin.example/callback",
            "FOODTECH_CUSTOMER_WEB": "https://customer.example",
            "FOODTECH_ADMIN_WEB": "https://admin.example",
            "FOODTECH_AUTH_TABLE": "auth-fixture",
            "FOODTECH_COOKING_TABLE": "cooking-fixture",
        }
        self.run_server = self.enterContext(patch("foodtech_business_api.serve_tonkatsu_business_api.uvicorn.run"))

    def test_serves_the_existing_authentication_boundary_on_loopback_by_default(self):
        with patch.dict(os.environ, self.env, clear=True):
            main()
        self.run_server.assert_called_once_with(
            "foodtech_business_api.main:app", host="127.0.0.1", port=8080, proxy_headers=False, access_log=False,
        )

    def test_missing_private_records_mode_or_invalid_port_never_starts_the_api(self):
        for key, value in (("FOODTECH_TONKATSU_GATEWAY_RECORDS_ENABLED", "false"),
                           ("FOODTECH_TONKATSU_BUSINESS_PORT", "0")):
            with self.subTest(key=key), patch.dict(os.environ, {**self.env, key: value}, clear=True):
                with self.assertRaises(ValueError):
                    main()
        self.run_server.assert_not_called()
