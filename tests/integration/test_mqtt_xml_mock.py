"""Synthetic XML messages over C# MQTT 5/TLS with XSD and QoS 1."""
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
class MqttXmlTests(unittest.TestCase):
    def test_vendor_xml_and_fault_redelivery(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                "-out", str(cert), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key), "-in", str(cert),
                "-password", "pass:test-password"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for profile, vendor, fault in (*[(profile, vendor, None) for profile in ("301", "311", "321") for vendor in "abc"], ("301", "a", "invalid-xml"), ("311", "a", "invalid-xml"), ("321", "c", "invalid-xml")):
                with self.subTest(profile=profile, vendor=vendor, fault=fault):
                    instance = f"mockprofile-{profile}-{vendor}-001"
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", 0))
                        port = probe.getsockname()[1]
                    env = {**os.environ, "FOODTECH_MQTT_USER": "test-user", "FOODTECH_MQTT_PASSWORD": "test-secret",
                           "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
                    command = ["dotnet", str(DLL), "serve-mqtt", "--fixture", str(ROOT / f"fixtures/{instance}.json"),
                               "--cert", str(pfx), "--port", str(port)]
                    if fault: command += ["--fault", fault]
                    server = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        for _ in range(100):
                            if server.poll() is not None: self.fail("MQTT Mock exited")
                            try:
                                with socket.create_connection(("127.0.0.1", port), timeout=.1): break
                            except OSError: time.sleep(.1)
                        else: self.fail("MQTT Mock did not start")
                        class Ingestor:
                            def __init__(self): self.records = []
                            def ingest(self, raw): self.records.append(raw)
                            def drain(self, stream): return []
                        class Failed:
                            def preserve_mqtt(self, raw, **kwargs): raise RuntimeError("storage failure")
                        class Sink:
                            def __init__(self): self.records = []
                            def preserve_mqtt(self, raw, **kwargs): self.records.append((raw, kwargs))
                        ingestor = Ingestor()
                        args = ("localhost", port, str(cert), "test-user", "test-secret", f"foodtech-collector-xml-{vendor}", ingestor)
                        if fault:
                            with self.assertRaisesRegex(RuntimeError, "not durably accepted"):
                                collect(*args, count=1, timeout_seconds=10, instance=instance, quarantine=Failed())
                            sink = Sink()
                            self.assertEqual(collect(*args, count=1, timeout_seconds=10, instance=instance, quarantine=sink), 1)
                            self.assertEqual(sink.records[0][0], b"<record><broken>")
                            self.assertEqual(sink.records[0][1]["profile_id"], f"mockprofile-{profile}")
                            self.assertEqual(ingestor.records, [])
                        else:
                            self.assertEqual(collect(*args, count=3, timeout_seconds=20, instance=instance), 3)
                            self.assertEqual([decode_record(raw)["sequenceNumber"] for raw in ingestor.records], [1, 2, 3])
                            results = [assess(str(i), raw) for i, raw in enumerate(ingestor.records)]
                            self.assertEqual([item.quality_status for item in results], ["VALID", "VALID", "QUARANTINED"])
                            self.assertTrue(all(item.mapping_version == f"mockprofile-{profile}-{vendor}-map-v1" for item in results))
                            self.assertTrue(all(raw.startswith("<record>") for raw in ingestor.records))
                            if profile == "311": self.assertIn("POT_TEMPERATURE", results[0].normalized)
                            if profile == "321": self.assertIn("RELATIVE_HUMIDITY", results[0].normalized)
                    finally:
                        server.terminate()
                        server.wait(timeout=5)
