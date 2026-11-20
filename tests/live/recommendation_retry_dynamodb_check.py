"""Isolated development-table check; all test records are removed afterward."""
import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import boto3

from foodtech_ai_gateway.ollama import OllamaResult
from foodtech_ai_gateway.policy import Caller, Execution, Feature, Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import TonkatsuBaseline


async def main():
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("development profile required")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != "573693340100":
        raise SystemExit("unexpected AWS account")
    resource = session.resource("dynamodb")
    table = resource.Table("foodtech-ai-execution-registry-dev")
    # A test-only accounting period avoids the current production-like limits.
    period, suffix = "2098-11", uuid4().hex
    tenant, model, execution = f"synthetic-{suffix}", f"synthetic-tier1-{suffix}", str(uuid4())
    registry = DynamoRegistry(table.name, period, resource=resource, client=session.client("dynamodb"),
                              local_models={model})
    keys = []
    def mark(kind, identifier, sk="META"):
        keys.append({"pk": f"{kind}#{identifier}", "sk": sk})
    try:
        now = datetime.now(timezone.utc)
        registry.create_model(Model(model, "ollama", Tier.TIER_1, "ACTIVE", True, True, now, now))
        mark("MODEL", model)
        for scope, ident, calls in (("TENANT", tenant, 3), ("PROVIDER", "ollama", None),
                                     ("PROJECT", "GLOBAL", None)):
            registry.create_limit(scope, ident, Decimal("0"), calls_remaining=calls)
            mark("LIMIT", f"{period}#{scope}#{ident}")
        registry.create_execution(Execution(execution, tenant, Feature.RECIPE_CORRECTION, model,
                                             None, "SYNTHETIC", Decimal("0")))
        mark("EXEC", execution)
        # Keys below can only be created by this unique execution ID.
        for sk in ("CALL#2", "CALL#3", "RESULT"):
            mark("EXEC", execution, sk)
        class FakeOllama:
            calls = 0
            async def structured_chat(self, *_):
                self.calls += 1
                return OllamaResult("bad" if self.calls < 3 else
                    '{"temperature_c":170,"duration_seconds":300}', None, None)
        fake = FakeOllama()
        outcome = await GatewayService(registry).execute_local_recipe(Caller("business-api", tenant), execution,
            evidence=Evidence("COMPLETE", "PASSED", True, False), baseline=TonkatsuBaseline("STANDARD", 300),
            context={"menu": "synthetic"}, ollama=fake)
        assert outcome.state == "SUCCEEDED" and fake.calls == 3
        assert registry.budget(tenant, "ollama").calls_remaining == 0
        assert all(table.get_item(Key=key, ConsistentRead=True).get("Item") for key in keys)
        assert "bad" not in str(registry.execution_result(execution))
        print("Three calls, two retry reservations and validated result: PASS")
    finally:
        for key in reversed(keys):
            table.delete_item(Key=key)
        if any(table.get_item(Key=key, ConsistentRead=True).get("Item") for key in keys):
            raise RuntimeError("test cleanup incomplete")
        print(f"Test records removed: {len(keys)}")


if __name__ == "__main__":
    asyncio.run(main())
