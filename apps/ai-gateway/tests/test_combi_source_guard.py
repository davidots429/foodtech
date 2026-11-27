"""SDK-shaped stored source fixtures; no protocol server or IAM attestation."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
from botocore.exceptions import ClientError
from botocore.paginate import Paginator
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_ai_gateway.combi_history_conditions import DynamoCombiHistoryConditions
from foodtech_ai_gateway.combi_source_guard import DynamoCombiSourceGuard
from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_contracts.combi_execution_binding import CombiExecutionBinding
from foodtech_contracts.combi_history_witness import CombiHistoryWitness, roster_digest
from foodtech_contracts.combi_read_scope import CombiReadScope
from test_combi_observed_inputs import fixture, NOW


class StoredSourceClient:
    def __init__(self):
        self.items, self.calls = {}, []
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()
        self.model = get_session().get_service_model('dynamodb')
        self.before_query, self.before_get, self.bad_cursor = None, None, None

    def encode(self, row): return {k: self.serializer.serialize(v) for k, v in row.items()}
    def decode(self, row): return {k: self.deserializer.deserialize(v) for k, v in row.items()}
    def get_paginator(self, name):
        if name != 'query': raise AssertionError(name)
        return Paginator(self.query, {'input_token': 'ExclusiveStartKey', 'output_token': 'LastEvaluatedKey',
            'limit_key': 'Limit', 'result_key': 'Items'}, self.model.operation_model('Query'))

    def query(self, **args):
        validate_parameters(args, self.model.operation_model('Query').input_shape)
        self.calls.append(('Query', deepcopy(args)))
        if self.before_query: self.before_query(args)
        pk = self.decode(args['ExpressionAttributeValues'])[':pk']
        after = self.decode(args.get('ExclusiveStartKey', {})).get('sk', '')
        rows = [v for (p, sk), v in sorted(self.items.items()) if p == pk and sk > after]
        page = rows[:3]
        result = {'Items': [self.encode(v) for v in page]}
        if len(rows) > len(page): result['LastEvaluatedKey'] = self.encode({'pk': pk, 'sk': page[-1]['sk']})
        if self.bad_cursor: result['LastEvaluatedKey'] = self.encode(self.bad_cursor)
        return result

    def get_item(self, **args):
        validate_parameters(args, self.model.operation_model('GetItem').input_shape)
        self.calls.append(('GetItem', deepcopy(args)))
        if self.before_get: self.before_get(args)
        key = self.decode(args['Key'])
        row = self.items.get((key['pk'], key['sk']))
        return {'Item': self.encode(row)} if row else {}


class CombiSourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.client = StoredSourceClient()
        self.owner, self.records = 'synthetic_owner', []
        for name in ('current', 'prior'):
            rows, raw = fixture(name)
            meta = rows['META']; pk = f'SCOPE#USER#{self.owner}#COMBI_MEAL#{meta["meal_batch_id"]}'
            for sk, row in rows.items():
                row = {**row, 'pk': pk, 'sk': sk}
                self.client.items[(pk, sk)] = row
            for index in range((len(raw)+99)//100):
                key = (pk+'#SOURCE', f'RAW#{index:04d}')
                self.client.items[key] = {'pk': key[0], 'sk': key[1], 'first_sequence': index*100+1,
                    'records': raw[index*100:(index+1)*100]}
            self.records.append((meta, raw))
        current, prior = (row for row, _ in self.records)
        self.binding = CombiExecutionBinding('RICE', current['meal_batch_id'], current['version'], current['rice_cooking_job_id'],
            current['source']['raw_sha256'], prior['meal_batch_id'], prior['version'], prior['rice_cooking_job_id'],
            prior['source']['raw_sha256'], 2)
        digest = roster_digest([self.binding.current_meal_id, self.binding.prior_meal_id])
        self.witness = CombiHistoryWitness(self.owner, self.binding, 'synthetic-combi-1', current['equipment_size'],
            12, 3, 2, str(uuid4()), digest, 'c'*64, 12, digest, 'd'*64, int(NOW.timestamp())-100, int(NOW.timestamp())+100)
        self.execution = Execution(str(uuid4()), self.owner, Feature.RECIPE_CORRECTION, 'llama3.1:8b', None,
            'SYNTHETIC', Decimal(0), job_id=self.binding.current_job_id, job_version=self.binding.current_meal_version,
            prior_job_id=self.binding.prior_job_id, prior_job_version=self.binding.prior_meal_version, combi_binding=self.binding)
        self.scope = CombiReadScope(self.owner, self.execution.execution_id, self.binding, self.witness, 'PENDING', 1)
        self.condition_client = Mock()
        self.conditions = DynamoCombiHistoryConditions(self.witness, auth_table='foodtech-auth-sessions-dev',
            cooking_table='foodtech-cooking-jobs-dev', account_id='123456789012', client=self.condition_client,
            clock=lambda: NOW.timestamp())
        self.guard = DynamoCombiSourceGuard(self.scope, cooking_table='foodtech-cooking-jobs-dev', client=self.client,
            history_conditions=self.conditions, clock=lambda: NOW)

    def test_full_paginated_source_observations_touch_only_four_bound_partitions(self):
        observed = self.guard.observe_inputs(self.execution)
        self.assertEqual(tuple(v.meal_id for v in observed), (self.binding.current_meal_id, self.binding.prior_meal_id))
        targets = set()
        for operation, args in self.client.calls:
            self.assertTrue(args['ConsistentRead'])
            self.assertEqual(args['TableName'], 'foodtech-cooking-jobs-dev')
            self.assertNotIn('ProjectionExpression', args)
            self.assertNotIn('FilterExpression', args)
            target = self.client.decode(args['Key'])['pk'] if operation == 'GetItem' else self.client.decode(args['ExpressionAttributeValues'])[':pk']
            targets.add(target)
        self.assertEqual(targets, set(self.scope.read_partitions))
        self.assertGreater(len([x for x in self.client.calls if x[0] == 'Query']), 4)
        self.assertEqual(self.condition_client.transact_write_items.call_count, 2)
        self.assertNotIn(self.owner, repr(self.guard))
        self.assertNotIn('private-note', json.dumps(observed[0].model_context()))

    def test_foreign_execution_or_failed_history_never_reads_source(self):
        self.assertFalse(self.guard.verify(replace(self.execution, tenant_id='foreign')))
        self.assertEqual(self.client.calls, [])
        self.condition_client.transact_write_items.side_effect = ClientError({'Error': {'Code': 'TransactionCanceledException'}}, 'TransactWriteItems')
        self.assertFalse(self.guard.verify(self.execution))
        self.assertEqual(self.client.calls, [])

    def test_missing_or_foreign_raw_chunks_and_changed_digest_are_denied(self):
        key = (self.scope.source_partitions[0], 'RAW#0000')
        original = deepcopy(self.client.items[key])
        for mutation in (None, {**original, 'pk': self.scope.source_partitions[1]},
                         {**original, 'first_sequence': True}, {**original, 'records': ['damaged']*100}):
            with self.subTest(mutation=mutation is None):
                if mutation is None: self.client.items.pop(key, None)
                else: self.client.items[key] = mutation
                self.assertFalse(self.guard.verify(self.execution))
        self.client.items[key] = original
        self.client.items[(self.scope.meal_partitions[0], 'META')]['source']['raw_sha256'] = 'f'*64
        self.assertFalse(self.guard.verify(self.execution))

    def test_mid_read_boolean_alias_change_is_not_hidden_by_python_equality(self):
        pk = self.scope.meal_partitions[0]
        calls = 0
        def change(args):
            nonlocal calls
            if ':pk' in self.client.decode(args['ExpressionAttributeValues']) and self.client.decode(args['ExpressionAttributeValues'])[':pk'] == pk and 'ExclusiveStartKey' not in args:
                calls += 1
                if calls == 2: self.client.items[(pk, 'META')]['extra'] = 1
        self.client.items[(pk, 'META')]['extra'] = True
        self.client.before_query = change
        self.assertFalse(self.guard.verify(self.execution))

    def test_invalid_cursors_or_partial_result_limits_never_validate(self):
        self.client.bad_cursor = {'pk': 'foreign', 'sk': 'META'}
        self.assertFalse(self.guard.verify(self.execution))
        self.client.bad_cursor = None
        from unittest.mock import patch
        for setting in ('MAX_MEAL_ROWS', 'MAX_MEAL_PAGES'):
            with patch('foodtech_ai_gateway.combi_source_guard.'+setting, 1):
                self.assertFalse(self.guard.verify(self.execution))

    def test_no_quality_or_history_change_blocks_otherwise_complete_source(self):
        pk = self.scope.meal_partitions[0]
        key = next(key for key, row in self.client.items.items() if key[0] == pk and row.get('event') == 'RICE_QUALITY_ASSESSED')
        original = self.client.items.pop(key)
        self.assertFalse(self.guard.verify(self.execution))
        self.client.items[key] = original
        self.condition_client.transact_write_items.reset_mock()
        self.condition_client.transact_write_items.side_effect = [{}, ClientError({'Error': {'Code': 'TransactionCanceledException'}}, 'TransactWriteItems')]
        self.assertFalse(self.guard.verify(self.execution))

    def test_settlement_composes_five_condition_checks_without_a_second_user_item(self):
        existing = {'ConditionCheck': {'TableName': 'foodtech-auth-sessions-dev',
            'Key': self.conditions._encode({'pk': 'USER#'+self.owner}), 'ConditionExpression': 'attribute_exists(pk)'}}
        checks = self.guard.settlement_checks(self.execution, existing)
        self.assertEqual(len(checks), 5)
        for check in checks: self.assertEqual(check['ConditionCheck']['ReturnValuesOnConditionCheckFailure'], 'NONE')
        validate_parameters({'TransactItems': checks}, self.client.model.operation_model('TransactWriteItems').input_shape)
        with self.assertRaises(ValueError): self.guard.settlement_checks(replace(self.execution, tenant_id='foreign'), existing)

    def test_foreign_device_oversized_count_or_foreign_metadata_fails_closed(self):
        key = (self.scope.meal_partitions[0], 'META')
        original = deepcopy(self.client.items[key])
        for change in ({'device_id': 'another-device'}, {'record_count': Decimal('1E100000')}):
            self.client.items[key]['source'].update(change)
            self.assertFalse(self.guard.verify(self.execution))
            self.client.items[key] = deepcopy(original)
        self.client.items[key]['pk'] = self.scope.meal_partitions[1]
        self.assertFalse(self.guard.verify(self.execution))

    def test_sdk_failure_clock_rollback_and_wrong_stage_do_not_succeed(self):
        def denied(_):
            raise ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'private detail'}}, 'GetItem')
        self.client.before_get = denied
        with self.assertRaisesRegex(ValueError, '^verified combi source unavailable$'):
            self.guard.observe_inputs(self.execution)
        self.client.before_get = None
        from datetime import timedelta
        self.guard.clock = Mock(side_effect=[NOW, NOW-timedelta(seconds=1)])
        self.assertFalse(self.guard.verify(self.execution))
        for changes in ({'client': None}, {'cooking_table': 'foodtech-cooking-jobs-demo'},
                        {'history_conditions': None}):
            args = dict(cooking_table='foodtech-cooking-jobs-dev', client=self.client, history_conditions=self.conditions)
            args.update(changes)
            with self.assertRaises(ValueError): DynamoCombiSourceGuard(self.scope, **args)
