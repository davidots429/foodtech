import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.meal_listing_deletion import clean_one_meal_listing


class MealListingDeletionTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.source = Mock(); self.source.name = 'cooking'; self.source.table_arn = 'arn:source'
        self.client = Mock()
        self.progress = SimpleNamespace(auth_table='auth', registry_table='registry', progress_table='progress',
                                        client=self.client, load_checkpoint=Mock())
        self.args = dict(source_table=self.source, progress_store=self.progress,
                         cipher=SimpleNamespace(source_table_arn='arn:source'),
                         subject=self.subject, request_id=self.request)
        self.row = {'pk': f'SCOPE#USER#{self.subject}#COMBI_MEALS', 'sk': 'MEAL#meal', 'meal_batch_id': 'meal'}

    def test_only_empty_partition_allows_guarded_removal(self):
        self.source.query.side_effect = [{'Items': [self.row]}, {'Items': []}]
        self.assertEqual(clean_one_meal_listing(**self.args), {'removed': True, 'state': 'IN_PROGRESS'})
        checks = self.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(checks[2]['ConditionCheck']['Key']['pk'],
                         {'S': f'SCOPE#USER#{self.subject}#COMBI_MEAL#meal'})
        self.assertEqual(checks[2]['ConditionCheck']['ConditionExpression'], 'attribute_not_exists(pk)')
        self.assertEqual(checks[3]['Delete']['ExpressionAttributeValues'][':id'], {'S': 'meal'})

    def test_nonempty_or_uncertain_partition_and_wrong_listing_are_rejected(self):
        for partition in ({'Items': [{'sk': 'META'}]}, {'Items': [{'sk': 'EVENT#1'}]},
                          {'Items': [], 'LastEvaluatedKey': {'pk': 'other', 'sk': 'META'}}, {}):
            self.source.query.side_effect = [{'Items': [self.row]}, partition]
            with self.assertRaises(ValueError):
                clean_one_meal_listing(**self.args)
        self.source.query.side_effect = None
        self.source.query.return_value = {'Items': [{**self.row, 'meal_batch_id': 'other'}]}
        with self.assertRaises(ValueError):
            clean_one_meal_listing(**self.args)
        self.client.transact_write_items.assert_not_called()

    def test_empty_listing_is_observation_and_lock_conflict_fails(self):
        self.source.query.return_value = {'Items': []}
        self.assertEqual(clean_one_meal_listing(**self.args), {'removed': False, 'state': 'VERIFYING'})
        self.source.query.side_effect = [{'Items': [self.row]}, {'Items': []}]
        self.client.transact_write_items.side_effect = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):
            clean_one_meal_listing(**self.args)


if __name__ == '__main__':
    unittest.main()
