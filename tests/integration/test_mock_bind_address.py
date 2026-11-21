"""Only explicit local/private IPv4 or wildcard binds are accepted."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / 'apps/mock-control/bin/Release/net10.0/MockControl.dll'


@unittest.skipUnless(shutil.which('dotnet') and DLL.exists(), 'built .NET 10 Mock required')
class MockBindTests(unittest.TestCase):
    def test_public_and_hostname_bind_addresses_are_rejected(self):
        cases = [('serve', 'mockprofile-100-a-001.json'),
                 ('serve-mqtt', 'mockprofile-300-a-001.json'),
                 ('serve-ua-json', 'mockprofile-200-a-001.json')]
        env = {**os.environ, 'FOODTECH_MOCK_CLIENT_ID': 'test', 'FOODTECH_MOCK_CLIENT_SECRET': 'test',
               'FOODTECH_MQTT_USER': 'test', 'FOODTECH_MQTT_PASSWORD': 'test',
               'FOODTECH_MOCK_CERT_PASSWORD': 'test'}
        for command, fixture in cases:
            for address in ('8.8.8.8', 'localhost', '::1'):
                with self.subTest(command=command, address=address):
                    process = subprocess.run(['dotnet', str(DLL), command, '--fixture', str(ROOT / 'fixtures' / fixture),
                        '--cert', 'unused', '--port', '18883', '--bind-address', address],
                        env=env, text=True, capture_output=True, timeout=10)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn('--bind-address', process.stderr)


if __name__ == '__main__':
    unittest.main()
