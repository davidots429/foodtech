"""No AWS calls: SDK validation and simulated atomic publication races."""
from dataclasses import replace
import unittest
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from foodtech_ai_gateway.ollama import OllamaResult
from foodtech_ai_gateway.recipe_read_authorization import DynamoRecipeReadAuthorization
from foodtech_ai_gateway.service import GatewayService
from foodtech_contracts.source_read_authorization import SourceReadAuthorization

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
        old_transact = self.flow.client.transact_write_items
        decoder = TypeDeserializer()

        def transact(*, TransactItems, ClientRequestToken):
            is_publication = any('Put' in item and item['Put'].get('Item', {}).get('sk') == {'S': 'RESULT'}
                                 and item['Put'].get('Item', {}).get('state') == {'S': 'SUCCEEDED'} for item in TransactItems)
            if is_publication and self.revoke_at_publish:
                self.active = False
            remaining, grant_checks = [], []
            for item in TransactItems:
                check = item.get('ConditionCheck', {})
                if check.get('TableName') == self.receipt.catalog_table or '#grants' in check.get('ExpressionAttributeNames', {}):
                    grant_checks.append(item)
                elif check.get('TableName') == self.receipt.auth_table:
                    # Adapt only the old fake principal table name; production
                    # requests are checked to use the same auth table above.
                    remaining.append({'ConditionCheck': {**check, 'TableName': 'auth-test'}})
                else:
                    remaining.append(item)
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
        self.flow.guard = replace(self.flow.guard, read_authorization=authorization)
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
