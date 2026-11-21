"""Exercise the C# MQTT 5 Mock and Python subscriber over authenticated TLS."""

import os
import json
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import paho.mqtt.client as mqtt

from foodtech_collection_adapter.mqtt import TOPIC, collect
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"
FIXTURE = ROOT / "fixtures/mockprofile-300-a-001.json"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class MqttMockTests(unittest.TestCase):
    def test_tls_acl_and_qos1_collection(self):
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
                                       "--cert", str(pfx), "--port", str(port), "--bind-address", "0.0.0.0"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        self.fail("MQTT Mock exited before accepting connections")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail("MQTT Mock did not start")
                with socket.create_connection(("localhost", port), timeout=5) as connection:
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        ssl.create_default_context().wrap_socket(connection, server_hostname="localhost")
                rejected = threading.Event()
                invalid = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="foodtech-collector-invalid", protocol=mqtt.MQTTv5)
                invalid.tls_set_context(ssl.create_default_context(cafile=str(cert)))
                invalid.username_pw_set("test-user", "wrong-secret")
                invalid.on_connect = lambda c, u, f, reason, p: rejected.set() if reason.is_failure else None
                invalid.connect("localhost", port)
                invalid.loop_start()
                self.assertTrue(rejected.wait(5), "invalid credentials must be rejected")
                invalid.loop_stop()
                old_protocol_rejected = threading.Event()
                old = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="foodtech-collector-old", protocol=mqtt.MQTTv311)
                old.tls_set_context(ssl.create_default_context(cafile=str(cert)))
                old.username_pw_set("test-user", "test-secret")
                old.on_connect = lambda c, u, f, reason, p: old_protocol_rejected.set() if reason.is_failure else None
                old.connect("localhost", port)
                old.loop_start()
                self.assertTrue(old_protocol_rejected.wait(5), "MQTT 3 must be rejected")
                old.loop_stop()
                denied = threading.Event()
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="foodtech-collector-acl-test", protocol=mqtt.MQTTv5)
                client.tls_set_context(ssl.create_default_context(cafile=str(cert)))
                client.username_pw_set("test-user", "test-secret")
                client.on_connect = lambda c, u, f, reason, p: c.subscribe("foodtech/#", qos=1)
                client.on_subscribe = lambda c, u, mid, reasons, p: denied.set() if reasons[0].is_failure else None
                client.connect("localhost", port)
                client.loop_start()
                self.assertTrue(denied.wait(5), "wildcard subscription must be denied")
                client.disconnect()
                client.loop_stop()

                closed = threading.Event()
                writer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="foodtech-collector-writer", protocol=mqtt.MQTTv5)
                writer.tls_set_context(ssl.create_default_context(cafile=str(cert)))
                writer.username_pw_set("test-user", "test-secret")
                writer.on_connect = lambda c, u, f, reason, p: c.publish(TOPIC, "untrusted", qos=1)
                writer.on_disconnect = lambda c, u, flags, reason, p: closed.set()
                writer.connect("localhost", port)
                writer.loop_start()
                self.assertTrue(closed.wait(5), "external publishers must be disconnected")
                writer.loop_stop()

                class Ingestor:
                    def __init__(self):
                        self.records = []
                        self.results = []
                        self.times = []
                    def ingest(self, raw):
                        self.records.append(raw)
                        self.times.append(time.monotonic())
                        self.results.append(assess(str(len(self.records)), raw))
                    def drain(self, _stream):
                        return []
                class FailingIngestor:
                    def ingest(self, _raw):
                        raise RuntimeError("simulated storage failure")
                    def drain(self, _stream):
                        return []
                with self.assertRaisesRegex(RuntimeError, "not durably accepted"):
                    collect("localhost", port, str(cert), "test-user", "test-secret",
                            "foodtech-collector-integration", FailingIngestor(), count=3, timeout_seconds=20)
                ingestor = Ingestor()
                self.assertEqual(collect("localhost", port, str(cert), "test-user", "test-secret",
                                         "foodtech-collector-integration", ingestor, count=3, timeout_seconds=20), 3)
                self.assertEqual(len(ingestor.records), 3)
                self.assertTrue(all('"synthetic-mqtt"' in raw for raw in ingestor.records))
                self.assertEqual([json.loads(raw)["sequenceNumber"] for raw in ingestor.records], [1, 2, 3])
                self.assertGreaterEqual(ingestor.times[2] - ingestor.times[1], 4.5)
                self.assertEqual([item.quality_status for item in ingestor.results], ["VALID", "VALID", "QUARANTINED"])
                self.assertTrue(all(item.mapping_version == "mockprofile-300-a-map-v1" for item in ingestor.results))
            finally:
                server.terminate()
                server.wait(timeout=5)
