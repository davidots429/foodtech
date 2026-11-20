"""Exercise MQTT session recovery after a real TCP interruption."""
import os
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from foodtech_collection_adapter.mqtt import collect
from foodtech_contracts import decode_record

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class MqttReconnectTests(unittest.TestCase):
    def test_interrupted_tcp_resumes_existing_mqtt_session(self):
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
                probe.bind(("127.0.0.1", 0)); broker_port = probe.getsockname()[1]
            env = {**os.environ, "FOODTECH_MQTT_USER": "test-user", "FOODTECH_MQTT_PASSWORD": "test-secret",
                   "FOODTECH_MOCK_CERT_PASSWORD": "test-password"}
            server = subprocess.Popen(["dotnet", str(DLL), "serve-mqtt", "--fixture",
                str(ROOT / "fixtures/mockprofile-300-a-001.json"), "--cert", str(pfx), "--port", str(broker_port)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            stop = threading.Event()
            listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(); listener.settimeout(.2)
            proxy_port = listener.getsockname()[1]
            dropped = threading.Event()
            def proxy():
                first = True
                while not stop.is_set():
                    try: incoming, _ = listener.accept()
                    except socket.timeout: continue
                    except OSError: return
                    try:
                        with incoming, socket.create_connection(("127.0.0.1", broker_port), timeout=2) as upstream:
                            started = time.monotonic()
                            while not stop.is_set():
                                if first and time.monotonic() - started > 1.5:
                                    dropped.set(); first = False; break
                                readable, _, _ = select.select([incoming, upstream], [], [], .1)
                                for current in readable:
                                    data = current.recv(65536)
                                    if not data: raise EOFError
                                    (upstream if current is incoming else incoming).sendall(data)
                    except (EOFError, OSError):
                        pass
            thread = threading.Thread(target=proxy, daemon=True)
            try:
                for _ in range(100):
                    if server.poll() is not None: self.fail("MQTT Mock exited")
                    try:
                        with socket.create_connection(("127.0.0.1", broker_port), timeout=.1): break
                    except OSError: time.sleep(.1)
                else: self.fail("MQTT Mock did not start")
                thread.start()
                class Ingestor:
                    def __init__(self): self.records = []
                    def ingest(self, raw): self.records.append(raw)
                    def drain(self, stream): return []
                ingestor = Ingestor()
                self.assertEqual(collect("localhost", proxy_port, str(cert), "test-user", "test-secret",
                    "foodtech-collector-reconnect", ingestor, count=3, timeout_seconds=25), 3)
                self.assertTrue(dropped.is_set())
                self.assertEqual([decode_record(raw)["sequenceNumber"] for raw in ingestor.records], [1, 2, 3])
            finally:
                stop.set(); listener.close()
                if thread.is_alive(): thread.join(timeout=3)
                server.terminate(); server.wait(timeout=5)
