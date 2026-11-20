"""C# combi HTTPS replay exposes complete five-second windows using a resumable cursor."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from foodtech_collection_adapter.rest import RestMockClient
from foodtech_contracts.combi_stream import CombiStreamValidator

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / 'apps/mock-control/bin/Release/net10.0/MockControl.dll'

@unittest.skipUnless(shutil.which('dotnet') and DLL.exists(), 'built .NET 10 Mock required')
class TimedCombiTests(unittest.TestCase):
    def test_five_second_replay_cursor_and_completion(self):
        fixture = json.loads((ROOT / 'fixtures/combi-normal-medium-v1.json').read_text())
        for phase in fixture['phases']:
            phase['segments'][0]['seconds'] = 5
        base = fixture['phases'][0]['segments'][0]
        fixture['phases'][0]['segments'] = [{**base, 'seconds': 2},
            {**base, 'seconds': 1, 'doorState': 'OPEN', 'criticalAlarm': True},
            {**base, 'seconds': 2}]
        jobs = {p['phase']: p['cookingJobId'] for p in fixture['phases']}
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source, key, cert, pfx = (temp / n for n in ('fixture.json', 'key.pem', 'cert.pem', 'cert.pfx'))
            source.write_text(json.dumps(fixture))
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
                '-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['openssl','pkcs12','-export','-out',str(pfx),'-inkey',str(key),'-in',str(cert),
                '-password','pass:test-password'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0)); port = probe.getsockname()[1]
            env = {**os.environ, 'FOODTECH_MOCK_CLIENT_ID':'test-client', 'FOODTECH_MOCK_CLIENT_SECRET':'test-secret',
                   'FOODTECH_MOCK_CERT_PASSWORD':'test-password'}
            server = subprocess.Popen(['dotnet',str(DLL),'serve','--fixture',str(source),'--cert',str(pfx),
                                       '--port',str(port),'--realtime'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f'https://localhost:{port}'
                for _ in range(100):
                    if server.poll() is not None: self.fail('Mock exited')
                    try:
                        if httpx.get(base+'/health', verify=str(cert), timeout=.3).status_code == 200: break
                    except httpx.TransportError: time.sleep(.05)
                else: self.fail('Mock did not start')
                client = RestMockClient(base,'test-client','test-secret',str(cert),profile_id='mockprofile-120')
                try:
                    first = client.poll()
                    self.assertFalse(first.complete)
                    self.assertEqual(first.next_cursor, len(first.records))
                    received = list(first.records); cursor = first.next_cursor; deadline = time.monotonic() + 22
                    alarm_delivered_before_window = (cursor < 5 and any(json.loads(raw)['criticalAlarm'] for raw in received))
                    while time.monotonic() < deadline:
                        result = client.poll(cursor, wait_seconds=4)
                        self.assertEqual(result.next_cursor, cursor + len(result.records))
                        if any(json.loads(raw)['criticalAlarm'] for raw in result.records):
                            alarm_delivered_before_window = result.next_cursor < 5
                        received.extend(result.records); cursor = result.next_cursor
                        if result.complete: break
                        time.sleep(.05)
                    self.assertTrue(result.complete)
                    self.assertTrue(alarm_delivered_before_window)
                    self.assertEqual(cursor,15)
                    validator = CombiStreamValidator(fixture['mealBatchId'],fixture['deviceId'],jobs)
                    self.assertEqual([validator.accept(x)['sequence'] for x in received],list(range(1,16)))
                    replay = client.poll(0)
                    self.assertEqual(replay.records,tuple(received))
                    self.assertTrue(replay.complete)
                finally: client.close()
            finally:
                server.terminate(); server.wait(timeout=5)
