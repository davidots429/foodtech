"""Synthetic equipment XML over the C# HTTPS server and REST collector."""
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from foodtech_collection_adapter import __main__ as cli
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / 'apps/mock-control/bin/Release/net10.0/MockControl.dll'


@unittest.skipUnless(shutil.which('dotnet') and DLL.exists(), 'built .NET 10 Mock required')
class RestXmlEquipmentTests(unittest.TestCase):
    def test_equipment_vendor_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, cert, pfx = (temp / name for name in ('key.pem', 'cert.pem', 'cert.pfx'))
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['openssl', 'pkcs12', '-export', '-out', str(pfx), '-inkey', str(key), '-in', str(cert),
                '-password', 'pass:test-password'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for profile in ('111', '121'):
                for vendor in 'abc':
                    with self.subTest(profile=profile, vendor=vendor):
                        instance = f'mockprofile-{profile}-{vendor}-001'
                        with socket.socket() as probe:
                            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
                        base = f'https://localhost:{port}'
                        config = {'FOODTECH_MOCK_BASE_URL': base, 'FOODTECH_MOCK_CA_FILE': str(cert),
                                  'FOODTECH_MOCK_CLIENT_ID': 'test-client', 'FOODTECH_MOCK_CLIENT_SECRET': 'test-secret',
                                  'FOODTECH_MOCK_CERT_PASSWORD': 'test-password', 'FOODTECH_DATA_TABLE': 'test',
                                  'FOODTECH_ORDERED_QUEUE_URL': 'test'}
                        server = subprocess.Popen(['dotnet', str(DLL), 'serve', '--fixture',
                            str(ROOT / f'fixtures/{instance}.json'), '--cert', str(pfx), '--port', str(port)],
                            env={**os.environ, **config}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        try:
                            for _ in range(100):
                                if server.poll() is not None: self.fail('Mock exited')
                                try:
                                    if httpx.get(base + '/health', verify=str(cert), timeout=.3).status_code == 200: break
                                except httpx.TransportError: time.sleep(.1)
                            else: self.fail('Mock did not start')
                            records, cursors, keys = [], [], []
                            class Ingestor:
                                def __init__(self, *args): pass
                                def cursor_state(self, key): keys.append(key); return 0, set()
                                def ingest(self, raw): records.append(raw); return []
                                def drain(self, stream): return []
                                def save_cursor(self, cursor, streams, key): cursors.append(cursor)
                            with patch.dict(os.environ, config), patch.object(cli, 'DynamoOrderedIngestor', Ingestor), \
                                    patch('sys.argv', ['collector', '--profile', f'mockprofile-{profile}',
                                          '--instance', instance, '--polls', '4', '--interval', '0']):
                                self.assertEqual(cli.main(), 0)
                            self.assertEqual(keys, [instance + '#default'])
                            self.assertEqual(cursors, [1, 2, 3, 3])
                            results = [assess(str(i), raw) for i, raw in enumerate(records)]
                            self.assertEqual([item.quality_status for item in results], ['VALID', 'VALID', 'QUARANTINED'])
                            self.assertEqual(results[0].mapping_version, f'mockprofile-{profile}-{vendor}-map-v1')
                            self.assertIn('POT_TEMPERATURE' if profile == '111' else 'RELATIVE_HUMIDITY', results[0].normalized)
                        finally:
                            server.terminate(); server.wait(timeout=5)


if __name__ == '__main__': unittest.main()
