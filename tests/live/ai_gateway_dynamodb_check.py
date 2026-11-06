"""Opt-in live check: Business API -> mTLS Gateway -> Ollama -> DynamoDB.

Run with AWS_PROFILE=foodtech after the development registry stack is deployed.
Only synthetic records are created. This script starts and stops its own local
Ollama and Gateway listeners, then deletes exactly the records it created.
"""

import asyncio
import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import boto3
import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from foodtech_ai_gateway import main as gateway_main
from foodtech_ai_gateway.main import app as gateway_app
from foodtech_ai_gateway.policy import Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_business_api import ai as business_ai
from foodtech_business_api.main import app as business_app


ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
TABLE = "foodtech-ai-execution-registry-dev"
MODEL = "llama3.1:8b"
ROOT = Path(__file__).resolve().parents[2]


def certificates(directory: Path) -> tuple[str, str, str, str, str]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Foodtech live check CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))

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
        cert = builder.sign(ca_key, hashes.SHA256())
        cert_path = directory / f"{name}.pem"
        key_path = directory / f"{name}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        key_path.chmod(0o600)
        return str(cert_path), str(key_path)

    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    server_cert, server_key = issue("gateway", ExtendedKeyUsageOID.SERVER_AUTH, server=True)
    client_cert, client_key = issue("business", ExtendedKeyUsageOID.CLIENT_AUTH, server=False)
    return str(ca_path), server_cert, server_key, client_cert, client_key


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def wait_for_ollama(url: str) -> None:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for _ in range(100):
            try:
                response = await client.get(f"{url}/api/tags")
                response.raise_for_status()
                if any(model.get("name") == MODEL for model in response.json().get("models", [])):
                    return
                raise AssertionError(f"{MODEL} is not installed in the selected Ollama model directory")
            except (httpx.ConnectError, httpx.ReadTimeout):
                await asyncio.sleep(0.1)
    raise TimeoutError("local Ollama did not become ready")


def item_key(kind: str, identifier: str, sk: str = "META") -> dict[str, str]:
    return {"pk": f"{kind}#{identifier}", "sk": sk}


