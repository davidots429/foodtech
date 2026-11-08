"""Local browser harness with real HTTP/mTLS and synthetic-only backends.

No AWS API or paid provider is called. Auth, storage, KMS and provider behavior
are explicit in-memory test doubles. The customer UI and three HTTP services
are the production applications.
"""

import asyncio
import argparse
import base64
import ipaddress
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

import httpx
import uvicorn
import boto3
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "ai-gateway" / "tests"))
from test_registry import AtomicClient, MemoryResource  # noqa: E402

from foodtech_ai_gateway.main import app as gateway_app  # noqa: E402
from foodtech_ai_gateway.policy import Credential, Model, Tier  # noqa: E402
from foodtech_ai_gateway.provider import ProviderResult  # noqa: E402
from foodtech_ai_gateway.registry import DynamoRegistry  # noqa: E402
from foodtech_ai_gateway.service import GatewayService  # noqa: E402
from foodtech_ai_gateway.vault import DynamoCredentialVault  # noqa: E402
from foodtech_business_api import ai as business_ai  # noqa: E402
from foodtech_business_api import auth as business_auth  # noqa: E402
from foodtech_business_api import credentials as business_credentials  # noqa: E402
from foodtech_business_api.main import app as business_app  # noqa: E402
from foodtech_credential_registration.main import app as registration_app  # noqa: E402
from foodtech_credential_registration.registration import (  # noqa: E402
    CredentialRegistrar, TenantPseudonymizer, TransferKey,
)
from foodtech_credential_registration.storage import CredentialWriter  # noqa: E402


TENANT = "synthetic-browser-customer"
CSRF = "synthetic-browser-csrf"
DUMMY_KEY = "synthetic-browser-key"
MODEL = "gpt-4o-mini"
REGION = "ap-southeast-2"
ACCOUNT = "573693340100"
REGISTRY_TABLE = "foodtech-ai-execution-registry-dev"
VAULT_TABLE = "foodtech-ai-credential-vault-dev"
VAULT_STACK = "FoodtechCredentialVault-dev"


def free_listener() -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    return listener


def certificates(directory: Path) -> tuple[str, str, str, str, str, str, str]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Foodtech synthetic CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))

    def issue(name: str, usage: ExtendedKeyUsageOID, *, server: bool) -> tuple[str, str]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        builder = (x509.CertificateBuilder().subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
        if server:
            builder = builder.add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        certificate = builder.sign(ca_key, hashes.SHA256())
        cert_path, key_path = directory / f"{name}.pem", directory / f"{name}.key"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        key_path.chmod(0o600)
        return str(cert_path), str(key_path)

    gateway_cert, gateway_key = issue("gateway", ExtendedKeyUsageOID.SERVER_AUTH, server=True)
    registration_cert, registration_key = issue(
        "registration", ExtendedKeyUsageOID.SERVER_AUTH, server=True)
    client_cert, client_key = issue("business", ExtendedKeyUsageOID.CLIENT_AUTH, server=False)
    return str(ca_path), gateway_cert, gateway_key, registration_cert, registration_key, client_cert, client_key


class SyntheticRegistrar:
    def __init__(self, registry: DynamoRegistry, secrets: dict[str, bytes]):
        self.registry = registry
        self.secrets = secrets
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        self.transfer = self

    def public_descriptor(self) -> dict:
        public = self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"key_version": "synthetic-v1", "algorithm": "RSA-OAEP-SHA-256",
                "public_key_spki_base64": base64.b64encode(public).decode(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}

    def register(self, tenant_id: str, provider: str, key_version: str,
                 ciphertext_base64: str) -> str:
        if tenant_id != TENANT or provider != "openai" or key_version != "synthetic-v1":
            raise ValueError("synthetic registration contract rejected")
        plaintext = self.private.decrypt(base64.b64decode(ciphertext_base64, validate=True),
                                         padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                      algorithm=hashes.SHA256(), label=None))
        if plaintext != DUMMY_KEY.encode():
            raise ValueError("only the published synthetic key is accepted")
        credential_id = str(uuid4())
        self.secrets[credential_id] = plaintext
        self.registry.create_credential(Credential(
            credential_id, tenant_id, provider, "ACTIVE", None, "synthetic-pseudonym"),
            vault_ref=f"vault:{credential_id}")
        return credential_id


