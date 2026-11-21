"""Opt-in consent transaction check using only synthetic registry records."""
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import boto3

from foodtech_ai_gateway import downgrade_flow
from foodtech_ai_gateway.downgrade_flow import DowngradeError, DowngradeFlow, NOTICE_VERSION
from foodtech_ai_gateway.policy import Caller, Execution, Feature, GatewayPolicy, Model, Tier, UNCONFIGURED_TIER1
from foodtech_ai_gateway.registry import DynamoRegistry

TABLE = "foodtech-ai-execution-registry-dev"


def main():
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use the foodtech development profile")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != "573693340100":
        raise SystemExit("Unexpected AWS account")
    table = session.resource("dynamodb").Table(TABLE)
    suffix = uuid4().hex
    parent, tenant, source, target = str(uuid4()), f"synthetic-{suffix}", f"tier1-{suffix}", f"local-{suffix}"
    job_id = str(uuid4())
    comparison = uuid4().hex + uuid4().hex
    period = "2097-11"
    registry = DynamoRegistry(TABLE, period, resource=session.resource("dynamodb"),
                              client=session.client("dynamodb"), local_models={target})
    keys = []
    def mark(kind, ident): keys.append({"pk": f"{kind}#{ident}", "sk": "META"})
    try:
        now = datetime.now(timezone.utc)
        registry.create_model(Model(source, "ollama", Tier.TIER_1, "INACTIVE", True, True, now, now))
        mark("MODEL", source)
        registry.create_model(Model(target, "ollama", Tier.TIER_3, "ACTIVE", True, True, now, now))
        mark("MODEL", target)
        for scope, ident, calls in (("TENANT", tenant, 3), ("PROVIDER", "ollama", None),
                                     ("PROJECT", "GLOBAL", None)):
            registry.create_limit(scope, ident, Decimal("0"), calls_remaining=calls)
            mark("LIMIT", f"{period}#{scope}#{ident}")
        registry.create_execution(Execution(parent, tenant, Feature.RECIPE_CORRECTION, source,
                                              None, "SYNTHETIC", Decimal("0"), job_id=job_id, job_version=7, comparison_id=comparison))
        mark("EXEC", parent)
        with patch.object(downgrade_flow, "MODEL_ID", target):
            flow = DowngradeFlow(registry)
            quote = flow.preview(tenant, parent)
            assert (quote["job_id"], quote["job_version"], quote["comparison_id"]) == (job_id, 7, comparison)
            mark("QUOTE", quote["quote_id"])
            mark("CONSENT", quote["quote_id"])
            mark("EXEC", quote["execution_id"])
            try:
                flow.confirm(tenant, quote["quote_id"], NOTICE_VERSION, False)
                raise AssertionError("Missing acknowledgement was accepted")
            except DowngradeError:
                pass
            child = flow.confirm(tenant, quote["quote_id"], NOTICE_VERSION, True)
            assert child != parent and child == quote["execution_id"]
            assert (registry.execution(child).job_id, registry.execution(child).job_version,
                    registry.execution(child).comparison_id) == (job_id, 7, comparison)
            assert registry.consent(quote["quote_id"]).parent_execution_id == parent
            assert GatewayPolicy(registry).authorize(Caller("business-api", tenant), child).allowed
            try:
                flow.confirm(tenant, quote["quote_id"], NOTICE_VERSION, True)
                raise AssertionError("Consent was reused")
            except DowngradeError:
                pass
            assert registry.budget(tenant, "ollama").calls_remaining == 3
            unconfigured_parent = str(uuid4())
            registry.create_execution(Execution(unconfigured_parent, tenant, Feature.RECIPE_CORRECTION,
                UNCONFIGURED_TIER1, None, "SYNTHETIC", Decimal("0"), job_id=job_id, job_version=7, comparison_id=comparison))
            mark("EXEC", unconfigured_parent)
            unconfigured_quote = flow.preview(tenant, unconfigured_parent)
            mark("QUOTE", unconfigured_quote["quote_id"])
            mark("CONSENT", unconfigured_quote["quote_id"])
            mark("EXEC", unconfigured_quote["execution_id"])
            unconfigured_child = flow.confirm(tenant, unconfigured_quote["quote_id"], NOTICE_VERSION, True)
            assert (registry.execution(unconfigured_child).job_id,
                    registry.execution(unconfigured_child).comparison_id) == (job_id, comparison)
            assert GatewayPolicy(registry).authorize(Caller("business-api", tenant), unconfigured_child).allowed
    finally:
        for key in reversed(keys): table.delete_item(Key=key)
        if any(table.get_item(Key=key, ConsistentRead=True).get("Item") for key in keys):
            raise RuntimeError("Test records remain")
    print(json.dumps({"atomic_consent_and_execution": "PASS", "replay_blocked": True,
                      "model_calls": 0, "cleanup_records": len(keys)}))


if __name__ == "__main__":
    main()
