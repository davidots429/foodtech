"""Ensure malformed REST UTF-8 is preserved as bytes before cursor advancement."""
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

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"
FIXTURE = ROOT / "fixtures/mockprofile-100-a-001.json"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class RestEncodingTests(unittest.TestCase):
    def test_original_bytes_and_cursor_after_durable_quarantine(self):
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
                                       "--port", str(port), "--bind-address", "0.0.0.0", "--fault", "invalid-utf8"], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if server.poll() is not None: self.fail("Mock exited")
                    try:
                        if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200: break
                    except httpx.TransportError: time.sleep(.1)
                else: self.fail("Mock did not start")
                calls = []
                class Ingestor:
                    def __init__(self, *args): pass
                    def cursor_state(self, key): return 0, set()
                    def ingest(self, raw): calls.append("ingest"); return []
                    def drain(self, stream): return []
                    def save_cursor(self, cursor, streams, key): calls.append(("cursor", cursor))
                class Sink:
                    def __init__(self, *args): pass
                    def preserve(self, raw, **kwargs): calls.append(("preserve", raw, kwargs["reason"]))
                config = {"FOODTECH_MOCK_BASE_URL": base, "FOODTECH_MOCK_CA_FILE": str(cert),
                          "FOODTECH_MOCK_CLIENT_ID": "test-client", "FOODTECH_MOCK_CLIENT_SECRET": "test-secret",
                          "FOODTECH_DATA_TABLE": "test", "FOODTECH_ORDERED_QUEUE_URL": "test",
                          "FOODTECH_INGESTION_DLQ_BUCKET": "test"}
                with patch.dict(os.environ, config), patch.object(cli, "DynamoOrderedIngestor", Ingestor), \
                     patch.object(cli, "IngestionQuarantine", Sink), patch("sys.argv", ["collector"]):
                    self.assertEqual(cli.main(), 0)
                self.assertEqual(calls[0], ("preserve", b'\xff\xfe\x80', "UNSUPPORTED_ENCODING"))
                self.assertEqual(calls[-1], ("cursor", 3))
                self.assertEqual(calls.count("ingest"), 2)
                calls.clear()
                with patch.dict(os.environ, config), patch.object(cli, "DynamoOrderedIngestor", Ingestor), \
                     patch.object(cli, "IngestionQuarantine", Sink), \
                     patch.object(Sink, "preserve", side_effect=RuntimeError("storage unavailable")), \
                     patch("sys.argv", ["collector"]):
                    with self.assertRaisesRegex(RuntimeError, "storage unavailable"): cli.main()
                self.assertEqual(calls, [])
            finally:
                server.terminate()
                server.wait(timeout=5)