class SyntheticVault:
    def __init__(self, secrets: dict[str, bytes]):
        self.secrets = secrets

    def read(self, credential: Credential) -> str:
        if credential.vault_ref != f"vault:{credential.credential_id}":
            raise ValueError("vault reference mismatch")
        return credential.credential_id

    def decrypt(self, credential_id: str) -> bytearray:
        return bytearray(self.secrets[credential_id])


class SyntheticProvider:
    async def generate(self, provider: str, model_id: str, prompt: str,
                       api_key: bytearray) -> ProviderResult:
        if provider != "openai" or model_id != MODEL or \
                bytes(api_key) != DUMMY_KEY.encode() or "synthetic" not in prompt:
            raise ValueError("synthetic provider contract rejected")
        return ProviderResult("합성 공급자 응답: 권장 조리조건은 운영자가 검토한 뒤 활용합니다.",
                              Decimal("0.0002"), 24, 30)


async def start_server(app, listener: socket.socket, *, cert: str | None = None,
                       key: str | None = None, ca: str | None = None):
    config = uvicorn.Config(app, host="127.0.0.1", port=listener.getsockname()[1],
                            ssl_certfile=cert, ssl_keyfile=key, ssl_ca_certs=ca,
                            ssl_cert_reqs=ssl.CERT_REQUIRED if cert else ssl.CERT_NONE,
                            proxy_headers=False, access_log=False, log_level="error",
                            lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(100):
        if server.started:
            return server, task
        await asyncio.sleep(0.05)
    raise TimeoutError("test service did not start")


async def wait_for_web() -> None:
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        for _ in range(100):
            try:
                response = await client.get("http://127.0.0.1:5173/")
                if response.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError("customer web did not start")


async def smoke_test(created: dict[str, str]) -> dict:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:5173", timeout=90,
                                 trust_env=False) as client:
        account = await client.get("/api/customer/me")
        assert account.status_code == 200 and account.json()["csrf"] == CSRF
        descriptor_response = await client.get("/api/customer/credentials/transfer-key")
        assert descriptor_response.status_code == 200
        descriptor = descriptor_response.json()
        public = serialization.load_der_public_key(base64.b64decode(
            descriptor["public_key_spki_base64"]))
        ciphertext = public.encrypt(DUMMY_KEY.encode(), padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        headers = {"X-CSRF-Token": CSRF}
        registered = await client.post("/api/customer/credentials", headers=headers,
                                       json={"provider": "openai", "key_version": descriptor["key_version"],
                                             "ciphertext_base64": base64.b64encode(ciphertext).decode()})
        assert registered.status_code == 201, registered.text
        credential_id = registered.json()["credential_id"]
        created["credential_id"] = credential_id
        preview = await client.post("/api/customer/ai/external/preview", headers=headers,
                                    json={"model_id": MODEL, "credential_id": credential_id})
        assert preview.status_code == 201, preview.text
        quote = preview.json()
        created["quote_id"] = quote["quote_id"]
        created["execution_id"] = quote["execution_id"]
        confirmed = await client.post(
            f"/api/customer/ai/external/quotes/{quote['quote_id']}/confirm",
            headers=headers, json={"notice_version": quote["notice_version"]})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["state"] == "SUCCEEDED"
        status = await client.get(f"/api/customer/ai/executions/{quote['execution_id']}/status")
        assert status.status_code == 200 and status.json()["actual_cost_usd"] == "0.0002"
        return {"registration": True, "preview": True, "consent": True,
                "synthetic_execution": True, "settlement": True, "mtls": True}


def aws_backends():
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("unexpected AWS account")
    resource = session.resource("dynamodb")
    if resource.Table(REGISTRY_TABLE).scan(Select="COUNT")["Count"] != 0 or \
            resource.Table(VAULT_TABLE).scan(Select="COUNT")["Count"] != 0:
        raise RuntimeError("development credential tables must be empty for this test")
    outputs = session.client("cloudformation").describe_stacks(StackName=VAULT_STACK)
    keys = {entry["OutputKey"]: entry["OutputValue"]
            for entry in outputs["Stacks"][0]["Outputs"]}
    kms = session.client("kms")
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    registry = DynamoRegistry(REGISTRY_TABLE, period, resource=resource,
                              client=session.client("dynamodb"))
    registrar = CredentialRegistrar(
        CredentialWriter(VAULT_TABLE, "dev", keys["CredentialStorageKeyArn"],
                         resource=resource, kms_client=kms), REGISTRY_TABLE,
        TransferKey(keys["CredentialTransferKeyArn"], period, kms_client=kms),
        TenantPseudonymizer(keys["CredentialPseudonymKeyArn"], "v1", kms_client=kms),
        dynamodb_client=session.client("dynamodb"))
    vault = DynamoCredentialVault(VAULT_TABLE, "dev", {keys["CredentialStorageKeyArn"]},
                                  resource=resource, kms_client=kms)
    return registry, registrar, vault, resource


def cleanup_aws(resource, period: str, created: dict[str, str]) -> None:
    registry_table, vault_table = resource.Table(REGISTRY_TABLE), resource.Table(VAULT_TABLE)
    credential_id = created.get("credential_id")
    if credential_id:
        key = {"pk": f"CREDENTIAL#{credential_id}", "sk": "META"}
        vault_table.delete_item(Key=key)
        registry_table.delete_item(Key=key)
    for kind, identifier, sort in (
        ("EXEC", created.get("execution_id"), "RESULT"),
        ("EXEC", created.get("execution_id"), "META"),
        ("CONSENT", created.get("quote_id"), "META"),
        ("QUOTE", created.get("quote_id"), "META"),
        ("MODEL", MODEL, "META"),
        ("LIMIT", f"{period}#TENANT#{TENANT}", "META"),
        ("LIMIT", f"{period}#PROVIDER#openai", "META"),
        ("LIMIT", f"{period}#PROJECT#GLOBAL", "META"),
    ):
        if identifier:
            registry_table.delete_item(Key={"pk": f"{kind}#{identifier}", "sk": sort})


async def main(*, smoke_only: bool, aws: bool) -> None:
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    resource = None
    if aws:
        registry, registrar, vault, resource = aws_backends()
    else:
        registry_resource = MemoryResource()
        registry = DynamoRegistry("synthetic-browser-registry", period,
                                  resource=registry_resource,
                                  client=AtomicClient(registry_resource.table))
        secrets: dict[str, bytes] = {}
        registrar = SyntheticRegistrar(registry, secrets)
        vault = SyntheticVault(secrets)
    created: dict[str, str] = {}
    temp = tempfile.TemporaryDirectory(prefix="foodtech-synthetic-browser-")
    servers = []
    listeners = []
    vite = None
    try:
        now = datetime.now(timezone.utc)
        registry.create_model(Model(MODEL, "openai", Tier.TIER_3, "ACTIVE", False,
                                    True, now, now,
                                    input_cost_ceiling_usd=Decimal("0.000001"),
                                    output_cost_ceiling_usd=Decimal("0.000001"),
                                    price_version="synthetic-v1"))
        registry.create_limit("TENANT", TENANT, Decimal("1"), calls_remaining=3)
        registry.create_limit("PROVIDER", "openai", Decimal("1"))
        registry.create_limit("PROJECT", "GLOBAL", Decimal("1"))
        ca, gateway_cert, gateway_key, registration_cert, registration_key, \
            client_cert, client_key = certificates(Path(temp.name))
        gateway_listener, registration_listener, business_listener = (
            free_listener(), free_listener(), free_listener())
        listeners = [gateway_listener, registration_listener, business_listener]
        gateway_port = gateway_listener.getsockname()[1]
        registration_port = registration_listener.getsockname()[1]
        business_port = business_listener.getsockname()[1]
        gateway_app.state.mtls_listener = True
        gateway_app.state.gateway_service = GatewayService(
            registry, vault=vault, provider=SyntheticProvider())
        registration_app.state.mtls_listener = True
        registration_app.state.credential_registrar = registrar

        async def synthetic_session(_request, surface):
            if surface != "customer":
                raise ValueError("synthetic harness permits customer only")
            return SimpleNamespace(subject=TENANT, csrf=CSRF, surface="customer",
                                   email="synthetic@example.invalid")

        config = {"FOODTECH_EXTERNAL_EXECUTION_ENABLED": "true",
                  "FOODTECH_GATEWAY_URL": f"https://127.0.0.1:{gateway_port}",
                  "FOODTECH_GATEWAY_CA": ca,
                  "FOODTECH_REGISTRATION_URL": f"https://127.0.0.1:{registration_port}",
                  "FOODTECH_REGISTRATION_CA": ca,
                  "FOODTECH_BUSINESS_CLIENT_CERT": client_cert,
                  "FOODTECH_BUSINESS_CLIENT_KEY": client_key}
        with patch.dict(os.environ, config), \
             patch.object(business_auth, "_session", side_effect=synthetic_session), \
             patch.object(business_ai, "_session", side_effect=synthetic_session), \
             patch.object(business_credentials, "_session", side_effect=synthetic_session):
            servers.append(await start_server(gateway_app, gateway_listener,
                                              cert=gateway_cert, key=gateway_key, ca=ca))
            servers.append(await start_server(registration_app, registration_listener,
                                              cert=registration_cert, key=registration_key, ca=ca))
            servers.append(await start_server(business_app, business_listener))
            with (Path(temp.name) / "vite.log").open("wb") as log:
                vite = subprocess.Popen(
                    ["npm", "run", "dev", "--workspace=@foodtech/customer-web", "--",
                     "--host", "127.0.0.1"], cwd=ROOT, stdout=log, stderr=log,
                    env={**os.environ, "FOODTECH_API_PROXY_TARGET":
                         f"http://127.0.0.1:{business_port}"})
                await wait_for_web()
                # AWS SDK calls are synchronous inside the service handlers. Keep
                # the test client on its own loop so they do not stall its timeout.
                result = await asyncio.to_thread(lambda: asyncio.run(smoke_test(created)))
                print(json.dumps({"url": "http://127.0.0.1:5173/",
                                  "synthetic_key": DUMMY_KEY, "aws_backends": aws,
                                  "smoke": result},
                                 ensure_ascii=False), flush=True)
                if not smoke_only:
                    stop = asyncio.Event()
                    loop = asyncio.get_running_loop()
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        loop.add_signal_handler(sig, stop.set)
                    await stop.wait()
    finally:
        if vite and vite.poll() is None:
            vite.terminate()
            await asyncio.to_thread(vite.wait, 10)
        for server, _task in servers:
            server.should_exit = True
        for _server, task in servers:
            await task
        for listener in listeners:
            listener.close()
        for app, names in ((gateway_app, ("mtls_listener", "gateway_service")),
                           (registration_app, ("mtls_listener", "credential_registrar"))):
            for name in names:
                if hasattr(app.state, name):
                    delattr(app.state, name)
        temp.cleanup()
        if resource is not None:
            cleanup_aws(resource, period, created)


def cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-only", action="store_true")
    parser.add_argument("--aws", action="store_true",
                        help="Use existing dev DynamoDB/KMS resources with synthetic auth/provider")
    args = parser.parse_args()
    if args.aws and not args.smoke_only:
        parser.error("--aws requires --smoke-only so all test records can be tracked and removed")
    asyncio.run(main(smoke_only=args.smoke_only, aws=args.aws))


if __name__ == "__main__":
    cli()
