"""Check that ECS TLS injection does not reach the app process as environment values."""

import importlib.util
import os
import pathlib
import stat
from tempfile import TemporaryDirectory
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


    def test_tonkatsu_container_modes_load_only_their_tls_paths(self):
        modes = (
            ("tonkatsu-gateway", "foodtech_ai_gateway.serve_tonkatsu",
             ("SERVER_CERT", "SERVER_KEY", "BROKER_CA", "BROKER_CERT")),
            ("tonkatsu-business-gateway", "foodtech_ai_gateway.serve_tonkatsu_business",
             ("BUSINESS_GATEWAY_SERVER_CERT", "BUSINESS_GATEWAY_SERVER_KEY",
              "BUSINESS_GATEWAY_CLIENT_CA", "BUSINESS_GATEWAY_CLIENT_CERT")),
            ("tonkatsu-broker", "foodtech_business_api.serve_tonkatsu_broker",
             ("BROKER_SERVER_CERT", "BROKER_SERVER_KEY", "BFF_CA", "BFF_CERT",
              "GATEWAY_SERVER_CERT", "BROKER_CLIENT_CERT", "BROKER_CLIENT_KEY")),
        )
        for mode, module, material_names in modes:
            with self.subTest(mode=mode), TemporaryDirectory() as parent:
                prefix = "FOODTECH_TONKATSU_"
                materials = {prefix + name: f"-----BEGIN TEST-----\n{name}\n" for name in material_names}
                env = {"FOODTECH_SERVICE": mode, **{key + "_PEM": value for key, value in materials.items()}}
                captured = {}

                def capture_exec(program, argv, process_env):
                    captured.update(argv=argv, env=dict(process_env))

                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(entrypoint.tempfile, "tempdir", parent),
                    patch.object(entrypoint.os, "execvpe", side_effect=capture_exec),
                ):
                    entrypoint.main()
                self.assertEqual(captured["argv"], ["python", "-m", module])
                self.assertEqual(set(captured["env"]), {"FOODTECH_SERVICE", *materials})
                for key, material in materials.items():
                    path = pathlib.Path(captured["env"][key])
                    self.assertEqual(path.read_text(), material)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_missing_tonkatsu_peer_pin_never_starts_the_listener(self):
        env = {"FOODTECH_SERVICE": "tonkatsu-gateway"}
        for name in ("SERVER_CERT", "SERVER_KEY", "BROKER_CA"):
            env[f"FOODTECH_TONKATSU_{name}_PEM"] = "-----BEGIN TEST-----\n"
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(entrypoint.tempfile, "mkdtemp") as mkdir,
            patch.object(entrypoint.os, "execvpe") as execute,
        ):
            with self.assertRaisesRegex(KeyError, "FOODTECH_TONKATSU_BROKER_CERT_PEM"):
                entrypoint.main()
            mkdir.assert_not_called()
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
