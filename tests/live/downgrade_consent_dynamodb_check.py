"""Opt-in consent transaction check using only synthetic registry records."""
import json
import os
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import boto3
import httpx

from foodtech_ai_gateway import downgrade_flow
from foodtech_ai_gateway.downgrade_flow import DowngradeError, DowngradeFlow, NOTICE_VERSION
from foodtech_ai_gateway.policy import Caller, Execution, Feature, GatewayPolicy, Model, Tier, UNCONFIGURED_TIER1
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.service import GatewayService

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
    prior_job_id = str(uuid4())
    comparison = uuid4().hex + uuid4().hex
    period = "2097-11"
    registry = DynamoRegistry(TABLE, period, resource=session.resource("dynamodb"),
                              client=session.client("dynamodb"), local_models={target, source}, auth_table="foodtech-auth-sessions-dev")
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
                                              None, "SYNTHETIC", Decimal("0"), job_id=job_id, job_version=7,
                                              comparison_id=comparison, prior_job_id=prior_job_id, prior_job_version=3,
                                              jeyuk_source_revision=30, prior_source_revision=29))
        mark("EXEC", parent)
        with patch.object(downgrade_flow, "MODEL_ID", target):
            flow = DowngradeFlow(registry)
            quote = flow.preview(tenant, parent)
            assert (quote["job_id"], quote["job_version"], quote["comparison_id"]) == (job_id, 7, comparison)
            assert (quote["prior_job_id"], quote["prior_job_version"]) == (prior_job_id, 3)
            assert (quote["jeyuk_source_revision"], quote["prior_source_revision"]) == (30, 29)
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
            assert (registry.execution(child).prior_job_id, registry.execution(child).prior_job_version) == (prior_job_id, 3)
            assert (registry.execution(child).jeyuk_source_revision, registry.execution(child).prior_source_revision) == (30, 29)
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
                UNCONFIGURED_TIER1, None, "SYNTHETIC", Decimal("0"), job_id=job_id, job_version=7,
                comparison_id=comparison, prior_job_id=prior_job_id, prior_job_version=3))
            mark("EXEC", unconfigured_parent)
            unconfigured_quote = flow.preview(tenant, unconfigured_parent)
            mark("QUOTE", unconfigured_quote["quote_id"])
            mark("CONSENT", unconfigured_quote["quote_id"])
            mark("EXEC", unconfigured_quote["execution_id"])
            unconfigured_child = flow.confirm(tenant, unconfigured_quote["quote_id"], NOTICE_VERSION, True)
            assert (registry.execution(unconfigured_child).job_id,
                    registry.execution(unconfigured_child).comparison_id) == (job_id, comparison)
            assert (registry.execution(unconfigured_child).prior_job_id,
                    registry.execution(unconfigured_child).prior_job_version) == (prior_job_id, 3)
            assert GatewayPolicy(registry).authorize(Caller("business-api", tenant), unconfigured_child).allowed
            table.update_item(Key={"pk": f"MODEL#{source}", "sk": "META"},
                              UpdateExpression="SET #s = :active ADD #v :one",
                              ExpressionAttributeNames={"#s": "state", "#v": "version"},
                              ExpressionAttributeValues={":active": "ACTIVE", ":one": 1})
            app.state.mtls_listener = True
            app.state.gateway_service = GatewayService(registry)
            async def request_configured():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
                    return await client.post("/internal/ai/recipe-requests", json={
                        "tenant_id": tenant, "job_id": job_id, "job_version": 7,
                        "prior_job_id": prior_job_id, "prior_job_version": 3})
            try:
                with patch.dict(os.environ, {"FOODTECH_RECIPE_TIER1_MODEL_ID": source}), \
                        patch("foodtech_ai_gateway.main._trial_limits", return_value=None):
                    response = asyncio.run(request_configured())
                assert response.status_code == 201
                configured_id = response.json()["execution_id"]
                mark("EXEC", configured_id)
                assert response.json()["reason"] == "RECOMMENDATION_EVIDENCE_UNVERIFIED"
                assert response.json()["execution_available"] is False
                configured = registry.execution(configured_id)
                assert (configured.model_id, configured.prior_job_id, configured.prior_job_version) == (source, prior_job_id, 3)
                assert registry.execution_state(configured_id) == "PENDING"
                assert registry.budget(tenant, "ollama").calls_remaining == 3
            finally:
                del app.state.mtls_listener
                del app.state.gateway_service
    finally:
        for key in reversed(keys): table.delete_item(Key=key)
        if any(table.get_item(Key=key, ConsistentRead=True).get("Item") for key in keys):
            raise RuntimeError("Test records remain")
    print(json.dumps({"atomic_consent_and_execution": "PASS", "replay_blocked": True,
                      "model_calls": 0, "cleanup_records": len(keys)}))


if __name__ == "__main__":
    main()
