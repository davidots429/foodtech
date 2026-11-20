"""Exercise a malformed C# MQTT delivery over actual TLS and QoS 1."""
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from foodtech_collection_adapter.mqtt import collect

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"
FIXTURE = ROOT / "fixtures/mockprofile-300-a-001.json"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class MqttQuarantineIntegrationTests(unittest.TestCase):
    def test_encoding_failure_redelivery_after_failed_storage(self):
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
            env = {**os.environ, "FOODTECH_MQTT_USER": "test-user", "FOODTECH_MQTT_PASSWORD": "test-secret",
                   "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
            server = subprocess.Popen(["dotnet", str(DLL), "serve-mqtt", "--fixture", str(FIXTURE),
                "--cert", str(pfx), "--port", str(port), "--fault", "invalid-utf8"], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if server.poll() is not None: self.fail("MQTT Mock exited")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.1): break
                    except OSError: time.sleep(.1)
                else: self.fail("MQTT Mock did not start")
                class Ingestor:
                    def ingest(self, raw): raise AssertionError("malformed message entered normal pipeline")
                    def drain(self, stream): return []
                class Failed:
                    def preserve_mqtt(self, raw, **kwargs): raise RuntimeError("storage unavailable")
                class Sink:
                    def __init__(self): self.items = []
                    def preserve_mqtt(self, raw, **kwargs): self.items.append((raw, kwargs))
                args = ("localhost", port, str(cert), "test-user", "test-secret", "foodtech-collector-fault", Ingestor())
                with self.assertRaisesRegex(RuntimeError, "not durably accepted"):
                    collect(*args, count=1, timeout_seconds=10, quarantine=Failed())
                sink = Sink()
                self.assertEqual(collect(*args, count=1, timeout_seconds=10, quarantine=sink), 1)
                self.assertEqual(sink.items[0][0], b'\xff\xfe\x80')
                self.assertEqual(sink.items[0][1]["reason"], "UNSUPPORTED_ENCODING")
            finally:
                server.terminate()
                server.wait(timeout=5)
