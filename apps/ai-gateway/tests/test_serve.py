import unittest
from unittest.mock import patch

from foodtech_ai_gateway import serve


class GatewayStartupTests(unittest.TestCase):
    def test_authentication_table_is_required_before_listener_starts(self):
        environment = {"FOODTECH_GATEWAY_SERVER_CERT": "cert", "FOODTECH_GATEWAY_SERVER_KEY": "key",
                       "FOODTECH_GATEWAY_BUSINESS_CA": "ca", "FOODTECH_AI_REGISTRY_TABLE": "registry"}
        with patch.dict("os.environ", environment, clear=True), \
                patch("os.path.isfile", return_value=True), patch.object(serve.uvicorn, "run") as run:
            with self.assertRaisesRegex(ValueError, "authentication table is required"):
                serve.main()
            run.assert_not_called()

    def test_configured_tables_allow_listener_start(self):
        environment = {"FOODTECH_GATEWAY_SERVER_CERT": "cert", "FOODTECH_GATEWAY_SERVER_KEY": "key",
                       "FOODTECH_GATEWAY_BUSINESS_CA": "ca", "FOODTECH_AI_REGISTRY_TABLE": "registry",
                       "FOODTECH_AUTH_TABLE": "auth"}
        with patch.dict("os.environ", environment, clear=True), \
                patch("os.path.isfile", return_value=True), patch.object(serve.uvicorn, "run") as run:
            serve.main()
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
