"""No AWS calls: SDK validation and simulated atomic publication races."""
from dataclasses import replace
import asyncio
from datetime import datetime, timedelta
import json
import unittest
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from foodtech_ai_gateway.ollama import OllamaResult
from foodtech_ai_gateway.policy import Caller
from foodtech_ai_gateway.recipe_read_authorization import DynamoRecipeReadAuthorization
from foodtech_ai_gateway.service import GatewayService
from foodtech_ai_gateway.prior_history_conditions import DynamoPriorHistoryConditions
from foodtech_contracts.prior_history_witness import PriorHistoryWitness
from foodtech_contracts.source_read_authorization import SourceReadAuthorization
from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker

import test_bound_recipe_integration as integration_fixture
from test_registry import TransactionCanceledException as SimulatedTransactionCancelled


def observation():
    return SourceReadAuthorization('foodtech-auth-sessions-dev', 'foodtech-source-access-dev',
        'tenant-a', 'exec', 0, ('JEYUK#batch-current', 'JEYUK#batch-prior'),
        (str(uuid4()), str(uuid4())), (1, 2), (2000, 2100))


class GrantCheckTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.Session(region_name='ap-southeast-2', aws_access_key_id='synthetic',
            aws_secret_access_key='synthetic').client('dynamodb')
        self.receipt = observation()
        self.guard = DynamoRecipeReadAuthorization(self.receipt, client=self.client, clock=lambda: 1000)
        self.execution = type('ExecutionReference', (), {'execution_id': 'exec', 'tenant_id': 'tenant-a'})()

    def test_only_condition_checks_bind_account_refs_versions_and_expiry_without_failure_payloads(self):
        checks = self.guard.settlement_checks(self.execution, self.receipt.source_partitions)
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(set(check) == {'ConditionCheck'} for check in checks))
        self.assertTrue(all(check['ConditionCheck']['ReturnValuesOnConditionCheckFailure'] == 'NONE' for check in checks))
        account, first, second = [check['ConditionCheck'] for check in checks]
        self.assertIn('attribute_exists(pk)', account['ConditionExpression'])
        self.assertIn('attribute_not_exists(deletion_lock_request)', account['ConditionExpression'])
        self.assertEqual(account['ExpressionAttributeNames']['#first'], self.receipt.source_partitions[0])
        self.assertEqual(first['ExpressionAttributeValues'][':version'], {'N': '1'})
        self.assertEqual(second['ExpressionAttributeValues'][':version'], {'N': '2'})
        self.assertIn('expires_at > :now', first['ConditionExpression'])
        self.assertNotIn('tenant-a', repr(self.receipt))
        self.assertNotIn('tenant-a', repr(self.guard))
        with Stubber(self.client) as stub:
            stub.add_response('transact_write_items', {})
            self.assertTrue(self.guard.verify(self.execution, self.receipt.source_partitions))
            stub.assert_no_pending_responses()

    def test_scope_mismatch_expiry_and_bad_clock_never_make_a_request(self):
        with Stubber(self.client):
            self.assertFalse(self.guard.verify(self.execution, tuple(reversed(self.receipt.source_partitions))))
            for now in [2000, float('nan'), float('inf'), True, -1]:
                self.guard.clock = lambda now=now: now
                self.assertFalse(self.guard.verify(self.execution, self.receipt.source_partitions))

    def test_revoke_denied_or_unavailable_is_never_authorization(self):
        for code in ['TransactionCanceledException', 'AccessDeniedException', 'InternalServerError']:
            with Stubber(self.client) as stub:
                stub.add_client_error('transact_write_items', code, 'synthetic', http_status_code=400)
                self.assertFalse(self.guard.verify(self.execution, self.receipt.source_partitions))
                stub.assert_no_pending_responses()

    def test_invalid_observation_shape_cannot_build_condition_checks(self):
        for changes in [{'grant_ids': (self.receipt.grant_ids[0],) * 2}, {'auth_epoch': True},
                        {'source_partitions': ([], [])}, {'grant_versions': (True, 2)},
                        {'catalog_table': 'foodtech-source-access-other'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.receipt, **changes)


class GrantTransactionCancelled(ClientError, SimulatedTransactionCancelled):
    pass


class GrantPublicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.flow = integration_fixture.BoundRecipeIntegrationTests(methodName='runTest')
        self.flow.setUp()
        self.receipt = observation()
        self.active = True
        self.revoke_at_publish = False
        self.grant_checks_at_publish = 0
        self.history_active = True
        self.history_change_at_publish = False
        self.history_checks_at_publish = 0
        old_transact = self.flow.client.transact_write_items
        decoder = TypeDeserializer()

        def transact(*, TransactItems, ClientRequestToken):
            is_publication = any('Put' in item and item['Put'].get('Item', {}).get('sk') == {'S': 'RESULT'}
                                 and item['Put'].get('Item', {}).get('state') == {'S': 'SUCCEEDED'} for item in TransactItems)
            if is_publication and self.revoke_at_publish:
                self.active = False
            if is_publication and self.history_change_at_publish:
                self.history_active = False
            remaining, grant_checks, history_checks = [], [], []
            for item in TransactItems:
                check = item.get('ConditionCheck', {})
                key = check.get('Key', {})
                if key.get('pk') in ({'S': 'CONTROL#COOKING_HISTORY_WRITERS'}, {'S': 'SCOPE#USER#tenant-a#JOBS'}):
                    history_checks.append(item)
                elif check.get('TableName') == self.receipt.catalog_table or '#grants' in check.get('ExpressionAttributeNames', {}):
                    grant_checks.append(item)
                elif check.get('TableName') == self.receipt.auth_table:
                    # Adapt only the old fake principal table name; production
                    # requests are checked to use the same auth table above.
                    remaining.append({'ConditionCheck': {**check, 'TableName': 'auth-test'}})
                else:
                    remaining.append(item)
            if history_checks:
                self.assertEqual(len(history_checks), 2)
                if is_publication:
                    self.history_checks_at_publish = len(history_checks)
                if not self.history_active:
                    raise GrantTransactionCancelled({'Error': {'Code': 'TransactionCanceledException',
                        'Message': 'synthetic stale history'}}, 'TransactWriteItems')
            if grant_checks:
                self.assertEqual(len(grant_checks), 3)
                if is_publication:
                    self.grant_checks_at_publish = len(grant_checks)
                if not self.active:
                    raise GrantTransactionCancelled({'Error': {'Code': 'TransactionCanceledException',
                        'Message': 'synthetic revoked'}}, 'TransactWriteItems')
                for item in grant_checks[1:]:
                    values = {k: decoder.deserialize(v) for k, v in item['ConditionCheck']['ExpressionAttributeValues'].items()}
                    self.assertEqual(values[':read'], 'RECIPE_SOURCE_READ')
                    self.assertGreater(values[':expires'], values[':now'])
            if remaining:
                return old_transact(TransactItems=remaining, ClientRequestToken=ClientRequestToken)
            return {}

        self.flow.client.transact_write_items = transact
        self.flow.registry.auth_table = self.receipt.auth_table
        authorization = DynamoRecipeReadAuthorization(self.receipt, client=self.flow.client, clock=lambda: 1000)
        execution = self.flow.execution
        self.history_witness = PriorHistoryWitness('tenant-a', execution.job_id, execution.prior_job_id,
            (execution.job_version, execution.prior_job_version), self.receipt.auth_epoch, 10, 1, 1,
            str(uuid4()), 'a' * 64, 'b' * 64, 10, 'a' * 64, 'c' * 64)
        history = DynamoPriorHistoryConditions(self.history_witness, auth_table=self.receipt.auth_table,
            cooking_table='foodtech-cooking-jobs-dev', account_id='123456789012', client=self.flow.client, clock=lambda: 1000)
        self.flow.guard = replace(self.flow.guard, read_authorization=authorization, history_authorization=history)
        self.flow.service = GatewayService(self.flow.registry, recipe_source_guard=self.flow.guard)

    def invoke(self, on_model=None):
        calls = []
        class Model:
            async def structured_chat(self, *_):
                calls.append(True)
                if on_model:
                    on_model()
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,'
                    '"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        result = self.flow.invoke(Model())
        return result, calls

    def test_active_grants_are_checked_in_same_success_transaction(self):
        result, calls = self.invoke()
        self.assertEqual(result.state, 'SUCCEEDED')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.grant_checks_at_publish, 3)
        self.assertEqual(self.history_checks_at_publish, 2)

    def prepare_batch_observation(self):
        cooking = self.flow.source.resource.tables['foodtech-cooking-jobs-dev']
        for (pk, sk), row in cooking.items.items():
            if sk == 'META':
                row.update(jeyuk_size_profile='SMALL', equipment_profile='jeyuk-small',
                           actual_load_kg='1.8', safety_status='PASSED')

    def test_derived_failed_batch_inputs_reach_model_and_atomic_guarded_settlement(self):
        self.prepare_batch_observation()
        cooking = self.flow.source.resource.tables['foodtech-cooking-jobs-dev'].items
        data = self.flow.source.resource.tables['foodtech-data-flow-dev'].items
        for job in (row for (_, sk), row in cooking.items() if sk == 'META'):
            placed = datetime.fromisoformat(job['actual_placed_at'])
            iso = lambda seconds: (placed + timedelta(seconds=seconds)).isoformat()
            job.update(safety_status='FAILED', jeyuk_addition_group_id='group', jeyuk_additions_simultaneous=True)
            job['jeyuk_feeds'] = {name: {'ingredient': name, 'provenance': 'SYNTHETIC', 'started_at': iso(start),
                'completed_at': iso(end), 'addition_group_id': 'group', 'mass_kg': mass,
                'initial_temperature_c': '4', 'temperature_observed_at': iso(-3)} for name, start, end, mass in
                [('MEAT', -2, 0, '1.0'), ('VEGETABLE', 1, 1.5, '0.5'), ('SAUCE', 2, 2.5, '0.3')]}
            pk = 'JEYUK#' + job['source_binding']['batch_id']
            tracker = JeyukCompletionTracker(0)
            for second in range(1, int(job['actual_cooking_seconds']) + 1):
                row = data[(pk, f'RAW#{second:06d}')]
                sample = json.loads(row['raw'])
                sample.update(heatingSetpointCelsius=200, mixtureCelsius=75, calibrationStatus='OK',
                    safetySensorPosition='LOWEST_MIXTURE_POINT', lastIngredientAdditionCompleted=second == 3)
                row['raw'] = json.dumps(sample)
                tracker.record(row['raw'])
            receipt = tracker.finish(cursor=int(job['actual_cooking_seconds']), complete=True, explicit=True)
            receipt.update(pk=pk, sk='COLLECTION#RECEIPT#1')
            data[(pk, receipt['sk'])] = receipt
        calls = []
        class Model:
            async def structured_chat(self, _model, messages, _schema):
                calls.append(messages)
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,'
                    '"vegetable_interval_seconds":1,"sauce_interval_seconds":2}', 1, 1)
        result = asyncio.run(self.flow.service.execute_observed_local_recipe(Caller('business-api', 'tenant-a'),
            'exec', ollama=Model()))
        self.assertEqual(result.state, 'SUCCEEDED')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.flow.registry.execution_state('exec'), 'SUCCEEDED')
        self.assertEqual(self.grant_checks_at_publish, 3)
        self.assertEqual(self.history_checks_at_publish, 2)
        self.assertEqual(self.flow.registry.budget('tenant-a', 'ollama').calls_remaining, 0)
        self.assertNotIn(self.flow.execution.prior_job_id, json.dumps(calls))

    def test_authorized_snapshot_exposes_facts_without_fabricating_input_readiness(self):
        self.prepare_batch_observation()
        current, prior = self.flow.guard.observe_inputs()
        self.assertEqual((current.cooking_seconds, prior.cooking_seconds), (30, 29))
        self.assertIsNone(current.heating_setpoint_c)
        self.assertIn('MIXTURE_SAFETY_SAMPLES_MISSING', current.unresolved)
        self.assertNotIn('completeness', current.model_context())

    def test_revoke_during_observation_discards_facts(self):
        self.prepare_batch_observation()
        original = self.flow.source.guard.observe_scoped
        def observe(*args):
            result = original(*args)
            self.active = False
            return result
        self.flow.source.guard.observe_scoped = observe
        with self.assertRaisesRegex(ValueError, 'source read authorization changed'):
            self.flow.guard.observe_inputs()

    def test_source_change_between_observation_reads_is_not_returned_as_stable_input(self):
        self.prepare_batch_observation()
        original = self.flow.source.guard._read_snapshot
        calls = 0
        def snapshot(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                table = self.flow.source.resource.tables['foodtech-data-flow-dev']
                table.items[('JEYUK#batch-current', 'SOURCE#REVISION')]['revision'] += 1
            return original(*args)
        self.flow.source.guard._read_snapshot = snapshot
        with self.assertRaisesRegex(ValueError, 'recipe source changed or is unavailable'):
            self.flow.guard.observe_inputs()

    def test_revoked_before_call_does_not_read_sources_or_call_model(self):
        self.active = False
        def forbidden(*args, **kwargs):
            raise AssertionError('source payload must not be read after denial')
        self.flow.source.guard.settlement_checks = forbidden
        result, calls = self.invoke()
        self.assertEqual(result.state, 'DENIED')
        self.assertIsNone(result.response_text)
        self.assertEqual(calls, [])

    def test_wrong_environment_or_invalid_authorization_never_reaches_model_or_settlement(self):
        other = replace(self.receipt, auth_table='foodtech-auth-sessions-demo', catalog_table='foodtech-source-access-demo')
        for authorization in [object(), DynamoRecipeReadAuthorization(other, client=self.flow.client, clock=lambda: 1000)]:
            self.flow.guard = replace(self.flow.guard, read_authorization=authorization)
            self.flow.service.recipe_source_guard = self.flow.guard
            result, calls = self.invoke()
            self.assertEqual((result.state, result.error_code), ('DENIED', 'SOURCE_SCOPE_UNAVAILABLE'))
            self.assertEqual(calls, [])
            with self.assertRaises(ValueError):
                self.flow.registry.finish('exec', succeeded=True, actual_cost_usd=0,
                    response_text='synthetic', expected_execution=self.flow.execution, recipe_source_guard=self.flow.guard)
        self.assertIsNone(self.flow.registry.execution_result('exec'))

    def test_revoked_during_model_does_not_publish_and_keeps_consumed_call(self):
        result, calls = self.invoke(lambda: setattr(self, 'active', False))
        self.assertEqual(result.state, 'FAILED')
        self.assertIsNone(result.response_text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.flow.registry.budget('tenant-a', 'ollama').calls_remaining, 0)

    def test_revocation_at_atomic_publication_rolls_back_candidate(self):
        self.revoke_at_publish = True
        result, calls = self.invoke()
        self.assertEqual((result.state, result.error_code), ('FAILED', 'RECIPE_SETTLEMENT_CONFLICT'))
        self.assertIsNone(result.response_text)
        stored = self.flow.registry.execution_result('exec')
        self.assertEqual(stored['error_code'], 'RECIPE_SETTLEMENT_CONFLICT')
        self.assertIsNone(stored['response_text'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.grant_checks_at_publish, 3)

    def test_missing_history_blocks_registered_model_and_direct_success_settlement(self):
        self.flow.guard = replace(self.flow.guard, history_authorization=None)
        self.flow.service.recipe_source_guard = self.flow.guard
        result, calls = self.invoke()
        self.assertEqual((result.state, result.error_code), ('DENIED', 'HISTORY_SCOPE_UNAVAILABLE'))
        self.assertEqual(calls, [])
        with self.assertRaises(ValueError):
            self.flow.registry.finish('exec', succeeded=True, actual_cost_usd=0, response_text='synthetic',
                expected_execution=self.flow.execution, recipe_source_guard=self.flow.guard)

    def test_history_change_during_model_discards_candidate(self):
        result, calls = self.invoke(lambda: setattr(self, 'history_active', False))
        self.assertEqual(result.state, 'FAILED')
        self.assertIsNone(result.response_text)
        self.assertEqual(len(calls), 1)

    def test_history_change_at_publication_rolls_back_candidate_atomically(self):
        self.history_change_at_publish = True
        result, calls = self.invoke()
        self.assertEqual((result.state, result.error_code), ('FAILED', 'RECIPE_SETTLEMENT_CONFLICT'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.history_checks_at_publish, 2)
        self.assertIsNone(self.flow.registry.execution_result('exec')['response_text'])
