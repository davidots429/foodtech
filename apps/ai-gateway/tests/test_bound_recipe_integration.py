"""Internal Gateway flow using the real source guard and synthetic records."""
import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from foodtech_ai_gateway.ollama import OllamaResult
from foodtech_ai_gateway.policy import Caller, Model, Tier
from foodtech_ai_gateway.recipe_source_guard import BoundRecipeSourceGuard
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import JeyukBaseline

import test_recipe_source_guard as source_fixture
from test_registry import AtomicClient, MemoryResource, TransactionCanceledException


class BoundRecipeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.source = source_fixture.RecipeSourceGuardTests(methodName='runTest')
        self.source.setUp()
        self.execution = self.source.execution
        resource = MemoryResource()
        client = AtomicClient(resource.table)
        self.registry = DynamoRegistry('synthetic', '2026-11', resource=resource,
                                       client=client, local_models={'model'})
        now = datetime.now(timezone.utc)
        self.registry.create_model(Model('model', 'ollama', Tier.TIER_1, 'ACTIVE', True, True, now, now))
        self.registry.create_execution(self.execution)
        self.registry.create_limit('TENANT', 'tenant-a', Decimal('1'), calls_remaining=1)
        self.registry.create_limit('PROVIDER', 'ollama', Decimal('1'))
        self.registry.create_limit('PROJECT', 'GLOBAL', Decimal('1'))
        jobs = (f'SCOPE#USER#tenant-a#JOB#{self.source.current}',
                f'SCOPE#USER#tenant-a#JOB#{self.source.prior}')
        self.guard = BoundRecipeSourceGuard(self.source.guard, self.execution, jobs,
                                           ('JEYUK#batch-current', 'JEYUK#batch-prior'))
        original_transact = client.transact_write_items
        def transact(*, TransactItems, ClientRequestToken):
            source_checks = [item for item in TransactItems if 'ConditionCheck' in item and
                             item['ConditionCheck']['TableName'] != self.registry.table_name]
            if source_checks:
                if len(source_checks) != 6 or not self.guard.verify(self.execution):
                    raise TransactionCanceledException()
                TransactItems = [item for item in TransactItems if item not in source_checks]
            return original_transact(TransactItems=TransactItems, ClientRequestToken=ClientRequestToken)
        client.transact_write_items = transact
        self.service = GatewayService(self.registry, recipe_source_guard=self.guard)

    def invoke(self, model):
        return asyncio.run(self.service.execute_local_recipe(Caller('business-api', 'tenant-a'), 'exec',
            evidence=Evidence('COMPLETE', 'PASSED', True, False),
            baseline=JeyukBaseline('SMALL', Decimal('200'), Decimal('30'), 480, Decimal('1.8'), 60, 60, False),
            context={}, ollama=model))

    def test_unchanged_source_allows_internal_candidate(self):
        class Model:
            async def structured_chat(self, *_):
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        result = self.invoke(Model())
        self.assertEqual(result.state, 'SUCCEEDED')
        self.assertIn('NEEDS_REVIEW', result.response_text)

    def test_source_change_during_model_call_discards_candidate(self):
        source = self.source.resource.tables['foodtech-data-flow-dev']
        class Model:
            async def structured_chat(self, *_):
                source.items[('JEYUK#batch-current', 'RAW#000001')]['raw'] += ' '
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        result = self.invoke(Model())
        self.assertEqual((result.state, result.error_code), ('FAILED', 'RECIPE_SOURCE_CHANGED_OR_UNAVAILABLE'))
        self.assertIsNone(result.response_text)
        self.assertNotIn('temperature_c', str(self.registry.execution_result('exec')))

    def test_execution_change_during_model_call_cannot_store_candidate(self):
        table = self.registry.table
        class Model:
            async def structured_chat(self, *_):
                execution = table.items[('EXEC#exec', 'META')]
                execution['prior_source_revision'] += 1
                execution['version'] += 1
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        result = self.invoke(Model())
        self.assertEqual((result.state, result.error_code), ('FAILED', 'SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED'))
        self.assertIsNone(result.response_text)
        self.assertIsNone(self.registry.execution_result('exec'))
        self.assertEqual(self.registry.execution_state('exec'), 'RESERVED')

    def test_execution_change_immediately_before_settlement_is_rejected(self):
        original_finish = self.registry.finish
        def racing_finish(*args, **kwargs):
            execution = self.registry.table.items[('EXEC#exec', 'META')]
            execution['prior_source_revision'] += 1
            execution['version'] += 1
            return original_finish(*args, **kwargs)
        self.registry.finish = racing_finish
        class Model:
            async def structured_chat(self, *_):
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        result = self.invoke(Model())
        self.assertEqual((result.state, result.error_code), ('FAILED', 'SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED'))
        self.assertIsNone(result.response_text)
        self.assertIsNone(self.registry.execution_result('exec'))

    def test_source_revision_change_immediately_before_settlement_is_rejected(self):
        original_finish = self.registry.finish
        def racing_finish(*args, **kwargs):
            source = self.source.resource.tables['foodtech-data-flow-dev']
            source.items[('JEYUK#batch-prior', 'SOURCE#REVISION')]['revision'] += 1
            return original_finish(*args, **kwargs)
        self.registry.finish = racing_finish
        class Model:
            async def structured_chat(self, *_):
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        result = self.invoke(Model())
        self.assertEqual((result.state, result.error_code), ('FAILED', 'SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED'))
        self.assertIsNone(result.response_text)
        self.assertIsNone(self.registry.execution_result('exec'))
        self.assertEqual(self.registry.execution_state('exec'), 'RESERVED')


if __name__ == '__main__':
    unittest.main()
