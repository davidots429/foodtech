import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from foodtech_ai_gateway.policy import (
    Budget, Caller, Consent, Credential, Execution, Feature, GatewayPolicy, Model, Tier,
)


NOW = datetime.now(timezone.utc)


class FakeRegistry:
    def __init__(self):
        self.executions = {}
        self.models = {}
        self.credentials = {}
        self.consents = {}
        self.budgets = {}
        self.available = set()

    def execution(self, key):
        return self.executions.get(key)

    def model(self, key):
        return self.models.get(key)

    def credential(self, key):
        return self.credentials.get(key)

    def consent(self, key):
        return self.consents.get(key)

    def budget(self, tenant_id, provider):
        return self.budgets.get((tenant_id, provider))

    def local_available(self, model_id):
        return model_id in self.available


class GatewayPolicyTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        self.registry.executions["run-1"] = Execution(
            "run-1", "tenant-a", Feature.GENERAL_GUIDE, "llama3.1:8b", None,
            "SYNTHETIC", Decimal("0"),
        )
        self.registry.models["llama3.1:8b"] = Model(
            "llama3.1:8b", "ollama", Tier.TIER_3, "ACTIVE", True, True, NOW, NOW,
        )
        self.registry.models["gpt-4o-mini"] = Model(
            "gpt-4o-mini", "openai", Tier.TIER_3, "ACTIVE", False, True, NOW, NOW,
        )
        self.registry.models["tier-1-model"] = Model(
            "tier-1-model", "openai", Tier.TIER_1, "ACTIVE", False, True, NOW, NOW,
        )
        self.registry.credentials["credential-a"] = Credential(
            "credential-a", "tenant-a", "openai", "ACTIVE", NOW + timedelta(days=1),
        )
        self.registry.budgets[("tenant-a", "ollama")] = Budget(
            Decimal("0"), Decimal("0"), Decimal("0"), 10,
        )
        self.registry.budgets[("tenant-a", "openai")] = Budget(
            Decimal("1"), Decimal("1"), Decimal("1"), 10,
        )
        self.registry.available.add("llama3.1:8b")
        self.policy = GatewayPolicy(self.registry)
        self.caller = Caller("business-api", "tenant-a")

    def allow_external_execution(self, model_id: str, cost: str):
        self.registry.executions["run-1"] = replace(
            self.registry.executions["run-1"], model_id=model_id,
            credential_id="credential-a", estimated_cost_usd=Decimal(cost),
            external_transfer_consent_id="transfer-1",
        )
        self.registry.consents["transfer-1"] = Consent(
            "transfer-1", "run-1", "tenant-a", model_id, "EXTERNAL_TRANSFER",
            "notice-v1", NOW, transmission_location="EXTERNAL",
            estimated_cost_usd=Decimal(cost),
        )

    def test_local_default_requires_authoritative_active_model_and_caller(self):
        allowed = self.policy.authorize(self.caller, "run-1")
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.model_id, "llama3.1:8b")
        self.assertEqual(self.policy.authorize(Caller("customer-web", "tenant-a"), "run-1").code,
                         "CALLER_NOT_AUTHORIZED")
        self.assertEqual(self.policy.authorize(Caller("business-api", "tenant-b"), "run-1").code,
                         "EXECUTION_NOT_FOUND")
        self.registry.models["llama3.1:8b"] = replace(self.registry.models["llama3.1:8b"],
                                                       regression_passed=False)
        self.assertEqual(self.policy.authorize(self.caller, "run-1").code, "MODEL_NOT_ACTIVE")

    def test_external_model_requires_own_active_credential_and_transfer_policy(self):
        self.allow_external_execution("gpt-4o-mini", "0.05")
        execution = self.registry.executions["run-1"]
        self.assertTrue(self.policy.authorize(self.caller, "run-1").allowed)
        self.registry.executions["run-1"] = replace(execution, external_transfer_consent_id=None)
        self.assertEqual(self.policy.authorize(self.caller, "run-1").code,
                         "EXTERNAL_TRANSFER_CONSENT_REQUIRED")
        self.registry.executions["run-1"] = execution
        self.registry.credentials["credential-a"] = replace(self.registry.credentials["credential-a"],
                                                             tenant_id="tenant-b")
        self.assertEqual(self.policy.authorize(self.caller, "run-1").code, "CREDENTIAL_NOT_USABLE")
        self.registry.credentials["credential-a"] = replace(self.registry.credentials["credential-a"],
                                                             tenant_id="tenant-a")
        self.registry.executions["run-1"] = replace(execution, data_classification="CUSTOMER")
        self.assertEqual(self.policy.authorize(self.caller, "run-1").code, "EXTERNAL_TRANSFER_BLOCKED")
        self.registry.executions["run-1"] = replace(execution, data_classification="CUSTOMER",
                                                     external_transfer_approved=True,
                                                     external_transfer_consent_id="transfer-1")
        self.assertTrue(self.policy.authorize(self.caller, "run-1").allowed)

    def test_sensitive_task_never_uses_external_model(self):
        self.registry.executions["run-1"] = replace(
            self.registry.executions["run-1"], feature=Feature.SENSITIVE_LOCAL,
            model_id="gpt-4o-mini", credential_id="credential-a",
        )
        self.assertEqual(self.policy.authorize(self.caller, "run-1").code, "EXTERNAL_TRANSFER_BLOCKED")

    def test_tier_one_downgrade_requires_new_execution_specific_consent(self):
        self.registry.executions["run-1"] = replace(
            self.registry.executions["run-1"], feature=Feature.RECIPE_CORRECTION,
            parent_execution_id="run-original",
        )
        refused = self.policy.authorize(self.caller, "run-1")
        self.assertEqual(refused.code, "TIER_NOT_ALLOWED")
        self.assertTrue(refused.requires_new_execution)
        self.registry.consents["consent-1"] = Consent(
            "consent-1", "run-1", "tenant-a", "llama3.1:8b", "TIER_DOWNGRADE",
            "notice-v1", NOW, parent_execution_id="run-original",
            quality_warning_acknowledged=True, transmission_location="LOCAL",
            estimated_cost_usd=Decimal("0"),
        )
        self.registry.executions["run-1"] = replace(self.registry.executions["run-1"],
                                                     downgrade_consent_id="consent-1")
        self.assertTrue(self.policy.authorize(self.caller, "run-1").allowed)
        self.registry.consents["consent-1"] = replace(self.registry.consents["consent-1"],
                                                       parent_execution_id="wrong-run")
        self.assertEqual(self.policy.authorize(self.caller, "run-1").code, "TIER_NOT_ALLOWED")

    def test_custom_model_needs_named_approval_after_regression(self):
        custom = replace(self.registry.models["llama3.1:8b"], custom=True)
        self.registry.models["llama3.1:8b"] = custom
        self.assertEqual(self.policy.authorize(self.caller, "run-1").code, "MODEL_NOT_ACTIVE")
        self.registry.models["llama3.1:8b"] = replace(custom, approved_by="project-admin")
        self.assertTrue(self.policy.authorize(self.caller, "run-1").allowed)

    def test_limit_never_auto_downgrades_tier_one(self):
        self.allow_external_execution("tier-1-model", "2")
        self.registry.executions["run-1"] = replace(
            self.registry.executions["run-1"], feature=Feature.RECIPE_CORRECTION,
        )
        denied = self.policy.authorize(self.caller, "run-1")
        self.assertEqual(denied.code, "LIMIT_ACTION_REQUIRED")
        self.assertTrue(denied.requires_new_execution)
        self.assertFalse(denied.allowed)

    def test_general_limit_falls_back_only_to_available_local_model_with_capacity(self):
        self.allow_external_execution("gpt-4o-mini", "2")
        decision = self.policy.authorize(self.caller, "run-1")
        self.assertEqual(decision.code, "LOCAL_FALLBACK")
        self.assertEqual(decision.model_id, "llama3.1:8b")
        self.assertTrue(decision.fallback_used)
        self.registry.budgets[("tenant-a", "ollama")] = Budget(
            Decimal("0"), Decimal("0"), Decimal("0"), 0,
        )
        self.assertEqual(self.policy.authorize(self.caller, "run-1").code, "LIMIT_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
