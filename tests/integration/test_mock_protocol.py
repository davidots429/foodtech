"""Cross-language hash and actual HTTPS/OAuth/disconnect checks."""

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
from foodtech_contracts import decode_record, payload_hash

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/mockprofile-100-a-001.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class MockProtocolTests(unittest.TestCase):
    def test_hash_matches_python_rfc8785(self):
        output = subprocess.check_output(
            ["dotnet", str(DLL), "hash", "--fixture", str(FIXTURE)], text=True,
        )
        actual = dict(line.split() for line in output.splitlines())
        records = json.loads(FIXTURE.read_text())["records"]
        expected = {record["recordId"]: payload_hash(decode_record(json.dumps(record))) for record in records}
        self.assertEqual(actual, expected)

    def test_https_oauth_and_disconnect_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1",
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([
                "openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key),
                "-in", str(cert), "-password", "pass:test-password",
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-client",
                   "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                   "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
            server = subprocess.Popen([
                "dotnet", str(DLL), "serve", "--fixture", str(FIXTURE), "--cert", str(pfx),
                "--port", str(port), "--disconnect-once",
            ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"https://localhost:{port}"
                for _ in range(50):
                    if server.poll() is not None:
                        self.fail("Mock server exited before health became available")
                    try:
                        health = httpx.get(base + "/health", verify=str(cert), timeout=0.3)
                        if health.status_code == 200:
                            break
                    except httpx.TransportError:
                        time.sleep(0.1)
                else:
                    self.fail("Mock server health did not become available")
                client = RestMockClient(base, "test-client", "test-secret", str(cert))
                try:
                    result = client.poll(0)
                    self.assertEqual(result.next_cursor, 3)
                    self.assertEqual([decode_record(raw)["sequenceNumber"] for raw in result.records], [1, 3, 2])
                    self.assertEqual(client.poll(result.next_cursor).records, ())
                finally:
                    client.close()
            finally:
                server.terminate()
                server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
