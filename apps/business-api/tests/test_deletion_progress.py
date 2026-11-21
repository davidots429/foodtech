import unittest
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.deletion_progress import DeletionProgressConflict, DeletionProgressStore


class Client:
    def __init__(self):
        self.calls = []
        self.error = None

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.store = DeletionProgressStore(auth_table='auth', registry_table='registry', progress_table='progress', client=self.client)
        self.args = dict(subject=str(uuid4()), request_id=str(uuid4()), domain='COOKING', expected_version=0,
                         state='IN_PROGRESS', encrypted_checkpoint=b'ciphertext')

    def test_first_and_following_writes_guard_lock_registry_and_version(self):
        self.assertEqual(self.store.save(**self.args), 1)
        ops = self.client.calls[-1]['TransactItems']
        self.assertEqual([next(iter(op)) for op in ops], ['ConditionCheck', 'ConditionCheck', 'Update'])
        self.assertIn('deletion_lock_request', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        self.assertEqual(ops[2]['Update']['ConditionExpression'], 'attribute_not_exists(pk)')
        self.assertEqual(self.store.save(**{**self.args, 'expected_version': 1, 'state': 'VERIFYING'}), 2)
        update = self.client.calls[-1]['TransactItems'][2]['Update']
        self.assertEqual(update['ConditionExpression'], '#version = :expected')
        self.assertEqual(update['ExpressionAttributeValues'][':expected'], {'N': '1'})

    def test_invalid_scope_and_completion_are_rejected_without_writes(self):
        for changes in ({'state': 'COMPLETED'}, {'expected_version': True}, {'domain': 'OTHER#CUSTOMER'},
                        {'encrypted_checkpoint': b''}, {'request_id': 'invalid'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save(**{**self.args, **changes})
        self.assertEqual(self.client.calls, [])

    def test_conditional_conflict_is_not_a_success(self):
        self.client.error = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(DeletionProgressConflict):
            self.store.save(**self.args)


if __name__ == '__main__':
    unittest.main()
