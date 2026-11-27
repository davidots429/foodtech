import unittest
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_ai_gateway.policy import Caller, Consent, Credential, Execution, Feature, Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry, RegistryConflict, _transaction_token
from foodtech_ai_gateway.service import GatewayService
from foodtech_ai_gateway.ollama import OllamaResult
from foodtech_ai_gateway.recipe_source_guard import BoundRecipeSourceGuard
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import JeyukBaseline, TonkatsuBaseline


NOW = datetime.now(timezone.utc)


class ConditionalCheckFailedException(ValueError):
    pass


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
            raise ConditionalCheckFailedException("duplicate record")
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
    exceptions = SimpleNamespace(TransactionCanceledException=TransactionCanceledException,
                                 ConditionalCheckFailedException=ConditionalCheckFailedException)

    def __init__(self, table):
        self.table = table
        self.decoder = TypeDeserializer()
        self.before_commit = None
        self.calls = []
        self.auth_states = {f'USER#{name}': {} for name in
                            ('tenant-a', 'customer-1', 'customer-a', 'new-customer')}

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
        deletes = []
        for action in TransactItems:
            if "Delete" in action:
                deletes.append(self._decode(action["Delete"]["Key"]))
                continue
            if "Put" in action:
                value = self._decode(action["Put"]["Item"])
                if (value["pk"], value["sk"]) in self.table.items:
                    raise TransactionCanceledException()
                puts.append(value)
                continue
            spec = action.get("Update") or action["ConditionCheck"]
            key = self._decode(spec["Key"])
            if spec.get("TableName") == "auth-test":
                state = self.auth_states.get(key["pk"], {})
                if 'attribute_exists(pk)' in spec['ConditionExpression']:
                    epoch = state.get('epoch')
                    if key['pk'] not in self.auth_states or isinstance(epoch, bool) or \
                            not isinstance(epoch, (int, Decimal)) or not 0 <= epoch < 2**63:
                        raise TransactionCanceledException()
                if state.get("suspended") or "deletion_lock_request" in state:
                    raise TransactionCanceledException()
                continue
            if spec.get("ConditionExpression") == "attribute_not_exists(pk)":
                if (key["pk"], key["sk"]) in self.table.items:
                    raise TransactionCanceledException()
                continue
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
                    if ":retry" in values and (item.get("retry_count", 0) != values[":retry"] - 1 or
                            item.get("reserved_provider") != values[":provider"] or
                            item.get("feature") != values[":feature"]):
                        raise TransactionCanceledException()
                elif key["pk"].startswith("QUOTE#"):
                    if item["state"] != values[":open"] or item["tenant_id"] != values[":tenant"] or \
                            item["expires"] <= values[":now"]:
                        raise TransactionCanceledException()
                elif ":cost" in values:
                    if item["remaining_usd"] < values[":cost"] or (
                        "TENANT#" in key["pk"] and item["calls_remaining"] < 1
                    ):
                        raise TransactionCanceledException()
                elif "calls_remaining = calls_remaining - :one" in spec["UpdateExpression"] and item["calls_remaining"] < 1:
                    raise TransactionCanceledException()
                elif ":delta" in values and item["remaining_usd"] < values[":delta"]:
                    raise TransactionCanceledException()
                updates.append((key, values, spec["UpdateExpression"]))
        # Validate every condition before mutating any item.
        for key, values, expression in updates:
            item = self.table.items[(key["pk"], key["sk"])]
            item["version"] += 1
            if key["pk"].startswith("EXEC#"):
                item["state"] = values.get(":final", "RESERVED")
                if ":retry" in values:
                    item["retry_count"] = values[":retry"]
                elif ":provider" in values:
                    item["reserved_provider"] = values[":provider"]
                else:
                    item["actual_cost_usd"] = values[":actual"]
                    item["settlement_status"] = values[":settlement"]
            elif key["pk"].startswith("QUOTE#"):
                item["state"] = values[":used"]
            else:
                if ":cost" in values:
                    item["remaining_usd"] -= values[":cost"]
                elif ":zero" in values:
                    item["remaining_usd"] = values[":zero"]
                    if "TENANT#" in key["pk"]:
                        item["calls_remaining"] = 0
                elif ":delta" in values:
                    item["remaining_usd"] -= values[":delta"]
                elif ":refund" in values:
                    item["remaining_usd"] += values[":refund"]
                if "TENANT#" in key["pk"] and ":cost" in values:
                    item["calls_remaining"] -= 1
                elif "calls_remaining = calls_remaining - :one" in expression:
                    item["calls_remaining"] -= 1
        for value in puts:
            self.table.items[(value["pk"], value["sk"])] = value
        for value in deletes:
            self.table.items.pop((value["pk"], value["sk"]), None)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.resource = MemoryResource()
        self.client = AtomicClient(self.resource.table)
        self.registry = DynamoRegistry("registry-test", "2026-11", resource=self.resource,
                                       client=self.client, auth_table="auth-test", local_models={"local-model"})
        self.registry.create_model(Model("local-model", "ollama", Tier.TIER_3, "ACTIVE", True,
                                         True, NOW, NOW))
        self.registry.create_execution(Execution("run-1", "tenant-a", Feature.GENERAL_GUIDE,
                                                  "local-model", None, "SYNTHETIC", Decimal("0.2")))
        self.registry.create_limit("TENANT", "tenant-a", Decimal("1"), calls_remaining=1)
        self.registry.create_limit("PROVIDER", "ollama", Decimal("1"))
        self.registry.create_limit("PROJECT", "GLOBAL", Decimal("1"))
        self.service = GatewayService(self.registry)
        self.client.calls.clear()

    def test_active_principal_check_never_returns_authentication_record(self):
        for initialized in (False, True):
            self.registry.require_principal = initialized
            guard = self.registry._active_tenant_guard('tenant-a')['ConditionCheck']
            self.assertEqual(guard['ReturnValuesOnConditionCheckFailure'], 'NONE')
            self.assertEqual(guard['Key'], {'pk': {'S': 'USER#tenant-a'}})

    def test_execution_snapshot_contains_consistent_state_and_version(self):
        snapshot = self.registry.execution_snapshot('run-1')
        self.assertEqual(snapshot.execution.execution_id, 'run-1')
        self.assertEqual((snapshot.state, snapshot.version), ('PENDING', 1))
        self.assertIsNone(self.registry.execution_snapshot('unknown'))
        self.resource.table.items[('EXEC#run-1', 'META')]['state'] = 'FAILED'
        self.resource.table.items[('EXEC#run-1', 'META')]['version'] = 2
        snapshot = self.registry.execution_snapshot('run-1')
        self.assertEqual((snapshot.state, snapshot.version), ('FAILED', 2))

    def test_status_snapshot_rejects_foreign_and_concurrent_changes(self):
        snapshot, result = self.registry.execution_status_snapshot('run-1', 'tenant-a')
        self.assertEqual(snapshot.state, 'PENDING')
        self.assertIsNone(result)
        original = self.registry.execution_result
        def forbidden(_):
            raise AssertionError('foreign tenant must not read result')
        self.registry.execution_result = forbidden
        self.assertIsNone(self.registry.execution_status_snapshot('run-1', 'other'))
        def race(execution_id):
            self.resource.table.items[('EXEC#run-1', 'META')]['version'] += 1
            return original(execution_id)
        self.registry.execution_result = race
        with self.assertRaises(RegistryConflict):
            self.registry.execution_status_snapshot('run-1', 'tenant-a')
        self.registry.execution_result = lambda _: {'state': 'SUCCEEDED', 'response_text': 'unexpected'}
        with self.assertRaises(RegistryConflict):
            self.registry.execution_status_snapshot('run-1', 'tenant-a')

    def test_execution_binding_versions_reject_corrupt_values(self):
        item = self.resource.table.items[('EXEC#run-1', 'META')]
        for name in ('job_version', 'prior_job_version', 'jeyuk_source_revision', 'prior_source_revision'):
            for value in (Decimal('1.5'), Decimal('NaN'), '1', True, 0, -1):
                with self.subTest(field=name, value=value):
                    item[name] = value
                    with self.assertRaises(ValueError):
                        self.registry.execution_snapshot('run-1')
            item[name] = Decimal('2')
            self.assertEqual(getattr(self.registry.execution_snapshot('run-1').execution, name), 2)
            item[name] = None

    def test_terminal_status_requires_matching_result(self):
        item = self.resource.table.items[('EXEC#run-1', 'META')]
        for state in ('SUCCEEDED', 'FAILED'):
            with self.subTest(state=state):
                item['state'] = state
                self.registry.execution_result = lambda _: None
                with self.assertRaises(RegistryConflict):
                    self.registry.execution_status_snapshot('run-1', 'tenant-a')
                self.registry.execution_result = lambda _, state=state: {'state': state}
                snapshot, result = self.registry.execution_status_snapshot('run-1', 'tenant-a')
                self.assertEqual(snapshot.state, state)
                self.assertEqual(result['state'], state)

    def test_settlement_rejects_corrupt_execution_without_storing_response(self):
        execution = self.registry.execution('run-1')
        self.assertTrue(self.service.prepare(Caller('business-api', 'tenant-a'), 'run-1').allowed)
        item = self.resource.table.items[('EXEC#run-1', 'META')]
        item['feature'] = 'INVALID'
        item['version'] += 1
        with self.assertRaises(RegistryConflict):
            self.registry.finish('run-1', succeeded=True, actual_cost_usd=Decimal('0'),
                                 response_text='synthetic', expected_execution=execution)
        self.assertIsNone(self.registry.execution_result('run-1'))

    def test_settlement_rejects_untrusted_guard_before_writing(self):
        execution = self.registry.execution('run-1')
        before = len(self.client.calls)
        with self.assertRaises(ValueError):
            self.registry.finish('run-1', succeeded=True, actual_cost_usd=Decimal('0'),
                                 response_text='synthetic', expected_execution=execution,
                                 recipe_source_guard=SimpleNamespace())
        self.assertEqual(len(self.client.calls), before)

    def recipe_execution(self):
        self.registry.create_model(Model("synthetic-tier1", "ollama", Tier.TIER_1, "ACTIVE", True,
                                         True, NOW, NOW))
        self.registry.local_models.add("synthetic-tier1")
        self.registry.create_execution(Execution("recipe-1", "tenant-a", Feature.RECIPE_CORRECTION,
                                                  "synthetic-tier1", None, "SYNTHETIC", Decimal("0")))
        self.resource.table.items[("LIMIT#2026-11#TENANT#tenant-a", "META")]["calls_remaining"] = 3
        self.assertTrue(self.service.prepare(Caller("business-api", "tenant-a"), "recipe-1").allowed)

    def test_transaction_token_stable_for_same_content_and_changes_with_parameters(self):
        original = [{"Update": {"Key": {"pk": {"S": "tenant"}}, "Version": {"N": "1"}}}]
        changed = [{"Update": {"Key": {"pk": {"S": "tenant"}}, "Version": {"N": "2"}}}]
        for operation in ("confirm", "reserve", "finish"):
            token = _transaction_token("registry-test", operation, "id", original)
            self.assertEqual(token, _transaction_token("registry-test", operation, "id", original))
            self.assertNotEqual(token, _transaction_token("registry-test", operation, "id", changed))

    def test_local_recipe_retries_debit_each_call_once_in_order(self):
        self.recipe_execution()
        caller = Caller("business-api", "tenant-a")
        self.assertFalse(self.service.prepare_local_recipe_retry(caller, "recipe-1", 3).allowed)
        self.assertTrue(self.service.prepare_local_recipe_retry(caller, "recipe-1", 2).allowed)
        self.assertFalse(self.service.prepare_local_recipe_retry(caller, "recipe-1", 2).allowed)
        self.assertTrue(self.service.prepare_local_recipe_retry(caller, "recipe-1", 3).allowed)
        self.assertEqual(self.registry.budget("tenant-a", "ollama").calls_remaining, 0)
        self.assertEqual(self.resource.table.items[("EXEC#recipe-1", "CALL#2")]["state"], "RESERVED")
        self.assertEqual(self.resource.table.items[("EXEC#recipe-1", "CALL#3")]["attempt"], 3)
        shape = get_session().get_service_model("dynamodb").operation_model("TransactWriteItems").input_shape
        validate_parameters({"TransactItems": self.client.calls[-1],
                             "ClientRequestToken": "11111111-1111-1111-1111-111111111111"}, shape)

    def test_retry_requires_active_authorization_and_atomic_limit(self):
        self.recipe_execution()
        caller = Caller("business-api", "tenant-a")
        self.assertFalse(self.service.prepare_local_recipe_retry(Caller("business-api", "tenant-b"),
                                                                "recipe-1", 2).allowed)
        model = self.resource.table.items[("MODEL#synthetic-tier1", "META")]
        model["state"] = "INACTIVE"
        model["version"] += 1
        self.assertFalse(self.service.prepare_local_recipe_retry(caller, "recipe-1", 2).allowed)
        model["state"] = "ACTIVE"
        model["version"] += 1
        tenant = self.resource.table.items[("LIMIT#2026-11#TENANT#tenant-a", "META")]
        self.client.before_commit = lambda: tenant.update({"calls_remaining": 0, "version": tenant["version"] + 1})
        self.assertFalse(self.service.prepare_local_recipe_retry(caller, "recipe-1", 2).allowed)
        self.assertNotIn(("EXEC#recipe-1", "CALL#2"), self.resource.table.items)

    def test_local_recipe_executes_retries_and_stores_only_validated_result(self):
        self.recipe_execution()
        # Use a fresh pending execution; recipe_execution already reserved recipe-1.
        self.registry.create_execution(Execution("recipe-2", "tenant-a", Feature.RECIPE_CORRECTION,
                                                  "synthetic-tier1", None, "SYNTHETIC", Decimal("0")))
        self.resource.table.items[("LIMIT#2026-11#TENANT#tenant-a", "META")]["calls_remaining"] = 3
        class FakeOllama:
            calls = 0
            async def structured_chat(self, model, messages, schema):
                self.calls += 1
                text = "invalid secret" if self.calls < 3 else '{"temperature_c":170,"duration_seconds":300}'
                return OllamaResult(text, 1, 1)
        model = FakeOllama()
        outcome = asyncio.run(self.service.execute_local_recipe(Caller("business-api", "tenant-a"), "recipe-2",
            evidence=Evidence("COMPLETE", "PASSED", True, False), baseline=TonkatsuBaseline("STANDARD", 300),
            context={"menu": "synthetic"}, ollama=model))
        self.assertEqual((outcome.state, model.calls), ("SUCCEEDED", 3))
        self.assertIn('"status":"NEEDS_REVIEW"', outcome.response_text)
        self.assertNotIn("invalid secret", str(self.resource.table.items))
        self.assertEqual(self.registry.budget("tenant-a", "ollama").calls_remaining, 0)

    def test_jeyuk_source_guard_is_required_and_discards_changed_source_result(self):
        self.recipe_execution()
        self.registry.create_execution(Execution("recipe-jeyuk", "tenant-a", Feature.RECIPE_CORRECTION,
                                                  "synthetic-tier1", None, "SYNTHETIC", Decimal("0")))
        class FakeOllama:
            calls = 0
            async def structured_chat(self, *_args):
                self.calls += 1
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        model = FakeOllama()
        args = dict(evidence=Evidence("COMPLETE", "PASSED", True, False),
                    baseline=JeyukBaseline("SMALL", Decimal("200"), Decimal("30"), 480, Decimal("1.8"), 60, 60, False),
                    context={}, ollama=model)
        caller = Caller("business-api", "tenant-a")
        unavailable = asyncio.run(self.service.execute_local_recipe(caller, "recipe-jeyuk", **args))
        self.assertEqual((unavailable.state, unavailable.error_code, model.calls), ("DENIED", "SOURCE_GUARD_UNAVAILABLE", 0))
        self.service.recipe_source_guard = SimpleNamespace(verify=lambda _execution: False)
        stale = asyncio.run(self.service.execute_local_recipe(caller, "recipe-jeyuk", **args))
        self.assertEqual((stale.state, stale.error_code, model.calls), ("DENIED", "SOURCE_SCOPE_UNAVAILABLE", 0))
        other_execution = Execution("other", "tenant-a", Feature.RECIPE_CORRECTION,
                                    "synthetic-tier1", None, "SYNTHETIC", Decimal("0"))
        def unexpected(*_args):
            raise AssertionError('different execution must not access source')
        self.service.recipe_source_guard = BoundRecipeSourceGuard(SimpleNamespace(verify_scoped=unexpected),
                                                                  other_execution, ('a', 'b'), ('c', 'd'))
        wrong_scope = asyncio.run(self.service.execute_local_recipe(caller, "recipe-jeyuk", **args))
        self.assertEqual((wrong_scope.state, wrong_scope.error_code, model.calls),
                         ("DENIED", "SOURCE_SCOPE_UNAVAILABLE", 0))

    def test_recipe_tier_and_missing_evidence_do_not_invoke_model(self):
        self.registry.create_execution(Execution("recipe-3", "tenant-a", Feature.RECIPE_CORRECTION,
                                                  "local-model", None, "SYNTHETIC", Decimal("0")))
        class NeverOllama:
            async def structured_chat(self, *args):
                raise AssertionError("model must not be called")
        args = dict(baseline=TonkatsuBaseline("STANDARD", 300), context={}, ollama=NeverOllama())
        caller = Caller("business-api", "tenant-a")
        denied = asyncio.run(self.service.execute_local_recipe(caller, "recipe-3",
            evidence=Evidence("COMPLETE", "PASSED", True, False), **args))
        self.assertEqual((denied.state, denied.error_code), ("DENIED", "TIER_NOT_ALLOWED"))
        missing = asyncio.run(self.service.execute_local_recipe(caller, "recipe-3",
            evidence=Evidence("UNKNOWN", "NOT_MEASURED", None, None), **args))
        self.assertEqual(missing.state, "INDETERMINATE")
        self.assertEqual(self.registry.execution_state("recipe-3"), "PENDING")

    def test_bound_source_guard_with_untrusted_inner_guard_cannot_invoke_model(self):
        self.recipe_execution()
        execution = Execution('recipe-bound', 'tenant-a', Feature.RECIPE_CORRECTION,
                              'synthetic-tier1', None, 'SYNTHETIC', Decimal('0'))
        self.registry.create_execution(execution)
        checks = []
        class ScopedGuard:
            def verify_scoped(self, value, jobs, sources):
                checks.append((value, jobs, sources))
                return True
        jobs, sources = ('job-current', 'job-prior'), ('source-current', 'source-prior')
        self.service.recipe_source_guard = BoundRecipeSourceGuard(ScopedGuard(), execution, jobs, sources)
        class FakeOllama:
            calls = 0
            async def structured_chat(self, *_args):
                self.calls += 1
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        model = FakeOllama()
        result = asyncio.run(self.service.execute_local_recipe(Caller('business-api', 'tenant-a'), 'recipe-bound',
            evidence=Evidence('COMPLETE', 'PASSED', True, False),
            baseline=JeyukBaseline('SMALL', Decimal('200'), Decimal('30'), 480, Decimal('1.8'), 60, 60, False),
            context={}, ollama=model))
        self.assertEqual((result.state, result.error_code, model.calls), ('DENIED', 'SOURCE_SCOPE_UNAVAILABLE', 0))
        self.assertEqual(checks, [])

    def test_general_execution_paths_cannot_bypass_recipe_output_guard(self):
        self.registry.create_execution(Execution("recipe-guard", "tenant-a", Feature.RECIPE_CORRECTION,
                                                  "local-model", None, "SYNTHETIC", Decimal("0")))
        caller = Caller("business-api", "tenant-a")
        result = asyncio.run(self.service.execute_local(caller, "recipe-guard", "prompt", None))
        self.assertEqual((result.state, result.error_code), ("DENIED", "STRUCTURED_RECIPE_REQUIRED"))
        external = GatewayService(self.registry, vault=object(), provider=object())
        result = asyncio.run(external.execute_external(caller, "recipe-guard", "prompt"))
        self.assertEqual((result.state, result.error_code), ("DENIED", "STRUCTURED_RECIPE_REQUIRED"))
        self.assertEqual(self.registry.execution_state("recipe-guard"), "PENDING")

    def test_retry_limit_and_safety_rejection_never_return_raw_output(self):
        self.registry.create_model(Model("synthetic-tier1", "ollama", Tier.TIER_1, "ACTIVE", True,
                                         True, NOW, NOW))
        self.registry.local_models.add("synthetic-tier1")
        class FakeOllama:
            def __init__(self, output): self.output, self.calls = output, 0
            async def structured_chat(self, *args):
                self.calls += 1
                return OllamaResult(self.output, 1, 1)
        caller = Caller("business-api", "tenant-a")
        args = dict(evidence=Evidence("COMPLETE", "PASSED", True, False),
                    baseline=TonkatsuBaseline("STANDARD", 300), context={})
        for execution_id, raw, expected in (("limited", "secret-invalid", "LIMIT_ACTION_REQUIRED"),
                ("unsafe", '{"temperature_c":300,"duration_seconds":300}', "LIMIT_CHECK_FAILED")):
            self.registry.create_execution(Execution(execution_id, "tenant-a", Feature.RECIPE_CORRECTION,
                                                       "synthetic-tier1", None, "SYNTHETIC", Decimal("0")))
            tenant = self.resource.table.items[("LIMIT#2026-11#TENANT#tenant-a", "META")]
            tenant["calls_remaining"] = 1
            model = FakeOllama(raw)
            outcome = asyncio.run(self.service.execute_local_recipe(caller, execution_id, ollama=model, **args))
            self.assertEqual((outcome.state, outcome.error_code, model.calls), ("FAILED", expected, 1))
            self.assertIsNone(outcome.response_text)
            self.assertNotIn(raw, str(self.registry.execution_result(execution_id)))

    def test_trial_limit_concurrent_creation_never_resets_an_existing_limit(self):
        def create(_):
            self.registry.ensure_trial_tenant_limit("new-customer", Decimal("0"), 2)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(create, range(40)))
        key = ("LIMIT#2026-11#TENANT#new-customer", "META")
        item = self.resource.table.items[key]
        self.assertEqual(item["calls_remaining"], 2)
        item["calls_remaining"] = 0
        item["version"] = 9
        create(0)
        self.assertEqual(item["calls_remaining"], 0)
        self.assertEqual(item["version"], 9)
        self.registry.period = "2026-12"
        create(0)
        self.assertEqual(self.resource.table.items[("LIMIT#2026-12#TENANT#new-customer", "META")]["calls_remaining"], 2)
        self.assertEqual(item["calls_remaining"], 0)
        with self.assertRaises(ValueError):
            self.registry.ensure_trial_tenant_limit("x", Decimal("NaN"), 1)

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

    def test_cost_above_reservation_is_recorded_and_freezes_all_limits(self):
        self.assertTrue(self.service.prepare(Caller("business-api", "tenant-a"), "run-1").allowed)
        self.registry.finish("run-1", succeeded=True, actual_cost_usd=Decimal("2"),
                             response_text="requires reconciliation")
        self.assertEqual(self.registry.execution_state("run-1"), "SUCCEEDED")
        self.assertEqual(self.registry.execution_result("run-1")["unreconciled_usd"],
                         Decimal("1.8"))
        self.assertEqual(self.registry.execution_result("run-1")["settlement_status"],
                         "REVIEW_REQUIRED")
        self.assertEqual(self.registry.budget("tenant-a", "ollama").calls_remaining, 0)
        self.assertEqual(self.registry.budget("tenant-a", "ollama").project_remaining_usd,
                         Decimal("0"))

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

    def test_reservation_token_tracks_transaction_parameters(self):
        tokens = []
        def reject(*, TransactItems, ClientRequestToken):
            tokens.append(ClientRequestToken)
            raise TransactionCanceledException()
        self.client.transact_write_items = reject
        for _ in range(2):
            self.registry.begin_request()
            execution = self.registry.execution("run-1")
            self.registry.budget("tenant-a", "ollama")
            with self.assertRaises(RegistryConflict):
                self.registry.reserve(execution, "ollama")
        self.assertEqual(tokens[0], tokens[1])
        self.resource.table.items[("LIMIT#2026-11#PROJECT#GLOBAL", "META")]["version"] += 1
        self.registry.begin_request()
        execution = self.registry.execution("run-1")
        self.registry.budget("tenant-a", "ollama")
        with self.assertRaises(RegistryConflict):
            self.registry.reserve(execution, "ollama")
        self.assertNotEqual(tokens[0], tokens[2])

    def test_credential_registry_accepts_only_vault_reference(self):
        credential = Credential("key-1", "tenant-a", "openai", "ACTIVE", None)
        with self.assertRaises(ValueError):
            self.registry.create_credential(credential, vault_ref="plaintext-key")
        self.registry.create_credential(credential, vault_ref="vault:tenant-a/key-1")
        stored = self.registry.credential("key-1")
        self.assertEqual(stored.vault_ref, "vault:tenant-a/key-1")
        self.assertEqual(stored.tenant_id, credential.tenant_id)
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
        self.assertEqual(len(checked), 4)

    def test_reservation_rejects_lock_acquired_during_authorization(self):
        self.client.before_commit = lambda: setattr(self.client, 'auth_states', {
            'USER#tenant-a': {'deletion_lock_request': 'synthetic'}})
        decision = self.service.prepare(Caller('business-api', 'tenant-a'), 'run-1')
        self.assertFalse(decision.allowed)
        self.assertEqual(self.registry.execution_state('run-1'), 'PENDING')
        self.assertEqual(self.registry.budget('tenant-a', 'ollama').calls_remaining, 1)

    def test_missing_principal_state_allows_normal_new_work_and_reservation(self):
        self.client.auth_states = {}
        self.registry.create_execution(Execution('new-run', 'tenant-a', Feature.GENERAL_GUIDE,
            'local-model', None, 'SYNTHETIC', Decimal('0')))
        decision = self.service.prepare(Caller('business-api', 'tenant-a'), 'run-1')
        self.assertTrue(decision.allowed)
        self.assertEqual(self.registry.execution_state('run-1'), 'RESERVED')
        self.assertIn(('ACTIVE#tenant-a', 'EXEC#run-1'), self.resource.table.items)

    def test_initialized_mode_blocks_absent_and_invalid_principals_without_creating_records(self):
        self.registry.require_principal = True
        for state in (None, {}, {'epoch': -1}, {'epoch': True}, {'epoch': '0'}, {'epoch': 2**63}):
            self.client.auth_states = {} if state is None else {'USER#tenant-a': state}
            with self.subTest(state=state), self.assertRaises(RegistryConflict):
                self.registry.create_execution(Execution('new-run', 'tenant-a', Feature.GENERAL_GUIDE,
                    'local-model', None, 'SYNTHETIC', Decimal('0')))
            self.assertNotIn(('EXEC#new-run', 'META'), self.resource.table.items)

    def test_initialized_reservation_rechecks_principal_after_authorization(self):
        self.registry.require_principal = True
        self.client.auth_states = {'USER#tenant-a': {'epoch': 0}}
        self.client.before_commit = lambda: self.client.auth_states.clear()
        self.assertFalse(self.service.prepare(Caller('business-api', 'tenant-a'), 'run-1').allowed)
        self.assertEqual(self.registry.execution_state('run-1'), 'PENDING')
        self.assertEqual(self.registry.budget('tenant-a', 'ollama').calls_remaining, 1)
        self.assertNotIn(('ACTIVE#tenant-a', 'EXEC#run-1'), self.resource.table.items)

    def test_initialized_mode_allows_valid_principal_and_retains_terminal_settlement(self):
        self.registry.require_principal = True
        self.client.auth_states = {'USER#tenant-a': {'epoch': 0}}
        self.assertTrue(self.service.prepare(Caller('business-api', 'tenant-a'), 'run-1').allowed)
        self.client.auth_states.clear()
        self.registry.finish('run-1', succeeded=False, actual_cost_usd=Decimal('0'), error_code='SYNTHETIC_FAILURE')
        self.assertEqual(self.registry.execution_state('run-1'), 'FAILED')
        self.assertNotIn(('ACTIVE#tenant-a', 'EXEC#run-1'), self.resource.table.items)

    def test_locked_tenant_cannot_create_execution_quote_or_new_limit(self):
        self.client.auth_states = {'USER#tenant-a': {'deletion_lock_request': 'synthetic'},
                                   'USER#other-locked': {'deletion_lock_request': 'synthetic'}}
        execution = Execution('new-run', 'tenant-a', Feature.GENERAL_GUIDE, 'local-model', None,
                              'SYNTHETIC', Decimal('0'))
        with self.assertRaises(RegistryConflict):
            self.registry.create_execution(execution)
        with self.assertRaises(RegistryConflict):
            self.registry.create_quote('new-quote', {'tenant_id': 'tenant-a'})
        with self.assertRaises(RegistryConflict):
            self.registry.ensure_trial_tenant_limit('other-locked', Decimal('0'), 1)
        self.assertNotIn(('EXEC#new-run', 'META'), self.resource.table.items)
        self.assertNotIn(('QUOTE#new-quote', 'META'), self.resource.table.items)

    def test_local_retry_rejects_deletion_lock(self):
        self.recipe_execution()
        self.client.auth_states = {'USER#tenant-a': {'suspended': True}}
        decision = self.service.prepare_local_recipe_retry(Caller('business-api', 'tenant-a'), 'recipe-1', 2)
        self.assertFalse(decision.allowed)
        self.assertNotIn(('EXEC#recipe-1', 'CALL#2'), self.resource.table.items)

    def test_active_marker_survives_lock_until_settlement(self):
        self.assertTrue(self.service.prepare(Caller('business-api', 'tenant-a'), 'run-1').allowed)
        marker = ('ACTIVE#tenant-a', 'EXEC#run-1')
        self.assertIn(marker, self.resource.table.items)
        self.client.auth_states = {'USER#tenant-a': {'deletion_lock_request': 'synthetic'}}
        self.registry.finish('run-1', succeeded=True, actual_cost_usd=Decimal('0.1'), response_text='synthetic')
        self.assertNotIn(marker, self.resource.table.items)
        self.assertEqual(self.registry.execution_state('run-1'), 'SUCCEEDED')

    def test_active_marker_remains_if_settlement_fails(self):
        self.assertTrue(self.service.prepare(Caller('business-api', 'tenant-a'), 'run-1').allowed)
        marker = ('ACTIVE#tenant-a', 'EXEC#run-1')
        self.client.before_commit = lambda: self.resource.table.items[
            ('LIMIT#2026-11#TENANT#tenant-a', 'META')].update(version=999)
        with self.assertRaises(RegistryConflict):
            self.registry.finish('run-1', succeeded=True, actual_cost_usd=Decimal('0.1'), response_text='synthetic')
        self.assertIn(marker, self.resource.table.items)


    def test_combi_generation_cannot_activate_without_authenticated_source_handoff(self):
        import copy
        from unittest.mock import AsyncMock
        from test_combi_recommendation import baseline
        self.recipe_execution()
        self.registry.create_execution(Execution("recipe-combi", "tenant-a", Feature.RECIPE_CORRECTION,
                                                  "synthetic-tier1", None, "SYNTHETIC", Decimal("0")))
        before = copy.deepcopy(self.resource.table.items)
        model = AsyncMock()
        for menu in ('RICE', 'MACKEREL'):
            result = asyncio.run(self.service.execute_local_recipe(Caller('business-api','tenant-a'), 'recipe-combi',
                evidence=Evidence('COMPLETE','PASSED',True,False), baseline=baseline(menu), context={}, ollama=model))
            self.assertEqual((result.state,result.error_code),('DENIED','COMBI_SOURCE_SCOPE_UNAVAILABLE'))
            self.assertEqual(self.resource.table.items,before)
        model.structured_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
