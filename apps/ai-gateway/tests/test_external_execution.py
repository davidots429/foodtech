import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from foodtech_ai_gateway.policy import Caller, Consent, Credential, Execution, Feature, Model, Tier
from foodtech_ai_gateway.provider import ProviderFailure, ProviderResult
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService
from foodtech_ai_gateway.vault import DynamoCredentialVault, VaultFailure
from test_registry import AtomicClient, MemoryResource


KEY_ARN = "arn:aws:kms:ap-southeast-2:573693340100:key/test-key"


class FakeVault:
    def __init__(self):
        self.reads = 0
        self.secret = bytearray(b"synthetic-test-key")

    def read(self, credential):
        self.reads += 1
        assert credential.vault_ref == "vault:key-1"
        return SimpleNamespace(credential_id="key-1")

    def decrypt(self, record):
        assert record.credential_id == "key-1"
        return self.secret


class FakeProvider:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def generate(self, provider, model_id, prompt, secret):
        self.calls += 1
        assert (provider, model_id, prompt) == ("openai", "gpt-4o-mini", "synthetic prompt")
        assert bytes(secret) == b"synthetic-test-key"
        if self.fail:
            raise ProviderFailure("PROVIDER_RATE_LIMITED")
        return ProviderResult("synthetic provider response", Decimal("0.01"), 4, 5)


class ExternalExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        resource = MemoryResource()
        registry = DynamoRegistry("test-ai", "2026-11", resource=resource,
                                  client=AtomicClient(resource.table))
        now = datetime.now(timezone.utc)
        registry.create_model(Model("gpt-4o-mini", "openai", Tier.TIER_3, "ACTIVE",
                                    False, True, now, now))
        registry.create_credential(Credential("key-1", "tenant-a", "openai", "ACTIVE",
                                               None, "pseudo-a"), vault_ref="vault:key-1")
        registry.create_consent(Consent("consent-1", "run-1", "tenant-a", "gpt-4o-mini",
                                         "EXTERNAL_TRANSFER", "notice-v1", now,
                                         transmission_location="EXTERNAL",
                                         estimated_cost_usd=Decimal("0.05")))
        registry.create_execution(Execution("run-1", "tenant-a", Feature.GENERAL_GUIDE,
                                             "gpt-4o-mini", "key-1", "SYNTHETIC",
                                             Decimal("0.05"), external_transfer_consent_id="consent-1"))
        registry.create_limit("TENANT", "tenant-a", Decimal("1"), calls_remaining=1)
        registry.create_limit("PROVIDER", "openai", Decimal("1"))
        registry.create_limit("PROJECT", "GLOBAL", Decimal("1"))
        self.registry = registry
        self.vault = FakeVault()
        self.provider = FakeProvider()
        self.service = GatewayService(registry, vault=self.vault, provider=self.provider)
        self.caller = Caller("business-api", "tenant-a")

    async def test_external_success_persists_result_and_wipes_key_buffer(self):
        result = await self.service.execute_external(self.caller, "run-1", "synthetic prompt")
        self.assertEqual(result.state, "SUCCEEDED")
        self.assertEqual(self.registry.execution_state("run-1"), "SUCCEEDED")
        self.assertEqual(self.registry.execution_result("run-1")["response_text"],
                         "synthetic provider response")
        self.assertEqual(self.registry.budget("tenant-a", "openai").calls_remaining, 0)
        self.assertEqual(self.registry.budget("tenant-a", "openai").user_remaining_usd,
                         Decimal("0.99"))
        self.assertEqual(bytes(self.vault.secret), b"\x00" * len(self.vault.secret))

    async def test_provider_failure_is_redacted_and_keeps_reservation(self):
        self.service.provider = FakeProvider(fail=True)
        result = await self.service.execute_external(self.caller, "run-1", "synthetic prompt")
        self.assertEqual((result.state, result.error_code), ("FAILED", "PROVIDER_RATE_LIMITED"))
        self.assertEqual(self.registry.execution_result("run-1")["error_code"],
                         "PROVIDER_RATE_LIMITED")
        self.assertEqual(self.registry.budget("tenant-a", "openai").user_remaining_usd,
                         Decimal("0.95"))
        self.assertNotIn("synthetic-test-key", str(self.registry.resource.table.items))
        self.assertEqual(bytes(self.vault.secret), b"\x00" * len(self.vault.secret))

    async def test_wrong_tenant_denied_before_vault_access(self):
        result = await self.service.execute_external(Caller("business-api", "tenant-b"),
                                                     "run-1", "synthetic prompt")
        self.assertEqual(result.state, "DENIED")
        self.assertEqual(self.vault.reads, 0)
        self.assertEqual(self.provider.calls, 0)


class VaultBoundaryTests(unittest.TestCase):
    def test_vault_checks_owner_key_and_context_before_returning_secret(self):
        record = {"pk": "CREDENTIAL#key-1", "sk": "META", "state": "ACTIVE",
                  "credential_id": "key-1", "tenant_pseudonym": "pseudo-a",
                  "provider": "openai", "environment": "dev", "schema_version": "1",
                  "pool_version": "v1", "shard_id": "0", "key_arn": KEY_ARN,
                  "ciphertext": b"ciphertext"}

        class Table:
            def get_item(self, **_kwargs):
                return {"Item": record}

        resource = SimpleNamespace(Table=lambda name: Table())
        sdk = SimpleNamespace(decrypt=lambda **kwargs: (
            b"synthetic-test-key", SimpleNamespace(
                algorithm=__import__("foodtech_ai_gateway.vault", fromlist=["SUITE"]).SUITE,
                encryption_context=kwargs["encryption_context"])))
        keyrings = SimpleNamespace(create_aws_kms_keyring=lambda **_kwargs: object())
        vault = DynamoCredentialVault("vault-test", "dev", {KEY_ARN}, resource=resource,
                                      kms_client=object(), sdk_client=sdk,
                                      material_providers=keyrings)
        credential = Credential("key-1", "tenant-a", "openai", "ACTIVE", None,
                                "pseudo-a", "vault:key-1")
        self.assertEqual(vault.decrypt(vault.read(credential)), bytearray(b"synthetic-test-key"))
        record["tenant_pseudonym"] = "pseudo-b"
        with self.assertRaises(VaultFailure):
            vault.read(credential)
        record["tenant_pseudonym"] = "pseudo-a"
        record["key_arn"] = KEY_ARN + "-other"
        with self.assertRaises(VaultFailure):
            vault.read(credential)


if __name__ == "__main__":
    unittest.main()
