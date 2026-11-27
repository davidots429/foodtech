"""Exercise the C# REST server with injected TLS material and a finite lifetime."""
import os
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest

import httpx

from foodtech_collection_adapter.rest import RestMockClient

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / 'apps/mock-control/bin/Release/net10.0/MockControl.dll'
FIXTURE = ROOT / 'fixtures/rest-invalid-utf8-single-v1.json'
CERT_VARS = ('FOODTECH_MOCK_SERVER_CERT_PEM', 'FOODTECH_MOCK_SERVER_KEY_PEM', 'FOODTECH_MOCK_CERT_PASSWORD')


@unittest.skipUnless(shutil.which('dotnet') and DLL.is_file(), 'built .NET 10 Mock required')
class InjectedRestCertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='foodtech-injected-rest-')
        cls.temp = Path(cls.directory.name)
        cls.key, cls.cert, cls.pfx = (cls.temp / name for name in ('key.pem', 'cert.pem', 'cert.pfx'))
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(cls.key),
            '-out', str(cls.cert), '-days', '1', '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['openssl', 'pkcs12', '-export', '-out', str(cls.pfx), '-inkey', str(cls.key),
            '-in', str(cls.cert), '-password', 'pass:synthetic-test-password'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.ca = ssl.create_default_context(cafile=str(cls.cert))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def configuration(self):
        env = {k: v for k, v in os.environ.items() if k not in CERT_VARS}
        env.update(FOODTECH_MOCK_CLIENT_ID='synthetic-test-client',
            FOODTECH_MOCK_CLIENT_SECRET='synthetic-test-secret',
            FOODTECH_MOCK_SERVER_CERT_PEM=self.cert.read_text(), FOODTECH_MOCK_SERVER_KEY_PEM=self.key.read_text(),
            ASPNETCORE_ENVIRONMENT='Production', DOTNET_EnableDiagnostics='0')
        return env

    def command(self, *, runtime='10', source='injected'):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        # Exercise the deployed Task command; only local paths/port/deadline and
        # the explicit legacy PFX test differ from the reviewed runtime command.
        command = json.loads((ROOT / 'infra/containers/private-rest-trial-command.json').read_text())
        command[command.index('--fixture') + 1] = str(FIXTURE)
        command[command.index('--port') + 1] = str(port)
        command[command.index('--certificate-source') + 1] = source
        index = command.index('--max-runtime')
        if runtime is None:
            del command[index:index + 2]
        else:
            command[index + 1] = runtime
        args = ['dotnet', str(DLL), *command]
        return args, f'https://localhost:{port}'

    def assert_redacted(self, output):
        self.assertNotIn('-----BEGIN PRIVATE KEY-----', output)
        self.assertNotIn('synthetic-invalid-key-never-log', output)

    def test_tls_oauth_and_original_fault_without_server_key_file(self):
        args, base = self.command()
        with tempfile.TemporaryDirectory(prefix='foodtech-mock-cwd-') as cwd:
            server = subprocess.Popen(args, cwd=cwd, env=self.configuration(), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        self.fail('Injected Mock exited before serving HTTPS.')
                    try:
                        if httpx.get(base + '/health', verify=self.ca, timeout=.3, trust_env=False).status_code == 200:
                            break
                    except httpx.TransportError:
                        time.sleep(.05)
                else:
                    self.fail('Injected Mock did not become ready.')
                wrong = httpx.post(base + '/oauth2/token', data={'grant_type': 'client_credentials'},
                    auth=('wrong-client', 'wrong-secret'), verify=self.ca, timeout=2, trust_env=False)
                self.assertEqual(wrong.status_code, 401)
                client = RestMockClient(base, 'synthetic-test-client', 'synthetic-test-secret', str(self.cert))
                try:
                    result = client.poll(0)
                    self.assertEqual(result.records, (b'\xff\xfe\x80',))
                    self.assertEqual(result.next_cursor, 1)
                    self.assertTrue(result.complete)
                    self.assertTrue(result.completion_explicit)
                finally:
                    client.close()
            finally:
                if server.poll() is None:
                    server.terminate()
                output, _ = server.communicate(timeout=10)
            self.assert_redacted(output)
            self.assertEqual(list(Path(cwd).iterdir()), [])

    def test_injected_listener_stops_at_its_deadline(self):
        args, _ = self.command(runtime='1')
        result = subprocess.run(args, env=self.configuration(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assert_redacted(result.stdout + result.stderr)

    def test_injected_mode_requires_an_explicit_valid_deadline(self):
        for runtime in (None, '0', '601', 'not-a-number'):
            with self.subTest(runtime=runtime):
                args, _ = self.command(runtime=runtime)
                result = subprocess.run(args, env=self.configuration(), capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('max-runtime', result.stdout + result.stderr)
                self.assert_redacted(result.stdout + result.stderr)

    def test_missing_malformed_and_mixed_key_inputs_fail_without_values(self):
        for changes, extra in (
            ({'FOODTECH_MOCK_SERVER_KEY_PEM': ''}, []),
            ({'FOODTECH_MOCK_SERVER_KEY_PEM': 'synthetic-invalid-key-never-log'}, []),
            ({'FOODTECH_MOCK_CERT_PASSWORD': 'synthetic-invalid-key-never-log'}, []),
            ({}, ['--cert', '/not-used.pfx']),
        ):
            args, _ = self.command()
            result = subprocess.run(args + extra, env={**self.configuration(), **changes}, capture_output=True,
                                    text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assert_redacted(result.stdout + result.stderr)

    def test_existing_pfx_source_remains_usable(self):
        args, _ = self.command(runtime='1', source='file')
        env = {k: v for k, v in self.configuration().items() if k not in CERT_VARS}
        env['FOODTECH_MOCK_CERT_PASSWORD'] = 'synthetic-test-password'
        result = subprocess.run(args + ['--cert', str(self.pfx)], env=env, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assert_redacted(result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
