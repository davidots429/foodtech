import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


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


if __name__ == '__main__':
    unittest.main()
