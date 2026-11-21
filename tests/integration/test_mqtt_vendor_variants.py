"""Synthetic vendor-specific MQTT 5/TLS collection and normalization."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from foodtech_collection_adapter.mqtt import collect
from foodtech_contracts import decode_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class MqttVendorTests(unittest.TestCase):
    def test_equipment_vendors(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key), "-in", str(cert),
                            "-password", "pass:test-password"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for profile, vendor in (("300", "b"), ("300", "c"), *[(profile, vendor) for profile in ("310", "320") for vendor in "abc"]):
                with self.subTest(profile=profile, vendor=vendor):
                    instance = f"mockprofile-{profile}-{vendor}-001"
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", 0))
                        port = probe.getsockname()[1]
                    env = {**os.environ, "FOODTECH_MQTT_USER": "test-user", "FOODTECH_MQTT_PASSWORD": "test-secret",
                           "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
                    server = subprocess.Popen(["dotnet", str(DLL), "serve-mqtt", "--fixture",
                        str(ROOT / f"fixtures/{instance}.json"), "--cert", str(pfx), "--port", str(port)],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        for _ in range(100):
                            if server.poll() is not None: self.fail("Mock exited")
                            try:
                                with socket.create_connection(("127.0.0.1", port), timeout=.1): break
                            except OSError: time.sleep(.1)
                        else: self.fail("Mock did not start")
                        class Ingestor:
                            def __init__(self): self.records = []
                            def ingest(self, raw): self.records.append(raw)
                            def drain(self, stream): return []
                        ingestor = Ingestor()
                        foreign = f"mockprofile-{profile}-c-001" if vendor != "c" else f"mockprofile-{profile}-b-001"
                        with self.assertRaisesRegex(RuntimeError, "subscription rejected"):
                            collect("localhost", port, str(cert), "test-user", "test-secret",
                                    f"foodtech-collector-foreign-{vendor}", ingestor,
                                    count=1, timeout_seconds=5, instance=foreign)
                        self.assertEqual(ingestor.records, [])
                        self.assertEqual(collect("localhost", port, str(cert), "test-user", "test-secret",
                            f"foodtech-collector-{vendor}", ingestor, count=3, timeout_seconds=20, instance=instance), 3)
                        self.assertEqual([decode_record(raw)["sequenceNumber"] for raw in ingestor.records], [1, 2, 3])
                        assessments = [assess(str(i), raw) for i, raw in enumerate(ingestor.records)]
                        self.assertEqual([item.quality_status for item in assessments], ["VALID", "VALID", "QUARANTINED"])
                        self.assertTrue(all(item.mapping_version == f"mockprofile-{profile}-{vendor}-map-v1" for item in assessments))
                        if vendor == "b":
                            self.assertEqual(assessments[0].normalized["OUTPUT_PERCENT"]["value"], "60")
                        elif vendor == "c":
                            self.assertEqual(assessments[0].normalized["SPEED_LEVEL"]["value"], 4 if profile == "320" else 6)
                        if vendor != "a":
                            self.assertNotIn("ROTATIONAL_SPEED_RPM", assessments[0].normalized)
                        if profile == "310": self.assertIn("POT_TEMPERATURE", assessments[0].normalized)
                        if profile == "320": self.assertIn("RELATIVE_HUMIDITY", assessments[0].normalized)
                        self.assertEqual(json.loads(ingestor.records[0])[{"a": "sourceId", "b": "source_id", "c": "SrcId"}[vendor]], "synthetic-mqtt")
                    finally:
                        server.terminate()
                        server.wait(timeout=5)
