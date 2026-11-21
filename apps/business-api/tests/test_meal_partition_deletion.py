import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.meal_partition_deletion import delete_one_meal_record


class MealPartitionDeletionTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request, self.meal = str(uuid4()), str(uuid4()), str(uuid4())
        self.pk = f'SCOPE#USER#{self.subject}#COMBI_MEAL#{self.meal}'
        self.table = Mock(name='table'); self.table.name = 'cooking'; self.table.table_arn = 'arn:source'
        self.client = Mock()
        self.progress = SimpleNamespace(auth_table='auth', registry_table='registry', progress_table='progress',
                                        client=self.client, load_checkpoint=Mock())
        self.args = dict(source_table=self.table, progress_store=self.progress,
                         cipher=SimpleNamespace(source_table_arn='arn:source'),
                         subject=self.subject, request_id=self.request, meal_id=self.meal)
        self.meta = {'pk': self.pk, 'sk': 'META', 'meal_batch_id': self.meal}

    def test_deletes_one_record_with_locked_request_and_scoped_meta(self):
        self.table.query.return_value = {'Items': [{'pk': self.pk, 'sk': 'EVENT#1'}, self.meta]}
        self.assertEqual(delete_one_meal_record(**self.args), {'state': 'IN_PROGRESS', 'removed': True})
        ops = self.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[0]['ConditionCheck']['ExpressionAttributeValues'][':request'], {'S': self.request})
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        self.assertEqual(ops[2]['ConditionCheck']['Key']['pk'], {'S': self.pk})
        self.assertEqual(ops[2]['ConditionCheck']['ExpressionAttributeValues'][':id'], {'S': self.meal})
        self.assertEqual(ops[3]['Delete']['Key']['sk'], {'S': 'EVENT#1'})

    def test_meta_only_is_observation_and_rejects_invalid_pages(self):
        self.table.query.return_value = {'Items': [self.meta]}
        self.assertEqual(delete_one_meal_record(**self.args), {'state': 'META_ONLY', 'removed': False})
        self.client.transact_write_items.assert_not_called()
        for page in ({'Items': []}, {'Items': [self.meta], 'LastEvaluatedKey': {'pk': self.pk, 'sk': 'META'}},
                     {'Items': [{**self.meta, 'meal_batch_id': 'other'}]},
                     {'Items': [{'pk': 'other', 'sk': 'EVENT#1'}]},
                     {'Items': [self.meta, self.meta]}):
            self.table.query.return_value = page
            with self.assertRaises(ValueError):
                delete_one_meal_record(**self.args)

    def test_invalid_scope_and_lock_conflict_fail_closed(self):
        for overrides in ({'meal_id': 'x#other'}, {'subject': str(uuid4()).upper()},
                          {'cipher': SimpleNamespace(source_table_arn='other')}):
            with self.assertRaises(ValueError):
                delete_one_meal_record(**{**self.args, **overrides})
        self.table.query.return_value = {'Items': [{'pk': self.pk, 'sk': 'EVENT#1'}]}
        self.client.transact_write_items.side_effect = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):
            delete_one_meal_record(**self.args)


if __name__ == '__main__':
    unittest.main()
