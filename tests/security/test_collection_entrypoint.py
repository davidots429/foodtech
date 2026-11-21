import importlib.util
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
