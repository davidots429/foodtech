"""Real C# HTTPS/OAuth collection feeds the Jeyuk process monitor."""

import json
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

from foodtech_collection_adapter.rest import RestMockClient
from foodtech_collection_adapter import __main__ as collector_cli
from foodtech_standardization_worker.jeyuk_bridge import JeyukProcessStream


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-process-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class JeyukHttpsTests(unittest.TestCase):
    def test_realtime_mock_to_until_complete_collector(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fixture = json.loads(FIXTURE.read_text())
            fixture["samples"] = [{**fixture["samples"][0], "seconds": 6}]
            source = temp / "short.json"; source.write_text(json.dumps(fixture))
            key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key), "-in", str(cert),
                            "-password", "pass:test-password"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
            env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                   "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
            server = subprocess.Popen(["dotnet", str(DLL), "serve", "--fixture", str(source), "--cert", str(pfx),
                                       "--port", str(port), "--realtime"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"https://localhost:{port}"
                for _ in range(100):
                    if server.poll() is not None: self.fail("Mock exited")
                    try:
                        if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200: break
                    except httpx.TransportError: time.sleep(.1)
                else: self.fail("Mock did not start")
                calls = {"records": [], "cursors": []}
                class Ingestor:
                    def __init__(self, *_args): pass
                    def cursor(self, key): return 0
                    def ingest(self, raw): calls["records"].append(raw)
                    def save_cursor(self, cursor, key): calls["cursors"].append(cursor)
                config = {"FOODTECH_MOCK_BASE_URL": base, "FOODTECH_MOCK_CA_FILE": str(cert),
                    "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                    "FOODTECH_DATA_TABLE": "test", "FOODTECH_ORDERED_QUEUE_URL": "test"}
                with patch.dict(os.environ, config), patch.object(collector_cli, "DynamoJeyukIngestor", Ingestor), \
                     patch("sys.argv", ["collector", "--profile", "mockprofile-110", "--until-complete",
                                        "--interval", ".5", "--max-runtime", "15"]):
                    self.assertEqual(collector_cli.main(), 0)
                self.assertEqual([json.loads(raw)["second"] for raw in calls["records"]], list(range(1, 7)))
                self.assertEqual(calls["cursors"][-1], 6)
                self.assertGreater(len(calls["cursors"]), 1)
            finally:
                server.terminate(); server.wait(timeout=5)

    def test_realtime_poll_only_exposes_elapsed_synthetic_samples(self):
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
                probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
            env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                   "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
            server = subprocess.Popen(["dotnet", str(DLL), "serve", "--fixture", str(FIXTURE), "--cert", str(pfx),
                                       "--port", str(port), "--realtime"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"https://localhost:{port}"
                for _ in range(100):
                    if server.poll() is not None: self.fail("Mock exited")
                    try:
                        if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200: break
                    except httpx.TransportError: time.sleep(.1)
                else: self.fail("Mock did not start")
                client = RestMockClient(base, "test-client", "test-secret", str(cert), profile_id="mockprofile-110")
                try:
                    first = client.poll(0)
                    self.assertFalse(first.complete)
                    self.assertLess(first.next_cursor, 30)
                    deadline = time.monotonic() + 6
                    next_result = first
                    while next_result.next_cursor == first.next_cursor and time.monotonic() < deadline:
                        time.sleep(.2)
                        next_result = client.poll(first.next_cursor)
                    self.assertGreater(next_result.next_cursor, first.next_cursor)
                    self.assertFalse(next_result.complete)
                    combined = first.records + next_result.records
                    stream = JeyukProcessStream()
                    for raw in combined: stream.accept(raw)
                    self.assertEqual([json.loads(raw)["second"] for raw in combined], list(range(1, len(combined) + 1)))
                finally:
                    client.close()
            finally:
                server.terminate(); server.wait(timeout=5)

    def test_authenticated_poll_feeds_process_and_rejects_wrong_profile(self):
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
                "--port", str(port),
            ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"https://localhost:{port}"
                for _ in range(50):
                    if server.poll() is not None:
                        self.fail("Jeyuk Mock server exited before health became available")
                    try:
                        if httpx.get(base + "/health", verify=str(cert), timeout=0.3).status_code == 200:
                            break
                    except httpx.TransportError:
                        time.sleep(0.1)
                else:
                    self.fail("Jeyuk Mock health did not become available")
                self.assertEqual(httpx.get(base + "/v1/records", verify=str(cert)).status_code, 401)
                wrong = RestMockClient(base, "test-client", "test-secret", str(cert))
                try:
                    with self.assertRaisesRegex(ValueError, "unexpected Mock contract"):
                        wrong.poll()
                finally:
                    wrong.close()
                client = RestMockClient(base, "test-client", "test-secret", str(cert),
                                        profile_id="mockprofile-110")
                try:
                    result = client.poll()
                    self.assertEqual(result.next_cursor, 30)
                    self.assertEqual(len(result.records), 30)
                    self.assertEqual(client.poll(result.next_cursor).records, ())
                    stream = JeyukProcessStream()
                    decisions = [stream.accept(raw) for raw in result.records]
                    for checkpoint in json.loads(FIXTURE.read_text())["checkpoints"]:
                        reading = decisions[checkpoint["second"] - 1]
                        self.assertEqual(reading.status, checkpoint["status"])
                        self.assertIn(checkpoint["reasonCode"], reading.reason_codes)
                finally:
                    client.close()
            finally:
                server.terminate()
                server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
