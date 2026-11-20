"""Collect vendor-c synthetic records over the actual C# HTTPS Mock."""
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
from foodtech_collection_adapter import __main__ as cli
from foodtech_collection_adapter.resequence import Received, Resequencer
from foodtech_contracts import decode_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"
FIXTURE = ROOT / "fixtures/mockprofile-100-c-001.json"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class VendorCIntegrationTests(unittest.TestCase):
    def test_https_source_normalization_and_cursor(self):
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
            env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                   "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
            server = subprocess.Popen(["dotnet", str(DLL), "serve", "--fixture", str(FIXTURE), "--cert", str(pfx),
                                       "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if server.poll() is not None: self.fail("Mock exited")
                    try:
                        if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200: break
                    except httpx.TransportError: time.sleep(.1)
                else: self.fail("Mock did not start")
                calls = {"records": [], "cursors": [], "receipts": []}
                class Ingestor:
                    def __init__(self, *args): pass
                    def cursor_state(self, key): calls["key"] = key; return 0, set()
                    def ingest(self, raw): calls["records"].append(raw); return []
                    def drain(self, stream): return []
                    def save_cursor(self, cursor, streams, key): calls["cursors"].append((cursor, key))
                    def save_completion_receipt(self, receipt, key): calls["receipts"].append((receipt, key))
                config = {"FOODTECH_MOCK_BASE_URL": base, "FOODTECH_MOCK_CA_FILE": str(cert),
                          "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                          "FOODTECH_DATA_TABLE": "test", "FOODTECH_ORDERED_QUEUE_URL": "test"}
                with patch.dict(os.environ, config), patch.object(cli, "DynamoOrderedIngestor", Ingestor), \
                     patch("sys.argv", ["collector", "--instance", "mockprofile-100-c-001"]):
                    self.assertEqual(cli.main(), 0)
                self.assertEqual(calls["key"], "vendor-c#default")
                self.assertEqual(calls["cursors"], [(3, "vendor-c#default")])
                self.assertEqual(calls["receipts"][0][0]["record_count"], 3)
                self.assertEqual(calls["receipts"][0][1], "vendor-c#default")
                self.assertEqual([decode_record(raw)["sequenceNumber"] for raw in calls["records"]], [1, 3, 2])
                engine, released = Resequencer(), []
                for i, raw in enumerate(calls["records"]):
                    record = decode_record(raw)
                    released.extend(engine.accept(Received(str(i), raw, record["sourceId"], record["deviceId"],
                        record["batchId"], record["sequenceNumber"], float(i)), float(i)))
                self.assertEqual([item.sequence_number for item in released], [1, 2, 3])
                self.assertEqual([assess(item.ingestion_event_id, item.raw).quality_status for item in released],
                                 ["VALID", "VALID", "QUARANTINED"])
                results = [assess(str(i), raw) for i, raw in enumerate(calls["records"])]
                self.assertEqual([item.quality_status for item in results], ["VALID", "QUARANTINED", "VALID"])
                self.assertEqual(results[0].normalized["SPEED_LEVEL"]["value"], 6)
                self.assertNotIn("ROTATIONAL_SPEED_RPM", results[0].normalized)
                self.assertIn("Readings", json.loads(calls["records"][0]))
            finally:
                server.terminate()
                server.wait(timeout=5)
