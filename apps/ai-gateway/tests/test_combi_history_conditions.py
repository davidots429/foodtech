from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import unittest
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_contracts.combi_execution_binding import CombiExecutionBinding
from foodtech_contracts.combi_history_witness import CombiHistoryWitness, roster_digest
from foodtech_ai_gateway.combi_history_conditions import DynamoCombiHistoryConditions
from foodtech_ai_gateway.policy import Execution, Feature


class CombiHistoryConditionTests(unittest.TestCase):
    def setUp(self):
        self.binding = CombiExecutionBinding('RICE', str(uuid4()), 4, str(uuid4()), 'a'*64,
            str(uuid4()), 3, str(uuid4()), 'b'*64, 2)
        digest = roster_digest([self.binding.current_meal_id, self.binding.prior_meal_id])
        self.witness = CombiHistoryWitness('synthetic-owner', self.binding, 'combi-device-1', 'MEDIUM_FIXED_RACK_10',
            12, 3, 2, str(uuid4()), digest, 'c'*64, 12, digest, 'd'*64, 900, 1100)
        self.client = Mock()
        self.guard = DynamoCombiHistoryConditions(self.witness, auth_table='foodtech-auth-sessions-dev',
            cooking_table='foodtech-cooking-jobs-dev', account_id='123456789012', client=self.client, clock=lambda: 1000)
        self.execution = Execution(str(uuid4()), self.witness.owner, Feature.RECIPE_CORRECTION, 'llama3.1:8b', None,
            'SYNTHETIC', Decimal(0), job_id=self.binding.current_job_id, job_version=4,
            prior_job_id=self.binding.prior_job_id, prior_job_version=3, combi_binding=self.binding)

    def test_exact_five_condition_only_targets_bind_history_authority_and_meal_versions(self):
        checks = self.guard.settlement_checks(self.execution)
        shape = get_session().get_service_model('dynamodb').operation_model('TransactWriteItems').input_shape
        validate_parameters({'TransactItems': checks, 'ClientRequestToken': str(uuid4())}, shape)
        self.assertEqual(len(checks), 5)
        self.assertTrue(all(set(x) == {'ConditionCheck'} for x in checks))
        targets = [(x['ConditionCheck']['TableName'], str(x['ConditionCheck']['Key'])) for x in checks]
        self.assertEqual(len(set(targets)), 5)
        self.assertTrue(all(x['ConditionCheck']['ReturnValuesOnConditionCheckFailure'] == 'NONE' for x in checks))
        self.assertEqual(checks[0]['ConditionCheck']['ExpressionAttributeValues'][':ch_epoch'], {'N': '2'})
        self.assertEqual(checks[2]['ConditionCheck']['ExpressionAttributeValues'][':revision'], {'N': '12'})
        self.assertEqual(checks[3]['ConditionCheck']['ExpressionAttributeValues'][':digest'], {'S': 'a'*64})
        self.assertEqual(checks[4]['ConditionCheck']['ExpressionAttributeValues'][':v'], {'N': '3'})
        self.assertNotIn('synthetic-owner', repr(self.guard))

    def test_account_guard_composition_preserves_existing_conditions_without_duplicate_item(self):
        auth = {'ConditionCheck': {'TableName': self.guard.auth_table,
            'Key': self.guard._encode({'pk': 'USER#'+self.witness.owner}),
            'ConditionExpression': 'attribute_exists(pk) AND #epoch = :epoch',
            'ExpressionAttributeNames': {'#epoch': 'epoch'}, 'ExpressionAttributeValues': {':epoch': {'N': '2'}},
            'ReturnValuesOnConditionCheckFailure': 'NONE'}}
        before = deepcopy(auth)
        result = self.guard.merge_auth_check(self.execution, auth)
        self.assertEqual(auth, before)
        self.assertEqual(len(result), 5)
        self.assertIn('(attribute_exists(pk) AND #epoch = :epoch) AND (', result[0]['ConditionCheck']['ConditionExpression'])
        self.assertIn('#ch_cov.#ch_proof', result[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(result[0]['ConditionCheck']['ExpressionAttributeValues'][':epoch'], {'N': '2'})

    def test_foreign_guard_or_alias_collision_cannot_be_merged(self):
        original = self.guard.settlement_checks(self.execution)[0]
        changes = [dict(TableName='foreign'), dict(Key={'pk': {'S': 'USER#other'}}),
            dict(ReturnValuesOnConditionCheckFailure='ALL_OLD'),
            dict(ExpressionAttributeNames={'#ch_epoch': 'not-epoch'}),
            dict(ExpressionAttributeValues={':ch_epoch': {'N': '3'}})]
        for change in changes:
            with self.subTest(change=change):
                value = deepcopy(original)
                value['ConditionCheck'].update(change)
                with self.assertRaises(ValueError): self.guard.merge_auth_check(self.execution, value)
        self.client.transact_write_items.assert_not_called()

    def test_other_owner_binding_epoch_or_source_hash_cannot_be_verified(self):
        for value in (replace(self.execution, tenant_id='other'),
                      replace(self.execution, combi_binding=replace(self.binding, auth_epoch=3)),
                      replace(self.execution, combi_binding=replace(self.binding, current_source_sha256='f'*64))):
            with self.subTest(value=value): self.assertFalse(self.guard.verify(value))
        self.client.transact_write_items.assert_not_called()

    def test_invalid_or_expired_clock_never_calls_aws(self):
        for instant in (True, float('nan'), 0, 899, 1100, 10**1000):
            with self.subTest(instant=instant):
                self.guard.clock = lambda: instant
                self.assertFalse(self.guard.verify(self.execution))
        self.client.transact_write_items.assert_not_called()

    def test_sdk_denial_or_transaction_conflict_is_not_a_verified_witness(self):
        for code in ('AccessDeniedException', 'TransactionCanceledException'):
            self.client.transact_write_items.side_effect = ClientError({'Error': {'Code': code, 'Message': 'private'}}, 'TransactWriteItems')
            self.assertFalse(self.guard.verify(self.execution))
        self.client.transact_write_items.side_effect = None
        self.assertTrue(self.guard.verify(self.execution))
        call = self.client.transact_write_items.call_args.kwargs
        self.assertEqual(len(call['ClientRequestToken']), 36)
        self.assertTrue(all('ConditionCheck' in item for item in call['TransactItems']))

    def test_empty_baseline_is_an_attestation_reference_not_an_automatically_issued_record(self):
        witness = replace(self.witness, coverage_revision=0, baseline_roster_sha256=roster_digest([]))
        guard = DynamoCombiHistoryConditions(witness, auth_table=self.guard.auth_table,
            cooking_table=self.guard.cooking_table, account_id='123456789012', client=self.client, clock=lambda: 1000)
        self.assertEqual(guard.settlement_checks(self.execution)[0]['ConditionCheck']['ExpressionAttributeValues'][':ch_revision'], {'N': '0'})
        self.client.transact_write_items.assert_not_called()

    def test_invalid_witness_and_mismatched_stage_are_rejected(self):
        for changes in ({'history_revision': True}, {'coverage_revision': 0}, {'equipment_size': []},
                        {'coverage_proof_sha256': 'bad'}, {'writer_valid_until': 899}, {'binding': None}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): replace(self.witness, **changes)
        with self.assertRaises(ValueError):
            DynamoCombiHistoryConditions(self.witness, auth_table='foodtech-auth-sessions-dev',
                cooking_table='foodtech-cooking-jobs-demo', account_id='123456789012', client=self.client)


if __name__ == '__main__':
    unittest.main()
