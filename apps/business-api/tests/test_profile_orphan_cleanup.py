import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.profile_orphan_cleanup import clean_one_orphan


class OrphanCleanupTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock(name='source')
        self.table.name, self.table.table_arn = 'cooking', 'arn:source'
        self.client = Mock()
        self.store = SimpleNamespace(auth_table='auth', registry_table='registry', progress_table='progress', client=self.client,
                                     load_checkpoint=Mock(return_value=None))
        self.args = dict(source_table=self.table, progress_store=self.store,
                         cipher=SimpleNamespace(source_table_arn='arn:source'),
                         subject='alice', request_id=str(uuid4()))
        self.item = {'pk': 'SCOPE#USER#alice#CONDITION_PROFILES', 'sk': 'PROFILE#p#VERSION#v1',
                     'profile_id': 'p', 'profile_version': 'v1'}

    def test_removes_only_matching_reference_when_source_absent_and_locked(self):
        self.table.query.return_value = {'Items': [self.item]}
        self.assertEqual(clean_one_orphan(**self.args), {'state': 'VERIFYING', 'removed': True})
        ops = self.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[2]['ConditionCheck']['ConditionExpression'], 'attribute_not_exists(pk)')
        self.assertEqual(ops[2]['ConditionCheck']['Key']['pk'], {'S': 'SCOPE#USER#alice#CONDITION_PROFILE#p'})
        self.assertEqual(ops[3]['Delete']['Key']['sk'], {'S': 'PROFILE#p#VERSION#v1'})
        self.assertIn('suspended', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})

    def test_empty_is_observation_and_bad_rows_fail_closed(self):
        self.table.query.return_value = {'Items': []}
        self.assertEqual(clean_one_orphan(**self.args), {'state': 'VERIFYING', 'removed': False})
        for page in ({'Items': [], 'LastEvaluatedKey': {'pk': 'x'}},
                     {'Items': [{**self.item, 'sk': 'PROFILE#other#VERSION#v1'}]}):
            self.table.query.return_value = page
            with self.assertRaises(ValueError): clean_one_orphan(**self.args)
        self.client.transact_write_items.assert_not_called()

    def test_changed_lock_or_existing_source_fails(self):
        self.table.query.return_value = {'Items': [self.item]}
        self.client.transact_write_items.side_effect = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict): clean_one_orphan(**self.args)
        self.store.load_checkpoint.side_effect = RuntimeError('lock changed')
        self.client.reset_mock()
        with self.assertRaises(RuntimeError): clean_one_orphan(**self.args)
        self.client.transact_write_items.assert_not_called()


if __name__ == '__main__': unittest.main()
