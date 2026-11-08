"""Actual Cognito customer session through the external AI flow on dev AWS.

Only a published synthetic key and a fake provider response are used. The
Business API, registration service, Gateway, Cognito and AWS storage are real.
Run interactively; the account owner enters the password in Cognito's browser.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import boto3

import customer_external_browser_harness as base
from foodtech_ai_gateway.policy import Model, Tier
from foodtech_ai_gateway.service import GatewayService
from foodtech_business_api.auth import Settings


def load_business_settings() -> Settings:
    env_path = base.ROOT / "apps" / "business-api" / ".env"
    if not env_path.is_file():
        raise RuntimeError("local Business API .env is required")
    for line in env_path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.startswith("FOODTECH_"):
            os.environ[name] = value.strip().strip('"')
    settings = Settings.from_env()
    if settings.customer_callback != "http://127.0.0.1:8000/api/auth/customer/callback" or \
            settings.customer_web != "http://127.0.0.1:5173" or settings.secure_cookies:
        raise RuntimeError("the existing local Cognito callback and web URLs are required")
    if not os.getenv("FOODTECH_AUTH_TABLE"):
        raise RuntimeError("this test requires the existing shared session table")
    return settings


def only_confirmed_customer(settings: Settings) -> str:
    pool_id = settings.issuer.rsplit("/", 1)[-1]
    cognito = boto3.Session(profile_name="foodtech", region_name=base.REGION).client("cognito-idp")
    users = [user for page in cognito.get_paginator("list_users").paginate(UserPoolId=pool_id)
             for user in page["Users"] if user.get("Enabled") and
             user.get("UserStatus") == "CONFIRMED"]
    if len(users) != 1:
        raise RuntimeError("the test pool must have exactly one confirmed, enabled account")
    return next(attribute["Value"] for attribute in users[0]["Attributes"]
                if attribute["Name"] == "sub")


def fixed_business_listener() -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 8000))
    listener.listen(16)
    return listener


def remove_test_records(resource, period: str, tenant_id: str,
                        credentials: list[str], quotes: list[tuple[str, str]]) -> None:
    registry = resource.Table(base.REGISTRY_TABLE)
    vault = resource.Table(base.VAULT_TABLE)
    for quote_id, execution_id in quotes:
        for kind, identifier, sort in (("EXEC", execution_id, "RESULT"),
                                       ("EXEC", execution_id, "META"),
                                       ("CONSENT", quote_id, "META"),
                                       ("QUOTE", quote_id, "META")):
            registry.delete_item(Key={"pk": f"{kind}#{identifier}", "sk": sort})
    for credential_id in credentials:
        key = {"pk": f"CREDENTIAL#{credential_id}", "sk": "META"}
        vault.delete_item(Key=key)
        registry.delete_item(Key=key)
    for kind, identifier in (("MODEL", base.MODEL),
                             ("LIMIT", f"{period}#TENANT#{tenant_id}"),
                             ("LIMIT", f"{period}#PROVIDER#openai"),
                             ("LIMIT", f"{period}#PROJECT#GLOBAL")):
        registry.delete_item(Key={"pk": f"{kind}#{identifier}", "sk": "META"})


async def run() -> None:
    settings = load_business_settings()
    tenant_id = only_confirmed_customer(settings)
    registry, registrar, vault, resource = base.aws_backends()
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    credentials: list[str] = []
    quotes: list[tuple[str, str]] = []
    original_register = registrar.register
    original_create_quote = registry.create_quote

    def record_registration(*args):
        credential_id = original_register(*args)
        credentials.append(credential_id)
        return credential_id

    def record_quote(quote_id: str, fields: dict):
        original_create_quote(quote_id, fields)
        quotes.append((quote_id, fields["execution_id"]))

    registrar.register = record_registration
    registry.create_quote = record_quote
    temp = tempfile.TemporaryDirectory(prefix="foodtech-cognito-external-")
    servers = []
    listeners = []
    vite = None
    try:
        now = datetime.now(timezone.utc)
        registry.create_model(Model(base.MODEL, "openai", Tier.TIER_3, "ACTIVE", False,
                                    True, now, now,
                                    input_cost_ceiling_usd=Decimal("0.000001"),
                                    output_cost_ceiling_usd=Decimal("0.000001"),
                                    price_version="synthetic-v1"))
        registry.create_limit("TENANT", tenant_id, Decimal("1"), calls_remaining=3)
        registry.create_limit("PROVIDER", "openai", Decimal("1"))
        registry.create_limit("PROJECT", "GLOBAL", Decimal("1"))
        ca, gateway_cert, gateway_key, registration_cert, registration_key, \
            client_cert, client_key = base.certificates(Path(temp.name))
        gateway_listener = base.free_listener()
        registration_listener = base.free_listener()
        business_listener = fixed_business_listener()
        listeners = [gateway_listener, registration_listener, business_listener]
        base.gateway_app.state.mtls_listener = True
        base.gateway_app.state.gateway_service = GatewayService(
            registry, vault=vault, provider=base.SyntheticProvider())
        base.registration_app.state.mtls_listener = True
        base.registration_app.state.credential_registrar = registrar
        config = {"FOODTECH_EXTERNAL_EXECUTION_ENABLED": "true",
                  "FOODTECH_GATEWAY_URL": f"https://127.0.0.1:{gateway_listener.getsockname()[1]}",
                  "FOODTECH_GATEWAY_CA": ca,
                  "FOODTECH_REGISTRATION_URL": f"https://127.0.0.1:{registration_listener.getsockname()[1]}",
                  "FOODTECH_REGISTRATION_CA": ca,
                  "FOODTECH_BUSINESS_CLIENT_CERT": client_cert,
                  "FOODTECH_BUSINESS_CLIENT_KEY": client_key}
        with patch.dict(os.environ, config):
            servers.append(await base.start_server(base.gateway_app, gateway_listener,
                                                   cert=gateway_cert, key=gateway_key, ca=ca))
            servers.append(await base.start_server(base.registration_app, registration_listener,
                                                   cert=registration_cert, key=registration_key, ca=ca))
            servers.append(await base.start_server(base.business_app, business_listener))
            with (Path(temp.name) / "vite.log").open("wb") as log:
                web_env = {name: value for name, value in os.environ.items()
                           if not name.startswith(("FOODTECH_", "AWS_"))}
                web_env["FOODTECH_API_PROXY_TARGET"] = "http://127.0.0.1:8000"
                vite = subprocess.Popen(
                    ["npm", "run", "dev", "--workspace=@foodtech/customer-web", "--",
                     "--host", "127.0.0.1"], cwd=base.ROOT, stdout=log, stderr=log,
                    env=web_env)
                await base.wait_for_web()
                print(json.dumps({"url": "http://127.0.0.1:5173/",
                                  "synthetic_key": base.DUMMY_KEY,
                                  "auth": "COGNITO", "storage": "AWS_DEV",
                                  "provider": "SYNTHETIC"}), flush=True)
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, stop.set)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=900)
                except TimeoutError:
                    pass
    finally:
        if vite and vite.poll() is None:
            vite.terminate()
            try:
                await asyncio.to_thread(vite.wait, 10)
            except subprocess.TimeoutExpired:
                vite.kill()
                await asyncio.to_thread(vite.wait, 10)
        for server, _task in servers:
            server.should_exit = True
        for _server, task in servers:
            await task
        for listener in listeners:
            listener.close()
        for app, names in ((base.gateway_app, ("mtls_listener", "gateway_service")),
                           (base.registration_app, ("mtls_listener", "credential_registrar"))):
            for name in names:
                if hasattr(app.state, name):
                    delattr(app.state, name)
        temp.cleanup()
        remove_test_records(resource, period, tenant_id, credentials, quotes)


if __name__ == "__main__":
    asyncio.run(run())
