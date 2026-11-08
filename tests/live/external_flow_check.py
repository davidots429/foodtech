"""Synthetic preview, consent, reservation and settlement in the dev registry."""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import boto3

from foodtech_ai_gateway.external_flow import ExternalFlow, FIXTURE_PROMPT
from foodtech_ai_gateway.policy import Caller, Credential, Model, Tier
from foodtech_ai_gateway.provider import ProviderResult
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService


REGION = "ap-southeast-2"
ACCOUNT = "573693340100"
TABLE = "foodtech-ai-execution-registry-dev"


class FakeVault:
    def read(self, _credential):
        return object()

    def decrypt(self, _record):
        return bytearray(b"synthetic-only-key")


class FakeProvider:
    def __init__(self, actual_cost: Decimal):
        self.actual_cost = actual_cost

    async def generate(self, provider, model_id, prompt, secret):
        if provider != "openai" or not model_id.startswith("synthetic-model-") or \
                prompt != FIXTURE_PROMPT or bytes(secret) != b"synthetic-only-key":
            raise AssertionError("unexpected provider request")
        return ProviderResult("synthetic response", self.actual_cost, 20, 30)


async def check() -> None:
    if boto3.client("sts", region_name=REGION).get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("unexpected AWS account")
    resource = boto3.resource("dynamodb", region_name=REGION)
    table = resource.Table(TABLE)
    if table.scan(Select="COUNT")["Count"] != 0:
        raise RuntimeError("development registry is not empty; refusing shared-limit test")
    registry = DynamoRegistry(TABLE, datetime.now(timezone.utc).strftime("%Y-%m"),
                              resource=resource,
                              client=boto3.client("dynamodb", region_name=REGION))
    suffix = str(uuid4())
    tenant_id, model_id, credential_id = (f"synthetic-tenant-{suffix}",
                                           f"synthetic-model-{suffix}",
                                           f"synthetic-key-{suffix}")
    created: list[tuple[str, str]] = []
    quote = None
    try:
        now = datetime.now(timezone.utc)
        registry.create_model(Model(
            model_id, "openai", Tier.TIER_3, "ACTIVE", False, True, now, now,
            input_cost_ceiling_usd=Decimal("0.000001"),
            output_cost_ceiling_usd=Decimal("0.000001"), price_version="synthetic-v1"))
        created.append(("MODEL", model_id))
        registry.create_credential(Credential(
            credential_id, tenant_id, "openai", "ACTIVE", None, "synthetic-pseudonym"),
            vault_ref=f"vault:{credential_id}")
        created.append(("CREDENTIAL", credential_id))
        for scope, identifier, calls in (("TENANT", tenant_id, 2),
                                         ("PROVIDER", "openai", None),
                                         ("PROJECT", "GLOBAL", None)):
            limit_id = f"{registry.period}#{scope}#{identifier}"
            registry.create_limit(scope, identifier, Decimal("1"), calls_remaining=calls)
            created.append(("LIMIT", limit_id))
        flow = ExternalFlow(registry)
        quote = flow.preview(tenant_id, model_id, credential_id)
        created.append(("QUOTE", quote["quote_id"]))
        execution_id = flow.confirm(tenant_id, quote["quote_id"], quote["notice_version"])
        created.extend((("CONSENT", quote["quote_id"]), ("EXEC", execution_id)))
        outcome = await GatewayService(registry, vault=FakeVault(),
                                       provider=FakeProvider(Decimal("0.0002"))
                                       ).execute_external(Caller("business-api", tenant_id),
                                                          execution_id, FIXTURE_PROMPT)
        if outcome.state != "SUCCEEDED" or \
                registry.execution_result(execution_id)["actual_cost_usd"] != Decimal("0.0002") or \
                registry.budget(tenant_id, "openai").calls_remaining != 1:
            raise AssertionError("external flow did not settle")
        created.append(("EXEC_RESULT", execution_id))
        overage_quote = flow.preview(tenant_id, model_id, credential_id)
        created.append(("QUOTE", overage_quote["quote_id"]))
        overage_id = flow.confirm(tenant_id, overage_quote["quote_id"],
                                  overage_quote["notice_version"])
        created.extend((("CONSENT", overage_quote["quote_id"]), ("EXEC", overage_id),
                        ("EXEC_RESULT", overage_id)))
        overage_service = GatewayService(
            registry, vault=FakeVault(), provider=FakeProvider(Decimal("2")))
        overage_result = await overage_service.execute_external(
            Caller("business-api", tenant_id), overage_id, FIXTURE_PROMPT)
        if overage_result.state != "SUCCEEDED" or \
                registry.execution_result(overage_id)["settlement_status"] != "REVIEW_REQUIRED" or \
                registry.budget(tenant_id, "openai").project_remaining_usd != 0 or \
                registry.budget(tenant_id, "openai").calls_remaining != 0:
            raise AssertionError("overage did not freeze limits")
        print(json.dumps({"quote_consumed": True, "consent_and_execution_atomic": True,
                          "reservation_and_settlement": True, "overage_freeze": True,
                          "real_provider_called": False}))
    finally:
        if quote:
            created.extend((("QUOTE", quote["quote_id"]),
                            ("CONSENT", quote["quote_id"]),
                            ("EXEC", quote["execution_id"]),
                            ("EXEC_RESULT", quote["execution_id"])))
        for kind, identifier in reversed(list(dict.fromkeys(created))):
            key = {"pk": f"{'EXEC' if kind == 'EXEC_RESULT' else kind}#{identifier}",
                   "sk": "RESULT" if kind == "EXEC_RESULT" else "META"}
            table.delete_item(Key=key)


def main() -> None:
    asyncio.run(check())


if __name__ == "__main__":
    main()
