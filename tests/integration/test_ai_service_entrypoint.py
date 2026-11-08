"""Check that ECS TLS injection does not reach the app process as environment values."""

import importlib.util
import os
import pathlib
import stat
import unittest
from unittest.mock import patch


ENTRYPOINT = pathlib.Path(__file__).resolve().parents[2] / "infra/containers/ai_service_entrypoint.py"
SPEC = importlib.util.spec_from_file_location("ai_service_entrypoint", ENTRYPOINT)
assert SPEC and SPEC.loader
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


class AiServiceEntrypointTests(unittest.TestCase):
    def test_tls_material_becomes_private_files_and_leaves_environment(self):
        env = {
            "FOODTECH_SERVICE": "credential-registration",
            "FOODTECH_TLS_SERVER_CERT_PEM": "-----BEGIN CERTIFICATE-----\ncert\n",
            "FOODTECH_TLS_SERVER_KEY_PEM": "-----BEGIN PRIVATE KEY-----\nkey\n",
            "FOODTECH_TLS_BUSINESS_CA_PEM": "-----BEGIN CERTIFICATE-----\nca\n",
        }
        captured = {}

        def capture_exec(program, argv, process_env):
            captured.update(program=program, argv=argv, env=dict(process_env))

        with patch.dict(os.environ, env, clear=True), patch.object(entrypoint.os, "execvpe", side_effect=capture_exec):
            entrypoint.main()
            for key in ("FOODTECH_TLS_SERVER_CERT_PEM", "FOODTECH_TLS_SERVER_KEY_PEM", "FOODTECH_TLS_BUSINESS_CA_PEM"):
                self.assertNotIn(key, captured["env"])
            self.assertEqual(captured["argv"], ["python", "-m", "foodtech_credential_registration.serve"])
            for key in ("FOODTECH_REGISTRATION_SERVER_CERT", "FOODTECH_REGISTRATION_SERVER_KEY", "FOODTECH_REGISTRATION_BUSINESS_CA"):
                path = pathlib.Path(captured["env"][key])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                path.unlink()
            pathlib.Path(captured["env"]["FOODTECH_REGISTRATION_SERVER_CERT"]).parent.rmdir()


if __name__ == "__main__":
    unittest.main()
