import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.tier_store import DynamoTierStore


class TierGuardTests(unittest.TestCase):
    def setUp(self):
        self.store = DynamoTierStore.__new__(DynamoTierStore)
        self.store.table = SimpleNamespace(name='tier')
        self.store.client = Mock()
        self.store.client.exceptions.TransactionCanceledException = type('Canceled', (Exception,), {})
        self.store.serializer = TypeSerializer()
        self.store.auth_table_name = 'auth'
        self.item = {'application_id': 'app', 'submitted_at': '2026-01-01',
                     'status': 'REQUESTED', 'version': 1}

    def test_submit_checks_epoch_and_lock(self):
        self.store.submit('customer', self.item, 4)
        ops = self.store.client.transact_write_items.call_args.kwargs['TransactItems']
        guard = ops[0]['ConditionCheck']
        self.assertEqual(guard['Key']['pk'], {'S': 'USER#customer'})
        self.assertEqual(guard['ExpressionAttributeValues'][':expected'], {'N': '4'})
        self.assertIn('deletion_lock_request', guard['ConditionExpression'])
        self.assertEqual(len(ops), 3)

    def test_review_checks_customer_and_admin_without_duplicate_keys(self):
        self.store.current = Mock(return_value=self.item)
        self.store.decide('customer', 'app', 'admin', 'REJECTED', 'now', 5)
        ops = self.store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual([op['ConditionCheck']['Key']['pk']['S'] for op in ops[:2]],
                         ['USER#customer', 'USER#admin'])
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N': '5'})
        self.store.decide('customer', 'app', 'customer', 'REJECTED', 'now', 5)
        ops = self.store.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(ops), 3)
        self.assertEqual(ops[0]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N': '5'})

    def test_missing_auth_config_fails_closed(self):
        self.store.auth_table_name = ''
        with self.assertRaises(ValueError):
            self.store.submit('customer', self.item)


if __name__ == '__main__':
    unittest.main()
