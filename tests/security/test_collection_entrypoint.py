import importlib.util
import base64
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


path = Path(__file__).resolve().parents[2] / 'infra/containers/collection_entrypoint.py'
spec = importlib.util.spec_from_file_location('collection_entrypoint', path)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class CollectionEntrypointTests(unittest.TestCase):
    def test_trial_requires_explicit_opt_in_and_rest_before_materializing_secrets(self):
        for environment in (
                {'FOODTECH_COLLECTION_RUN_MODE': 'private-rest-trial'},
                {'FOODTECH_COLLECTION_RUN_MODE': 'private-rest-trial',
                 'FOODTECH_REST_TRIAL_ENABLED': 'true', 'FOODTECH_COLLECTION_PROTOCOL': 'mqtt'},
                {'FOODTECH_COLLECTION_RUN_MODE': 'unknown'}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                    patch.object(entrypoint.tempfile, 'mkdtemp') as create, \
                    patch.object(entrypoint.os, 'execvpe') as execute:
                with self.assertRaises(ValueError):
                    entrypoint.main()
                create.assert_not_called()
                execute.assert_not_called()

    def test_trial_uses_private_ca_and_dedicated_module_without_secret_arguments(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'FOODTECH_COLLECTION_RUN_MODE': 'private-rest-trial',
                    'FOODTECH_REST_TRIAL_ENABLED': 'true',
                    'FOODTECH_MOCK_CA_PEM': '-----BEGIN CERTIFICATE-----\nsynthetic',
                    'FOODTECH_MOCK_CLIENT_SECRET': 'synthetic-secret'}, clear=True), \
                patch.object(entrypoint.sys, 'argv', ['entry', '--trial']), \
                patch.object(entrypoint.tempfile, 'mkdtemp', return_value=directory), \
                patch.object(entrypoint.ssl, 'create_default_context'), \
                patch.object(entrypoint.os, 'execvpe') as execute:
            entrypoint.main()
            self.assertEqual(execute.call_args.args[1],
                             ['python', '-m', 'foodtech_collection_adapter.rest_trial', '--trial'])
            child = execute.call_args.args[2]
            self.assertNotIn('FOODTECH_MOCK_CA_PEM', child)
            self.assertEqual(stat.S_IMODE(Path(child['FOODTECH_MOCK_CA_FILE']).stat().st_mode), 0o600)
            self.assertNotIn('synthetic-secret', execute.call_args.args[1])

    def test_ca_is_private_and_secret_is_not_in_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {'FOODTECH_MOCK_CA_PEM': '-----BEGIN CERTIFICATE-----\nsynthetic',
                           'FOODTECH_MOCK_CLIENT_SECRET': 'synthetic-secret'}
            with patch.dict(os.environ, environment, clear=True), patch.object(entrypoint.sys, 'argv', ['entry', '--polls', '2']), \
                    patch.object(entrypoint.tempfile, 'mkdtemp', return_value=directory), \
                    patch.object(entrypoint.ssl, 'create_default_context') as validate, \
                    patch.object(entrypoint.os, 'execvpe') as execute:
                entrypoint.main()
                ca = Path(directory) / 'ca.pem'
                self.assertEqual(stat.S_IMODE(ca.stat().st_mode), 0o600)
                validate.assert_called_once_with(cafile=str(ca))
                command = execute.call_args.args
                self.assertEqual(command[1], ['python', '-m', 'foodtech_collection_adapter', '--polls', '2'])
                self.assertNotIn('synthetic-secret', command[1])
                self.assertNotIn('FOODTECH_MOCK_CA_PEM', command[2])
                self.assertEqual(command[2]['FOODTECH_MOCK_CA_FILE'], str(ca))

    def test_invalid_ca_does_not_start_collector(self):
        with patch.dict(os.environ, {'FOODTECH_MOCK_CA_PEM': 'invalid'}, clear=True), \
                patch.object(entrypoint.sys, 'argv', ['entry']), patch.object(entrypoint.os, 'execvpe') as execute:
            with self.assertRaises(ValueError):
                entrypoint.main()
            execute.assert_not_called()

    def test_mqtt_uses_its_own_module_and_ca_variable(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'FOODTECH_COLLECTION_PROTOCOL': 'mqtt',
                    'FOODTECH_MOCK_CA_PEM': '-----BEGIN CERTIFICATE-----\nsynthetic'}, clear=True), \
                patch.object(entrypoint.sys, 'argv', ['entry', '--count', '3']), \
                patch.object(entrypoint.tempfile, 'mkdtemp', return_value=directory), \
                patch.object(entrypoint.ssl, 'create_default_context'), patch.object(entrypoint.os, 'execvpe') as execute:
            entrypoint.main()
            self.assertEqual(execute.call_args.args[1], ['python', '-m', 'foodtech_collection_adapter.mqtt', '--count', '3'])
            self.assertEqual(execute.call_args.args[2]['FOODTECH_MQTT_CA_FILE'], str(Path(directory) / 'ca.pem'))

    def test_unknown_protocol_does_not_start_collector(self):
        with patch.dict(os.environ, {'FOODTECH_COLLECTION_PROTOCOL': 'unknown'}, clear=True), \
                patch.object(entrypoint.sys, 'argv', ['entry', '--help']), patch.object(entrypoint.os, 'execvpe') as execute:
            with self.assertRaisesRegex(ValueError, 'unsupported collection protocol'):
                entrypoint.main()
            execute.assert_not_called()

    def test_opcua_json_uses_its_own_module_and_ca_variable(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'FOODTECH_COLLECTION_PROTOCOL': 'opcua-json',
                    'FOODTECH_MOCK_CA_PEM': '-----BEGIN CERTIFICATE-----\nsynthetic'}, clear=True), \
                patch.object(entrypoint.sys, 'argv', ['entry', '--count', '3']), \
                patch.object(entrypoint.tempfile, 'mkdtemp', return_value=directory), \
                patch.object(entrypoint.ssl, 'create_default_context'), patch.object(entrypoint.os, 'execvpe') as execute:
            entrypoint.main()
            self.assertEqual(execute.call_args.args[1], ['python', '-m', 'foodtech_collection_adapter.opcua_json', '--count', '3'])
            self.assertEqual(execute.call_args.args[2]['FOODTECH_MOCK_CA'], str(Path(directory) / 'ca.pem'))

    def test_opcua_binary_requires_matching_key_and_private_files(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-client')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
        for candidate, valid in ((key, True), (other, False)):
            env = {'FOODTECH_COLLECTION_PROTOCOL': 'opcua-binary',
                   'FOODTECH_OPCUA_CLIENT_CERT_PEM': cert.public_bytes(serialization.Encoding.PEM).decode(),
                   'FOODTECH_OPCUA_CLIENT_KEY_PEM': candidate.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
                   'FOODTECH_OPCUA_SERVER_CERT_DER_BASE64': base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()}
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, env, clear=True), patch.object(entrypoint.sys, 'argv', ['entry']), \
                    patch.object(entrypoint.tempfile, 'mkdtemp', return_value=directory), \
                    patch.object(entrypoint.os, 'execvpe') as execute:
                if not valid:
                    with self.assertRaisesRegex(ValueError, 'invalid OPC UA certificate material'):
                        entrypoint.main()
                    execute.assert_not_called()
                    continue
                entrypoint.main()
                self.assertEqual(execute.call_args.args[1], ['python', '-m', 'foodtech_collection_adapter.opcua'])
                child = execute.call_args.args[2]
                for variable in ('FOODTECH_OPCUA_CLIENT_CERT', 'FOODTECH_OPCUA_CLIENT_KEY', 'FOODTECH_OPCUA_SERVER_CERT'):
                    self.assertEqual(stat.S_IMODE(Path(child[variable]).stat().st_mode), 0o600)
                self.assertNotIn('FOODTECH_OPCUA_CLIENT_KEY_PEM', child)

    def test_real_ca_is_loadable_and_malformed_pem_is_rejected(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-ca')])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
        pem = certificate.public_bytes(serialization.Encoding.PEM).decode('ascii')
        for value, valid in ((pem, True), ('-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n', False)):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {'FOODTECH_MOCK_CA_PEM': value}, clear=True), \
                    patch.object(entrypoint.sys, 'argv', ['entry']), \
                    patch.object(entrypoint.tempfile, 'mkdtemp', return_value=directory), \
                    patch.object(entrypoint.os, 'execvpe') as execute:
                if valid:
                    entrypoint.main()
                    execute.assert_called_once()
                else:
                    with self.assertRaises(entrypoint.ssl.SSLError):
                        entrypoint.main()
                    execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
