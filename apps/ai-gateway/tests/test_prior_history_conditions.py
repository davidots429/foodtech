import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import boto3
from botocore.stub import Stubber

from foodtech_ai_gateway.prior_history_conditions import DynamoPriorHistoryConditions
from foodtech_ai_gateway.recipe_read_authorization import DynamoRecipeReadAuthorization
from foodtech_contracts.prior_history_witness import PriorHistoryWitness
from foodtech_contracts.source_read_authorization import SourceReadAuthorization


class HistoryConditionTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client('dynamodb', region_name='ap-southeast-2',
            aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        self.current, self.prior = str(uuid4()), str(uuid4())
        self.sources = ('JEYUK#batch-current', 'JEYUK#batch-prior')
        self.execution = SimpleNamespace(execution_id='exec', tenant_id='tenant-a', feature='RECIPE_CORRECTION',
            job_id=self.current, prior_job_id=self.prior, job_version=7, prior_job_version=3)
        self.receipt = SourceReadAuthorization('foodtech-auth-sessions-dev', 'foodtech-source-access-dev',
            'tenant-a', 'exec', 2, self.sources, (str(uuid4()), str(uuid4())), (1, 2), (2000, 2100))
        self.authorization = DynamoRecipeReadAuthorization(self.receipt, client=self.client, clock=lambda: 1000)
        self.witness = PriorHistoryWitness('tenant-a', self.current, self.prior, (7, 3), 2, 10, 3, 2,
            str(uuid4()), 'a' * 64, 'b' * 64, 10, 'a' * 64, 'c' * 64)
        self.guard = DynamoPriorHistoryConditions(self.witness, auth_table='foodtech-auth-sessions-dev',
            cooking_table='foodtech-cooking-jobs-dev', account_id='123456789012', client=self.client, clock=lambda: 1000)

    def test_merges_account_conditions_and_has_five_unique_condition_only_targets(self):
        original = self.authorization.settlement_checks(self.execution, self.sources)
        checks = self.guard.settlement_checks(self.execution, self.authorization, self.sources)
        self.assertEqual(len(checks), 5)
        keys = [(x['ConditionCheck']['TableName'], json.dumps(x['ConditionCheck']['Key'], sort_keys=True)) for x in checks]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertTrue(all(set(x) == {'ConditionCheck'} and x['ConditionCheck']['ReturnValuesOnConditionCheckFailure'] == 'NONE'
                            for x in checks))
        account = checks[0]['ConditionCheck']
        for text in ['#epoch = :epoch', '#grants.#first = :first', 'attribute_not_exists(deletion_lock_request)',
                     '#hist.#hv = :hv', '#hist.#hproof = :hproof', '#hist.#hbase = :hbase']:
            self.assertIn(text, account['ConditionExpression'])
        self.assertNotIn('#hist', original[0]['ConditionCheck']['ExpressionAttributeNames'])
        self.assertIn('valid_until > :now', checks[-2]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(checks[-1]['ConditionCheck']['ExpressionAttributeValues'][':revision'], {'N': '10'})
        with Stubber(self.client) as stub:
            stub.add_response('transact_write_items', {})
            self.assertTrue(self.guard.verify(self.execution, self.authorization, self.sources))
            stub.assert_no_pending_responses()

    def test_changed_execution_epoch_source_or_client_never_checks_aws(self):
        with Stubber(self.client):
            wrong = SimpleNamespace(**{**vars(self.execution), 'prior_job_version': 4})
            self.assertFalse(self.guard.verify(wrong, self.authorization, self.sources))
            wrong_id = SimpleNamespace(**{**vars(self.execution), 'execution_id': 'another-execution'})
            self.assertFalse(self.guard.verify(wrong_id, self.authorization, self.sources))
            other_auth = DynamoRecipeReadAuthorization(replace(self.receipt, auth_epoch=3), client=self.client, clock=lambda: 1000)
            self.assertFalse(self.guard.verify(self.execution, other_auth, self.sources))
            self.assertFalse(self.guard.verify(self.execution, self.authorization, self.sources[::-1]))
            other_client = DynamoRecipeReadAuthorization(self.receipt, client=object(), clock=lambda: 1000)
            self.assertFalse(self.guard.verify(self.execution, other_client, self.sources))

    def test_denial_or_unavailable_history_never_returns_authorization(self):
        for code in ['TransactionCanceledException', 'AccessDeniedException', 'InternalServerError']:
            with self.subTest(code=code), Stubber(self.client) as stub:
                stub.add_client_error('transact_write_items', code, 'synthetic', http_status_code=400)
                self.assertFalse(self.guard.verify(self.execution, self.authorization, self.sources))
                stub.assert_no_pending_responses()

    def test_bad_clock_and_bad_witness_shape_are_rejected(self):
        with Stubber(self.client):
            for value in [True, float('nan'), float('inf'), 0]:
                self.guard.clock = lambda value=value: value
                self.assertFalse(self.guard.verify(self.execution, self.authorization, self.sources))
        for changes in [{'auth_epoch': True}, {'job_versions': [7, 3]}, {'generation': 'not-uuid'},
                        {'coverage_revision': 11}, {'roster_sha256': 'd' * 64}, {'coverage_proof_sha256': 'invalid'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.witness, **changes)


if __name__ == '__main__':
    unittest.main()
