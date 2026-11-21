"""Verify synthetic external quote confirmation after a version conflict."""
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import boto3

from foodtech_ai_gateway.external_flow import ExternalFlow, ExternalFlowError
from foodtech_ai_gateway.policy import Credential, Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry

ACCOUNT, REGION, TABLE = "573693340100", "ap-southeast-2", "foodtech-ai-execution-registry-dev"


def main():
    if os.environ.get("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use the foodtech development profile")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    resource = session.resource("dynamodb")
    table = resource.Table(TABLE)
    period = f"{secrets.randbelow(4000) + 5000:04d}-{secrets.randbelow(12) + 1:02d}"
    suffix = uuid4().hex
    tenant, model, credential = f"synthetic-{suffix}", f"synthetic-model-{suffix}", f"synthetic-key-{suffix}"
    registry = DynamoRegistry(TABLE, period, resource=resource, client=session.client("dynamodb"))
    keys = []
    def key(kind, identifier): return {"pk": f"{kind}#{identifier}", "sk": "META"}
    try:
        now = datetime.now(timezone.utc)
        registry.create_model(Model(model, "openai", Tier.TIER_3, "ACTIVE", False, True, now, now,
            input_cost_ceiling_usd=Decimal("0.000001"), output_cost_ceiling_usd=Decimal("0.000001"),
            price_version="synthetic"))
        keys.append(key("MODEL", model))
        registry.create_credential(Credential(credential, tenant, "openai", "ACTIVE", None),
                                   vault_ref=f"vault:{credential}")
        keys.append(key("CREDENTIAL", credential))
        for scope, identifier, calls in (("TENANT", tenant, 1), ("PROVIDER", "openai", None),
                                         ("PROJECT", "GLOBAL", None)):
            registry.create_limit(scope, identifier, Decimal("1"), calls_remaining=calls)
            keys.append(key("LIMIT", f"{period}#{scope}#{identifier}"))
        flow = ExternalFlow(registry)
        quote = flow.preview(tenant, model, credential)
        keys.extend((key("QUOTE", quote["quote_id"]), key("CONSENT", quote["quote_id"]),
                     key("EXEC", quote["execution_id"])))
        original = registry.client
        class ConflictOnce:
            def __getattr__(self, name): return getattr(original, name)
            def transact_write_items(self, **kwargs):
                table.update_item(Key=key("MODEL", model), UpdateExpression="ADD #v :one",
                                  ExpressionAttributeNames={"#v": "version"},
                                  ExpressionAttributeValues={":one": 1})
                return original.transact_write_items(**kwargs)
        registry.client = ConflictOnce()
        try:
            flow.confirm(tenant, quote["quote_id"], quote["notice_version"])
        except ExternalFlowError as error:
            if error.code != "QUOTE_CONFLICT": raise
        else:
            raise AssertionError("Expected quote conflict")
        registry.client = original
        if flow.confirm(tenant, quote["quote_id"], quote["notice_version"]) != quote["execution_id"]:
            raise AssertionError("Quote retry failed")
        if not all(table.get_item(Key=k, ConsistentRead=True).get("Item") for k in keys):
            raise AssertionError("Quote, consent or execution missing")
        print("Quote confirmation after known conflict verified")
    finally:
        for k in reversed(keys): table.delete_item(Key=k)
        if any(table.get_item(Key=k, ConsistentRead=True).get("Item") for k in keys):
            raise RuntimeError("Synthetic quote records remain")
        print("Synthetic quote records removed")


if __name__ == "__main__": main()
