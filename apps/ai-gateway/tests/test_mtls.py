import asyncio
import ipaddress
import socket
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from foodtech_ai_gateway.main import app
from foodtech_business_api.ai import _client as business_gateway_client


def _certificates(directory: Path) -> tuple[str, str, str, str, str]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Foodtech test business CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))

    def issue(name: str, usage, *, server: bool) -> tuple[str, str]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (x509.CertificateBuilder().subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
        if server:
            builder = builder.add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        certificate = builder.sign(ca_key, hashes.SHA256())
        cert_path = directory / f"{name}.pem"
        key_path = directory / f"{name}.key"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        return str(cert_path), str(key_path)

    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    server_cert, server_key = issue("gateway", ExtendedKeyUsageOID.SERVER_AUTH, server=True)
    client_cert, client_key = issue("business-api", ExtendedKeyUsageOID.CLIENT_AUTH, server=False)
    return str(ca_path), server_cert, server_key, client_cert, client_key


class MutualTlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_business_client_certificate_reaches_gateway(self):
        with tempfile.TemporaryDirectory() as temp:
            ca, server_cert, server_key, client_cert, client_key = _certificates(Path(temp))
            app.state.mtls_listener = True
            app.state.gateway_service = SimpleNamespace(registry=SimpleNamespace(
                create_execution=lambda execution: None))
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            port = listener.getsockname()[1]
            config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                    ssl_certfile=server_cert, ssl_keyfile=server_key,
                                    ssl_ca_certs=ca, ssl_cert_reqs=ssl.CERT_REQUIRED,
                                    proxy_headers=False, access_log=False, log_level="error",
                                    lifespan="off")
            server = uvicorn.Server(config)
            task = asyncio.create_task(server.serve(sockets=[listener]))
            loop = asyncio.get_running_loop()
            previous_handler = loop.get_exception_handler()

            def expected_handshake_failure(loop, context):
                error = context.get("exception")
                if isinstance(error, ssl.SSLError) and "PEER_DID_NOT_RETURN_A_CERTIFICATE" in str(error):
                    return
                if previous_handler:
                    previous_handler(loop, context)
                else:
                    loop.default_exception_handler(context)

            loop.set_exception_handler(expected_handshake_failure)
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(server.started)
                url = f"https://127.0.0.1:{port}/internal/ai/executions"
                anonymous_context = ssl.create_default_context(cafile=ca)
                async with httpx.AsyncClient(verify=anonymous_context, timeout=2,
                                             trust_env=False) as client:
                    with self.assertRaises(httpx.TransportError):
                        await client.post(url, json={"tenant_id": "tenant-a"})
                async with business_gateway_client(ca, client_cert, client_key) as client:
                    response = await client.post(url, json={"tenant_id": "tenant-a"})
                self.assertEqual(response.status_code, 201)
            finally:
                server.should_exit = True
                await task
                listener.close()
                loop.set_exception_handler(previous_handler)
                del app.state.gateway_service
                del app.state.mtls_listener


if __name__ == "__main__":
    unittest.main()
