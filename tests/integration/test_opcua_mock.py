"""Real certificate-trusted OPC UA Binary Browse/Read/Subscription boundary."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa

from foodtech_collection_adapter.opcua import NAMESPACE, collect, validate_server_certificate
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"
FIXTURE = ROOT / "fixtures/mockprofile-203-a-001.json"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class OpcUaMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_readonly_subscription(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            pki, client_key, client_pem = temp / "pki", temp / "client-key.pem", temp / "client.pem"
            trusted = pki / "trusted/certs"
            trusted.mkdir(parents=True)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(client_key),
                "-out", str(client_pem), "-days", "1", "-subj", "/CN=FoodtechTestClient/O=Foodtech",
                "-addext", "basicConstraints=critical,CA:FALSE", "-addext",
                f"subjectAltName=URI:urn:foodtech:opcua:collector,DNS:localhost,DNS:{socket.gethostname()}",
                "-addext", "keyUsage=critical,digitalSignature,nonRepudiation,keyEncipherment,dataEncipherment",
                "-addext", "extendedKeyUsage=clientAuth,serverAuth"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["openssl", "x509", "-in", str(client_pem), "-outform", "DER", "-out", str(trusted / "client.der")],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            endpoint = f"opc.tcp://localhost:{port}/foodtech"
            server = subprocess.Popen(["dotnet", str(DLL), "serve-opcua", "--fixture", str(FIXTURE),
                                       "--pki", str(pki), "--port", str(port)], env=os.environ.copy(),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(150):
                    if server.poll() is not None:
                        self.fail("OPC UA Mock exited during startup")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.1):
                            break
                    except OSError:
                        await asyncio.sleep(.1)
                else:
                    self.fail("OPC UA Mock did not start")
                server_cert = next(path for path in (pki / "certs").glob("*.der")
                                   if isinstance(x509.load_der_x509_certificate(path.read_bytes()).public_key(), rsa.RSAPublicKey))
                validate_server_certificate(endpoint, str(server_cert))
                with self.assertRaises(ValueError):
                    validate_server_certificate(endpoint, str(trusted / "client.der"))
                client = Client(endpoint)
                client.application_uri = "urn:foodtech:opcua:collector"
                await client.set_security(SecurityPolicyBasic256Sha256, str(client_pem), str(client_key),
                                          server_certificate=str(server_cert), mode=ua.MessageSecurityMode.SignAndEncrypt)
                for attempt in range(100):
                    try:
                        await client.connect()
                        break
                    except ua.uaerrors.BadServerHalted:
                        if attempt == 99:
                            raise
                        await asyncio.sleep(.2)
                try:
                    index = await client.get_namespace_index(NAMESPACE)
                    device = await client.nodes.objects.get_child([f"{index}:SyntheticDevice"])
                    for child in await device.get_children():
                        self.assertNotEqual(await child.read_node_class(), ua.NodeClass.Method)
                    sequence = await device.get_child([f"{index}:Sequence"])
                    self.assertEqual(await sequence.read_value(), 1)
                    self.assertNotIn(ua.AccessLevel.CurrentWrite, await sequence.get_access_level())
                    with self.assertRaises(ua.UaStatusCodeError):
                        await sequence.write_value(ua.Variant(99, ua.VariantType.Int32))
                finally:
                    await client.disconnect()

                unknown_key, unknown_pem = temp / "unknown-key.pem", temp / "unknown.pem"
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(unknown_key),
                    "-out", str(unknown_pem), "-days", "1", "-subj", "/CN=UnknownClient/O=Foodtech",
                    "-addext", "basicConstraints=critical,CA:FALSE", "-addext",
                    f"subjectAltName=URI:urn:foodtech:opcua:collector,DNS:localhost,DNS:{socket.gethostname()}",
                    "-addext", "keyUsage=critical,digitalSignature,nonRepudiation,keyEncipherment,dataEncipherment",
                    "-addext", "extendedKeyUsage=clientAuth,serverAuth"],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                unknown = Client(endpoint)
                unknown.application_uri = "urn:foodtech:opcua:collector"
                await unknown.set_security(SecurityPolicyBasic256Sha256, str(unknown_pem), str(unknown_key),
                                           server_certificate=str(server_cert), mode=ua.MessageSecurityMode.SignAndEncrypt)
                with self.assertRaises(ua.UaStatusCodeError):
                    await asyncio.wait_for(unknown.connect(), timeout=5)
                unencrypted = Client(endpoint)
                with self.assertRaises(ua.UaStatusCodeError):
                    await asyncio.wait_for(unencrypted.connect(), timeout=5)

                class Ingestor:
                    def __init__(self):
                        self.records = []
                        self.results = []
                        self.times = []
                    def ingest(self, raw):
                        self.records.append(json.loads(raw))
                        self.times.append(time.monotonic())
                        self.results.append(assess(str(len(self.records)), raw))
                    def drain(self, _stream):
                        return []
                ingestor = Ingestor()
                self.assertEqual(await collect(endpoint, str(client_pem), str(client_key), str(server_cert), ingestor,
                                               count=3, timeout_seconds=25), 3)
                self.assertEqual([record["sequenceNumber"] for record in ingestor.records], [1, 2, 3])
                self.assertGreaterEqual(ingestor.times[2] - ingestor.times[1], 4.3)
                self.assertEqual([result.quality_status for result in ingestor.results], ["VALID", "VALID", "QUARANTINED"])
                self.assertTrue(all(result.mapping_version == "mockprofile-203-a-map-v1" for result in ingestor.results))
            finally:
                server.terminate()
                server.wait(timeout=10)
