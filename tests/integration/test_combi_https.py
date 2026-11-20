"""Authenticated C# combi Mock poll and explicit meal-bound source validation."""

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

from foodtech_collection_adapter.combi_stream import CombiStreamValidator
from foodtech_collection_adapter.rest import RestMockClient

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/combi-normal-medium-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class CombiHttpsTests(unittest.TestCase):
    def test_poll_requires_auth_and_preserves_bound_phase_identities(self):
        fixture = json.loads(FIXTURE.read_text())
        jobs = {phase["phase"]: phase["cookingJobId"] for phase in fixture["phases"]}
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key),
                            "-in", str(cert), "-password", "pass:test-password"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                   "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
            server = subprocess.Popen(["dotnet", str(DLL), "serve", "--fixture", str(FIXTURE), "--cert", str(pfx),
                                       "--port", str(port)], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"https://localhost:{port}"
                for _ in range(50):
                    if server.poll() is not None:
                        self.fail("Mock exited before health became available")
                    try:
                        if httpx.get(base + "/health", verify=str(cert), timeout=0.3).status_code == 200:
                            break
                    except httpx.TransportError:
                        time.sleep(0.1)
                else:
                    self.fail("Mock health did not become available")
                self.assertEqual(httpx.get(base + "/v1/records", verify=str(cert)).status_code, 401)
                client = RestMockClient(base, "test-client", "test-secret", str(cert), profile_id="mockprofile-120")
                try:
                    result = client.poll()
                    self.assertEqual((len(result.records), result.next_cursor), (2700, 2700))
                    self.assertEqual(client.poll(result.next_cursor).records, ())
                    validator = CombiStreamValidator(fixture["mealBatchId"], fixture["deviceId"], jobs)
                    parsed = [validator.accept(item) for item in result.records]
                    self.assertEqual((parsed[0]["cookingJobId"], parsed[1800]["cookingJobId"],
                                      parsed[1980]["cookingJobId"]),
                                     (jobs["RICE"], jobs["TRANSITION"], jobs["MACKEREL"]))
                finally:
                    client.close()
            finally:
                server.terminate()
                server.wait(timeout=5)