async def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for this development-only check")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(TABLE)
    if table.table_status != "ACTIVE":
        raise SystemExit("AI execution registry table is not ACTIVE")

    run_id = uuid4().hex
    expected_ids = [str(uuid4()), str(uuid4())]
    tenant = f"synthetic-ai-validation-{run_id}"
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    registry = DynamoRegistry(TABLE, period, resource=dynamodb,
                              client=session.client("dynamodb"), local_models={MODEL})
    keys: list[dict[str, str]] = []
    for execution_id in expected_ids:
        keys.extend((item_key("EXEC", execution_id),
                     item_key("EXEC", execution_id, "RESULT")))
    temp = tempfile.TemporaryDirectory(prefix="foodtech-ai-live-")
    ollama: subprocess.Popen | None = None
    gateway_task: asyncio.Task | None = None
    gateway_server: uvicorn.Server | None = None
    gateway_listener: socket.socket | None = None
    try:
        def created(kind: str, identifier: str) -> None:
            keys.append(item_key(kind, identifier))

        now = datetime.now(timezone.utc)
        registry.create_model(Model(MODEL, "ollama", Tier.TIER_3, "ACTIVE",
                                    True, True, now, now))
        created("MODEL", MODEL)
        for scope, identifier, calls in (("TENANT", tenant, 2),
                                         ("PROVIDER", "ollama", None),
                                         ("PROJECT", "GLOBAL", None)):
            registry.create_limit(scope, identifier, Decimal("0"), calls_remaining=calls)
            created("LIMIT", f"{period}#{scope}#{identifier}")

        ca, server_cert, server_key, client_cert, client_key = certificates(Path(temp.name))
        ollama_bin = os.getenv("FOODTECH_OLLAMA_BIN") or shutil.which("ollama")
        models = Path(os.getenv("FOODTECH_OLLAMA_MODELS") or
                      ROOT / "models" / "ollama-models")
        if not ollama_bin or not models.is_dir():
            raise SystemExit("Ollama binary and the local model directory are required")
        ollama_url = f"http://127.0.0.1:{free_port()}"
        with (Path(temp.name) / "ollama.log").open("wb") as log:
            ollama = subprocess.Popen([ollama_bin, "serve"], stdout=log, stderr=log,
                                      env={**os.environ, "OLLAMA_HOST": ollama_url,
                                           "OLLAMA_MODELS": str(models), "OLLAMA_NO_CLOUD": "1"})
            await wait_for_ollama(ollama_url)

            gateway_listener = socket.socket()
            gateway_listener.bind(("127.0.0.1", 0))
            gateway_listener.listen(16)
            port = gateway_listener.getsockname()[1]
            gateway_app.state.mtls_listener = True
            gateway_server = uvicorn.Server(uvicorn.Config(
                gateway_app, host="127.0.0.1", port=port,
                ssl_certfile=server_cert, ssl_keyfile=server_key,
                ssl_ca_certs=ca, ssl_cert_reqs=ssl.CERT_REQUIRED,
                proxy_headers=False, access_log=False, log_level="error", lifespan="off"))
            gateway_task = asyncio.create_task(gateway_server.serve(sockets=[gateway_listener]))
            for _ in range(100):
                if gateway_server.started:
                    break
                await asyncio.sleep(0.01)
            if not gateway_server.started:
                raise TimeoutError("mTLS Gateway did not become ready")

            async def synthetic_session(_request, surface):
                assert surface == "customer"
                return SimpleNamespace(subject=tenant)

            config = {
                "AWS_DEFAULT_REGION": REGION,
                "FOODTECH_AI_REGISTRY_TABLE": TABLE,
                "FOODTECH_OLLAMA_URL": ollama_url,
                "FOODTECH_GATEWAY_URL": f"https://127.0.0.1:{port}",
                "FOODTECH_GATEWAY_CA": ca,
                "FOODTECH_BUSINESS_CLIENT_CERT": client_cert,
                "FOODTECH_BUSINESS_CLIENT_KEY": client_key,
            }
            with patch.dict(os.environ, config), \
                 patch.object(gateway_main, "uuid4", side_effect=expected_ids), \
                 patch.object(business_ai, "_session", side_effect=synthetic_session), \
                 patch.object(business_ai, "_csrf", return_value=None):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=business_app),
                                             base_url="http://business.local") as client:
                    success = await client.post("/api/customer/ai/guidance",
                                                json={"prompt": "합성 데이터 시험입니다. 한 문장으로 답하세요."})
                    assert success.status_code == 200, success.text
                    success_body = success.json()
                    success_id = success_body["execution_id"]
                    assert success_id == expected_ids[0]
                    assert success_body["state"] == "SUCCEEDED" and success_body["response_text"]
                    assert registry.execution_state(success_id) == "SUCCEEDED"
                    assert registry.execution_result(success_id)["response_text"] == success_body["response_text"]
                    assert registry.budget(tenant, "ollama").calls_remaining == 1

                    ollama.terminate()
                    await asyncio.to_thread(ollama.wait, 10)
                    failure = await client.post("/api/customer/ai/guidance",
                                                json={"prompt": "합성 장애 시험입니다."})
                    assert failure.status_code == 200, failure.text
                    failure_body = failure.json()
                    failure_id = failure_body["execution_id"]
                    assert failure_id == expected_ids[1]
                    assert failure_body["state"] == "FAILED"
                    assert failure_body["error_code"] == "OLLAMA_UNAVAILABLE"
                    assert registry.execution_state(failure_id) == "FAILED"
                    assert registry.execution_result(failure_id)["error_code"] == "OLLAMA_UNAVAILABLE"
                    assert registry.budget(tenant, "ollama").calls_remaining == 0

            print(json.dumps({"account": ACCOUNT, "region": REGION, "table": TABLE,
                              "success_state": "SUCCEEDED", "response_bytes": len(
                                  success_body["response_text"].encode("utf-8")),
                              "failure_state": "FAILED", "failure_code": "OLLAMA_UNAVAILABLE",
                              "remaining_calls": 0, "mtls": "real loopback TLS"},
                             ensure_ascii=False))
    finally:
        if gateway_server:
            gateway_server.should_exit = True
        try:
            if gateway_task:
                await gateway_task
        finally:
            if gateway_listener:
                gateway_listener.close()
            if hasattr(gateway_app.state, "mtls_listener"):
                del gateway_app.state.mtls_listener
            if ollama and ollama.poll() is None:
                ollama.terminate()
                await asyncio.to_thread(ollama.wait, 10)
            try:
                for key in reversed(keys):
                    table.delete_item(Key=key)
            finally:
                temp.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
