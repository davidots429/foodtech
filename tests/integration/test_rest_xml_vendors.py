"""Synthetic vendor-b/c XML over actual authenticated HTTPS."""
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


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class RestXmlVendorsTests(unittest.TestCase):
    def test_vendor_b_c_over_https(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                "-out", str(cert), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key), "-in", str(cert),
                "-password", "pass:test-password"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for vendor in ("b", "c"):
                with self.subTest(vendor=vendor):
                    instance = f"mockprofile-101-{vendor}-001"
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", 0))
                        port = probe.getsockname()[1]
                    base = f"https://localhost:{port}"
                    env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                           "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
                    server = subprocess.Popen(["dotnet", str(DLL), "serve", "--fixture", str(ROOT / f"fixtures/{instance}.json"),
                        "--cert", str(pfx), "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        for _ in range(100):
                            if server.poll() is not None: self.fail("Mock exited")
                            try:
                                if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200: break
                            except httpx.TransportError: time.sleep(.1)
                        else: self.fail("Mock did not start")
                        calls = {"records": [], "cursors": []}
                        class Ingestor:
                            def __init__(self, *args): pass
                            def cursor_state(self, key): calls["key"] = key; return 0, set()
                            def ingest(self, raw): calls["records"].append(raw); return []
                            def drain(self, stream): return []
                            def save_cursor(self, cursor, streams, key): calls["cursors"].append(cursor)
                        config = {"FOODTECH_MOCK_BASE_URL": base, "FOODTECH_MOCK_CA_FILE": str(cert),
                                  "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                                  "FOODTECH_DATA_TABLE": "test", "FOODTECH_ORDERED_QUEUE_URL": "test"}
                        with patch.dict(os.environ, config), patch.object(cli, "DynamoOrderedIngestor", Ingestor), \
                             patch("sys.argv", ["collector", "--profile", "mockprofile-101", "--instance", instance,
                                                "--polls", "4", "--interval", "0"]):
                            self.assertEqual(cli.main(), 0)
                        self.assertEqual(calls["key"], f"xml-{vendor}#default")
                        self.assertEqual(calls["cursors"], [1, 2, 3, 3])
                        engine, released = Resequencer(), []
                        for i, raw in enumerate(calls["records"]):
                            self.assertTrue(raw.startswith("<record>"))
                            record = decode_record(raw)
                            released.extend(engine.accept(Received(str(i), raw, record["sourceId"], record["deviceId"],
                                record["batchId"], record["sequenceNumber"], float(i)), float(i)))
                        self.assertEqual([item.sequence_number for item in released], [1, 2, 3])
                        result = [assess(item.ingestion_event_id, item.raw) for item in released]
                        self.assertEqual([item.quality_status for item in result], ["VALID", "VALID", "QUARANTINED"])
                        self.assertTrue(all(item.mapping_version == f"mockprofile-101-{vendor}-map-v1" for item in result))
                        self.assertIn("OUTPUT_PERCENT" if vendor == "b" else "SPEED_LEVEL", result[0].normalized)
                        self.assertNotIn("ROTATIONAL_SPEED_RPM", result[0].normalized)
                    finally:
                        server.terminate()
                        server.wait(timeout=5)
