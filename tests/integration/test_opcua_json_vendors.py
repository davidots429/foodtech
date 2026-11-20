"""Project-specific sessionless HTTPS sources using vendor-b and vendor-c."""
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from foodtech_collection_adapter.opcua_json import collect
from foodtech_contracts import decode_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class UaJsonVendorTests(unittest.TestCase):
    def test_vendor_b_and_c_source_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key), "-in", str(cert),
                            "-password", "pass:test-password"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for profile, vendor in (("200", "b"), ("200", "c"), ("210", "a"), ("210", "b"), ("210", "c"),
                                    ("220", "a"), ("220", "b"), ("220", "c")):
                with self.subTest(profile=profile, vendor=vendor):
                    instance = f"mockprofile-{profile}-{vendor}-001"
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", 0))
                        port = probe.getsockname()[1]
                    base = f"https://localhost:{port}"
                    env = {**os.environ, "FOODTECH_MOCK_CLIENT_ID": "test-user", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                           "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
                    server = subprocess.Popen(["dotnet", str(DLL), "serve-ua-json", "--fixture",
                        str(ROOT / f"fixtures/{instance}.json"), "--cert", str(pfx), "--port", str(port)],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        for _ in range(100):
                            if server.poll() is not None: self.fail("Mock exited")
                            try:
                                if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200: break
                            except httpx.TransportError: time.sleep(.1)
                        else: self.fail("Mock did not start")
                        class Ingestor:
                            def __init__(self): self.records = []
                            def ingest(self, raw): self.records.append(raw)
                            def drain(self, stream): return []
                        class Failed:
                            def ingest(self, raw): raise RuntimeError("storage failure")
                        with self.assertRaises(ValueError):
                            collect(base, "test-user", "test-secret", str(cert), Ingestor(),
                                    count=1, timeout_seconds=10)
                        with self.assertRaisesRegex(RuntimeError, "storage failure"):
                            collect(base, "test-user", "test-secret", str(cert), Failed(),
                                    count=3, timeout_seconds=20, instance=instance)
                        ingestor = Ingestor()
                        self.assertEqual(collect(base, "test-user", "test-secret", str(cert), ingestor,
                                                 count=3, timeout_seconds=20, instance=instance), 3)
                        self.assertEqual([decode_record(raw)["sequenceNumber"] for raw in ingestor.records], [1, 2, 3])
                        results = [assess(str(i), raw) for i, raw in enumerate(ingestor.records)]
                        self.assertEqual([item.quality_status for item in results], ["VALID", "VALID", "QUARANTINED"])
                        self.assertTrue(all(item.mapping_version == f"mockprofile-{profile}-{vendor}-map-v1" for item in results))
                        if profile == "210":
                            self.assertIn("POT_TEMPERATURE", results[0].normalized)
                            self.assertNotIn("CHAMBER_TEMPERATURE", results[0].normalized)
                        if profile == "220":
                            self.assertEqual(results[0].normalized["CLIMATE_SETTING_PERCENT"]["value"], "100")
                            self.assertEqual(results[0].normalized["RELATIVE_HUMIDITY"]["value"], "98")
                            self.assertEqual(results[0].normalized["FAN_SPEED_LEVEL"]["value"], 4)
                        if vendor == "b":
                            self.assertEqual(results[0].normalized["OUTPUT_PERCENT"]["value"], "60")
                        elif vendor == "c":
                            self.assertEqual(results[0].normalized["SPEED_LEVEL"]["value"], 4 if profile == "220" else 6)
                        if vendor != "a":
                            self.assertNotIn("ROTATIONAL_SPEED_RPM", results[0].normalized)
                    finally:
                        server.terminate()
                        server.wait(timeout=5)
