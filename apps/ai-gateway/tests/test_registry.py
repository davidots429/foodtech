import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_ai_gateway.policy import Caller, Consent, Credential, Execution, Feature, Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService


NOW = datetime.now(timezone.utc)


class TransactionCanceledException(Exception):
    pass


class MemoryTable:
    def __init__(self):
        self.items = {}

    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead
        value = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(value)} if value else {}

    def put_item(self, *, Item, ConditionExpression):
        key = (Item["pk"], Item["sk"])
        if key in self.items:
            raise ValueError("duplicate record")
        self.items[key] = dict(Item)

    def update_item(self, **kwargs):
        item = self.items[(kwargs["Key"]["pk"], "META")]
        if item["state"] != "RESERVED":
            raise ValueError("invalid state transition")
        item["state"] = kwargs["ExpressionAttributeValues"][":final"]
        item["version"] += 1


class MemoryResource:
    def __init__(self):
        self.table = MemoryTable()

    def Table(self, name):
        return self.table


class AtomicClient:
    exceptions = SimpleNamespace(TransactionCanceledException=TransactionCanceledException)

    def __init__(self, table):
        self.table = table
        self.decoder = TypeDeserializer()
        self.before_commit = None
        self.calls = []

    def _decode(self, values):
        return {name: self.decoder.deserialize(value) for name, value in values.items()}

    def transact_write_items(self, *, TransactItems, ClientRequestToken):
        assert len(ClientRequestToken) == 36
        if self.before_commit:
            self.before_commit()
            self.before_commit = None
        self.calls.append(TransactItems)
        updates = []
        puts = []
        for action in TransactItems:
            if "Put" in action:
                value = self._decode(action["Put"]["Item"])
                if (value["pk"], value["sk"]) in self.table.items:
                    raise TransactionCanceledException()
                puts.append(value)
                continue
            spec = action.get("Update") or action["ConditionCheck"]
            key = self._decode(spec["Key"])
            values = self._decode(spec["ExpressionAttributeValues"])
            item = self.table.items.get((key["pk"], key["sk"]))
            if not item or item["version"] != values[":v"]:
                raise TransactionCanceledException()
            if "Update" in action:
                if key["pk"].startswith("EXEC#"):
                    if item["state"] != values.get(":pending", values.get(":reserved")) or (
                        ":tenant" in values and item["tenant_id"] != values[":tenant"]
                    ):
                        raise TransactionCanceledException()
                elif ":cost" in values:
                    if item["remaining_usd"] < values[":cost"] or (
                        "TENANT#" in key["pk"] and item["calls_remaining"] < 1
                    ):
                        raise TransactionCanceledException()
                elif ":delta" in values and item["remaining_usd"] < values[":delta"]:
                    raise TransactionCanceledException()
                updates.append((key, values))
        # Validate every condition before mutating any item.
        for key, values in updates:
            item = self.table.items[(key["pk"], key["sk"])]
            item["version"] += 1
            if key["pk"].startswith("EXEC#"):
                item["state"] = values.get(":final", "RESERVED")
                if ":provider" in values:
                    item["reserved_provider"] = values[":provider"]
                else:
                    item["actual_cost_usd"] = values[":actual"]
            else:
                if ":cost" in values:
                    item["remaining_usd"] -= values[":cost"]
                elif ":delta" in values:
                    item["remaining_usd"] -= values[":delta"]
                else:
                    item["remaining_usd"] += values[":refund"]
                if "TENANT#" in key["pk"] and ":cost" in values:
                    item["calls_remaining"] -= 1
        for value in puts:
            self.table.items[(value["pk"], value["sk"])] = value


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.resource = MemoryResource()
        self.client = AtomicClient(self.resource.table)
        self.registry = DynamoRegistry("registry-test", "2026-11", resource=self.resource,
                                       client=self.client, local_models={"local-model"})
        self.registry.create_model(Model("local-model", "ollama", Tier.TIER_3, "ACTIVE", True,
                                         True, NOW, NOW))
        self.registry.create_execution(Execution("run-1", "tenant-a", Feature.GENERAL_GUIDE,
                                                  "local-model", None, "SYNTHETIC", Decimal("0.2")))
        self.registry.create_limit("TENANT", "tenant-a", Decimal("1"), calls_remaining=1)
        self.registry.create_limit("PROVIDER", "ollama", Decimal("1"))
        self.registry.create_limit("PROJECT", "GLOBAL", Decimal("1"))
        self.service = GatewayService(self.registry)

    def test_execution_id_reads_durable_records_and_claims_limits_once(self):
        self.assertEqual(self.registry.execution("run-1").tenant_id, "tenant-a")
        decision = self.service.prepare(Caller("business-api", "tenant-a"), "run-1")
        self.assertTrue(decision.allowed)
        self.assertEqual(self.registry.execution_state("run-1"), "RESERVED")
        self.assertEqual(self.registry.budget("tenant-a", "ollama").calls_remaining, 0)
        self.assertEqual(self.registry.budget("tenant-a", "ollama").project_remaining_usd,
                         Decimal("0.8"))
        self.assertEqual(self.service.prepare(Caller("business-api", "tenant-a"), "run-1").code,
                         "LIMIT_EXCEEDED")
        self.registry.finish("run-1", succeeded=True, actual_cost_usd=Decimal("0.1"),
                             response_text="합성 조리 안내")
        self.assertEqual(self.registry.execution_state("run-1"), "SUCCEEDED")
        self.assertEqual(self.registry.execution_result("run-1")["response_text"], "합성 조리 안내")
        self.assertEqual(self.registry.budget("tenant-a", "ollama").project_remaining_usd,
                         Decimal("0.9"))

    def test_transaction_request_matches_dynamodb_api_shape(self):
        self.assertTrue(self.service.prepare(Caller("business-api", "tenant-a"), "run-1").allowed)
        shape = get_session().get_service_model("dynamodb").operation_model(
            "TransactWriteItems").input_shape
        validate_parameters({"TransactItems": self.client.calls[-1],
                             "ClientRequestToken": "11111111-1111-1111-1111-111111111111"}, shape)

    def test_settlement_exceeding_remaining_budget_is_atomic(self):
        self.assertTrue(self.service.prepare(Caller("business-api", "tenant-a"), "run-1").allowed)
        with self.assertRaises(Exception):
            self.registry.finish("run-1", succeeded=True, actual_cost_usd=Decimal("2"),
                                 response_text="cannot be committed")
        self.assertEqual(self.registry.execution_state("run-1"), "RESERVED")
        self.assertIsNone(self.registry.execution_result("run-1"))
        self.assertEqual(self.registry.budget("tenant-a", "ollama").project_remaining_usd,
                         Decimal("0.8"))

    def test_tenant_mismatch_cannot_consume_budget(self):
        self.assertEqual(self.service.prepare(Caller("business-api", "tenant-b"), "run-1").code,
                         "EXECUTION_NOT_FOUND")
        self.assertFalse(self.client.calls)

    def test_authorized_execution_change_before_claim_is_rejected(self):
        original = self.registry.execution
        reads = 0

        def racing_read(execution_id):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.resource.table.items[("EXEC#run-1", "META")]["version"] += 1
            return original(execution_id)

        self.registry.execution = racing_read
        decision = self.service.prepare(Caller("business-api", "tenant-a"), "run-1")
        self.assertEqual(decision.code, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        self.assertFalse(self.client.calls)

    def test_concurrent_limit_change_cancels_entire_reservation(self):
        key = ("LIMIT#2026-11#PROJECT#GLOBAL", "META")
        self.client.before_commit = lambda: self.resource.table.items[key].update(
            {"remaining_usd": Decimal("0"), "version": 2})
        decision = self.service.prepare(Caller("business-api", "tenant-a"), "run-1")
        self.assertEqual(decision.code, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        self.assertEqual(self.registry.execution_state("run-1"), "PENDING")
        self.assertEqual(self.registry.budget("tenant-a", "ollama").calls_remaining, 1)

    def test_credential_registry_accepts_only_vault_reference(self):
        credential = Credential("key-1", "tenant-a", "openai", "ACTIVE", None)
        with self.assertRaises(ValueError):
            self.registry.create_credential(credential, vault_ref="plaintext-key")
        self.registry.create_credential(credential, vault_ref="vault:tenant-a/key-1")
        self.assertEqual(self.registry.credential("key-1"), credential)
        self.assertNotIn("plaintext", str(self.resource.table.items))

    def test_external_execution_reads_model_credential_and_consent_records(self):
        self.registry.create_model(Model("external-model", "openai", Tier.TIER_3,
                                         "ACTIVE", False, True, NOW, NOW))
        self.registry.create_credential(Credential("credential-a", "tenant-a", "openai",
                                                    "ACTIVE", None), vault_ref="vault:tenant-a/a")
        self.registry.create_consent(Consent("consent-a", "run-2", "tenant-a", "external-model",
                                              "EXTERNAL_TRANSFER", "notice-v1", NOW,
                                              transmission_location="EXTERNAL",
                                              estimated_cost_usd=Decimal("0.3")))
        self.registry.create_execution(Execution("run-2", "tenant-a", Feature.GENERAL_GUIDE,
                                                 "external-model", "credential-a", "SYNTHETIC",
                                                 Decimal("0.3"),
                                                 external_transfer_consent_id="consent-a"))
        self.registry.create_limit("PROVIDER", "openai", Decimal("1"))
        decision = self.service.prepare(Caller("business-api", "tenant-a"), "run-2")
        self.assertTrue(decision.allowed)
        self.assertEqual(self.registry.execution_state("run-2"), "RESERVED")
        checked = [item["ConditionCheck"] for item in self.client.calls[-1]
                   if "ConditionCheck" in item]
        self.assertEqual(len(checked), 3)


if __name__ == "__main__":
    unittest.main()
