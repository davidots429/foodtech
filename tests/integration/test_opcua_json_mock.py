"""Real HTTPS for the project-specific synthetic sessionless Browse/Read contract."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from foodtech_collection_adapter.opcua_json import UaJsonClient, collect
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"
FIXTURE = ROOT / "fixtures/mockprofile-200-a-001.json"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class UaJsonTests(unittest.TestCase):
    def test_https_read_only_and_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key), "-in", str(cert),
                            "-password", "pass:test-password"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            base = f"https://localhost:{port}"
            env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-user", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                   "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
            server = subprocess.Popen(["dotnet", str(DLL), "serve-ua-json", "--fixture", str(FIXTURE),
                                       "--cert", str(pfx), "--port", str(port), "--bind-address", "0.0.0.0"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with httpx.Client(base_url=base, verify=str(cert), timeout=1) as http:
                    for _ in range(100):
                        if server.poll() is not None:
                            self.fail("Mock exited before accepting connections")
                        try:
                            if http.get("/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            time.sleep(.1)
                    else:
                        self.fail("Mock did not start")
                    self.assertEqual(http.get("/ua-json/v1/browse").status_code, 401)
                    self.assertIn(http.post("/ua-json/v1/write").status_code, (404, 405))
                client = UaJsonClient(base, "test-user", "test-secret", str(cert))
                try:
                    self.assertEqual(client.read("DeviceId"), "mockprofile-200-a-001")
                    with self.assertRaises(httpx.HTTPStatusError):
                        client.read("Write")
                finally:
                    client.close()
                bad = UaJsonClient(base, "test-user", "wrong-secret", str(cert))
                try:
                    with self.assertRaises(httpx.HTTPStatusError):
                        bad.read("DeviceId")
                finally:
                    bad.close()
                class Failed:
                    def ingest(self, raw):
                        raise RuntimeError("simulated storage failure")
                with self.assertRaisesRegex(RuntimeError, "storage failure"):
                    collect(base, "test-user", "test-secret", str(cert), Failed(), count=3, timeout_seconds=20)
                class Ingestor:
                    def __init__(self):
                        self.records, self.results, self.times = [], [], []
                    def ingest(self, raw):
                        self.records.append(raw)
                        self.times.append(time.monotonic())
                        self.results.append(assess(str(len(self.records)), raw))
                    def drain(self, stream):
                        return []
                ingestor = Ingestor()
                self.assertEqual(collect(base, "test-user", "test-secret", str(cert), ingestor,
                                         count=3, timeout_seconds=20), 3)
                self.assertEqual([json.loads(raw)["sequenceNumber"] for raw in ingestor.records], [1, 2, 3])
                self.assertGreaterEqual(ingestor.times[2] - ingestor.times[1], 4.0)
                self.assertEqual([item.quality_status for item in ingestor.results], ["VALID", "VALID", "QUARANTINED"])
                self.assertTrue(all(item.mapping_version == "mockprofile-200-a-map-v1" for item in ingestor.results))

                # Restart before collection, then again after two saved records. The
                # new server starts its history at one and invalidates old tokens.
                server.terminate(); server.wait(timeout=5)
                def start_server():
                    return subprocess.Popen(["dotnet", str(DLL), "serve-ua-json", "--fixture", str(FIXTURE),
                        "--cert", str(pfx), "--port", str(port)], env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                def wait_healthy(process):
                    for _ in range(100):
                        if process.poll() is not None: self.fail("Mock exited during restart")
                        try:
                            if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200: return
                        except httpx.TransportError: pass
                        time.sleep(.1)
                    self.fail("Mock did not restart")
                server = start_server(); wait_healthy(server)
                second_saved = threading.Event()
                class RestartIngestor(Ingestor):
                    def ingest(self, raw):
                        super().ingest(raw)
                        if len(self.records) == 2: second_saved.set()
                restarting = RestartIngestor()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(collect, base, "test-user", "test-secret", str(cert), restarting,
                                         count=3, timeout_seconds=60)
                    self.assertTrue(second_saved.wait(15), "second record was not received")
                    server.terminate(); server.wait(timeout=5)
                    server = start_server(); wait_healthy(server)
                    self.assertEqual(future.result(timeout=60), 3)
                self.assertEqual([json.loads(raw)["sequenceNumber"] for raw in restarting.records], [1, 2, 3])
            finally:
                server.terminate()
                server.wait(timeout=5)
