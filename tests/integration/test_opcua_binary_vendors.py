"""Certificate-trusted synthetic OPC UA Binary sources for vendor-b and vendor-c."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa

from foodtech_collection_adapter.opcua import collect
from foodtech_contracts import decode_record
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class BinaryVendorTests(unittest.IsolatedAsyncioTestCase):
    def test_mock_rejects_invalid_source_state_before_listening(self):
        with tempfile.TemporaryDirectory() as directory:
            for vendor in ("b", "c"):
                data = json.loads((ROOT / f"fixtures/mockprofile-203-{vendor}-001.json").read_text())
                data["records"][0]["device_status" if vendor == "b" else "Status"] = 9 if vendor == "b" else "UNKNOWN"
                fixture = Path(directory) / f"invalid-{vendor}.json"
                fixture.write_text(json.dumps(data))
                result = subprocess.run(["dotnet", str(DLL), "serve-opcua", "--fixture", str(fixture),
                                         "--pki", str(Path(directory) / "pki"), "--port", "48123"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                self.assertNotEqual(result.returncode, 0)

    async def test_vendor_b_and_c_readonly_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            key, pem, der = temp / "key.pem", temp / "client.pem", temp / "client.der"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                "-out", str(pem), "-days", "1", "-subj", "/CN=FoodtechTestClient/O=Foodtech",
                "-addext", "basicConstraints=critical,CA:FALSE", "-addext",
                f"subjectAltName=URI:urn:foodtech:opcua:collector,DNS:localhost,DNS:{socket.gethostname()}",
                "-addext", "keyUsage=critical,digitalSignature,nonRepudiation,keyEncipherment,dataEncipherment",
                "-addext", "extendedKeyUsage=clientAuth,serverAuth"], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "x509", "-in", str(pem), "-outform", "DER", "-out", str(der)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for profile, vendor in (("203", "b"), ("203", "c"), *[(profile, vendor) for profile in ("213", "223") for vendor in "abc"]):
                with self.subTest(profile=profile, vendor=vendor):
                    instance = f"mockprofile-{profile}-{vendor}-001"
                    pki = temp / f"pki-{profile}-{vendor}"
                    trusted = pki / "trusted/certs"
                    trusted.mkdir(parents=True)
                    shutil.copyfile(der, trusted / "client.der")
                    with socket.socket() as probe:
                        probe.bind(("127.0.0.1", 0))
                        port = probe.getsockname()[1]
                    endpoint = f"opc.tcp://localhost:{port}/foodtech"
                    server = subprocess.Popen(["dotnet", str(DLL), "serve-opcua", "--fixture",
                        str(ROOT / f"fixtures/{instance}.json"), "--pki", str(pki), "--port", str(port)],
                        env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        for _ in range(150):
                            if server.poll() is not None: self.fail("UA Mock exited")
                            try:
                                with socket.create_connection(("127.0.0.1", port), timeout=.1): break
                            except OSError: await asyncio.sleep(.1)
                        else: self.fail("UA Mock did not start")
                        server_cert = next(path for path in (pki / "certs").glob("*.der")
                                           if isinstance(x509.load_der_x509_certificate(path.read_bytes()).public_key(), rsa.RSAPublicKey))
                        client = Client(endpoint)
                        client.application_uri = "urn:foodtech:opcua:collector"
                        await client.set_security(SecurityPolicyBasic256Sha256, str(pem), str(key),
                                                  server_certificate=str(server_cert), mode=ua.MessageSecurityMode.SignAndEncrypt)
                        for attempt in range(100):
                            try:
                                await client.connect()
                                break
                            except ua.uaerrors.BadServerHalted:
                                if attempt == 99: raise
                                await asyncio.sleep(.2)
                        try:
                            index = await client.get_namespace_index(f"urn:foodtech:synthetic:opcua:{profile}:{vendor}:v1")
                            device = await client.nodes.objects.get_child([f"{index}:SyntheticDevice"])
                            temperature = await device.get_child([f"{index}:TemperatureCelsius"])
                            self.assertAlmostEqual(await temperature.read_value(), 180)
                            self.assertNotIn(ua.AccessLevel.CurrentWrite, await temperature.get_access_level())
                            speed = await device.get_child([f"{index}:OutputPercent" if vendor == "b" else f"{index}:SpeedLevel" if vendor == "c" else f"{index}:SpeedRpm"])
                            expected_speed = 60 if vendor in ("a", "b") else 4 if profile == "223" else 6
                            self.assertEqual(await speed.read_value(), expected_speed)
                        finally:
                            await client.disconnect()
                        class Failed:
                            def ingest(self, raw): raise RuntimeError("storage failure")
                        class Ingestor:
                            def __init__(self): self.records = []
                            def ingest(self, raw): self.records.append(raw)
                            def drain(self, stream): return []
                        with self.assertRaisesRegex(RuntimeError, "storage failure"):
                            await collect(endpoint, str(pem), str(key), str(server_cert), Failed(),
                                          count=3, timeout_seconds=30, instance=instance)
                        ingestor = Ingestor()
                        self.assertEqual(await collect(endpoint, str(pem), str(key), str(server_cert), ingestor,
                                                       count=3, timeout_seconds=30, instance=instance), 3)
                        self.assertEqual([decode_record(raw)["sequenceNumber"] for raw in ingestor.records], [1, 2, 3])
                        results = [assess(str(i), raw) for i, raw in enumerate(ingestor.records)]
                        self.assertEqual([item.quality_status for item in results], ["VALID", "VALID", "QUARANTINED"])
                        self.assertTrue(all(item.mapping_version == f"mockprofile-{profile}-{vendor}-map-v1" for item in results))
                        if vendor != "a":
                            self.assertNotIn("ROTATIONAL_SPEED_RPM", results[0].normalized)
                        if profile == "213":
                            self.assertIn("POT_TEMPERATURE", results[0].normalized)
                        if profile == "223":
                            self.assertIn("RELATIVE_HUMIDITY", results[0].normalized)
                    finally:
                        server.terminate()
                        server.wait(timeout=10)
