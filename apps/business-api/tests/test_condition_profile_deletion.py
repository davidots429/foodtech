import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict, delete_profile_pair


class ProfileDeletionTests(unittest.TestCase):
    def setUp(self):
        self.table, self.client = SimpleNamespace(name='cooking'), Mock()
        self.args = {'auth_table': 'auth', 'registry_table': 'registry', 'subject': 'alice', 'request_id': str(uuid4()),
                     'profile_id': 'profile', 'version': 'v1'}

    def test_guarded_pair_transaction_scopes_both_records(self):
        delete_profile_pair(self.table, self.client, **self.args)
        operations = self.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(operations), 4)
        self.assertEqual(operations[0]['ConditionCheck']['Key'], {'pk': {'S': 'USER#alice'}})
        self.assertIn('suspended = :yes', operations[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(operations[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        self.assertEqual(operations[2]['Delete']['Key'], {'pk': {'S': 'SCOPE#USER#alice#CONDITION_PROFILE#profile'},
                                                          'sk': {'S': 'VERSION#v1'}})
        self.assertEqual(operations[3]['Delete']['Key'], {'pk': {'S': 'SCOPE#USER#alice#CONDITION_PROFILES'},
                                                          'sk': {'S': 'PROFILE#profile#VERSION#v1'}})
        self.assertIn('attribute_not_exists(pk)', operations[2]['Delete']['ConditionExpression'])

    def test_invalid_scope_and_failed_condition_do_not_report_success(self):
        for change in ({'subject': 'alice#other'}, {'request_id': 'bad'}, {'profile_id': 'p#x'}, {'auth_table': 'cooking'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                delete_profile_pair(self.table, self.client, **{**self.args, **change})
        self.client.transact_write_items.assert_not_called()
        self.client.transact_write_items.side_effect = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):
            delete_profile_pair(self.table, self.client, **self.args)


if __name__ == '__main__':
    unittest.main()
