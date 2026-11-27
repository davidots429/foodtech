import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from botocore.session import get_session
from botocore.validate import validate_parameters
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

    def test_history_counter_is_removed_only_under_the_same_deletion_guards(self):
        counter = {'pk': self.row['pk'], 'sk': 'HISTORY#REVISION', 'revision': 7}
        self.source.query.side_effect = [{'Items': []}, {'Items': [counter]}]
        self.assertEqual(clean_one_meal_listing(**self.args), {'removed': True, 'state': 'IN_PROGRESS'})
        operations = self.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(operations[0]['ConditionCheck']['TableName'], 'auth')
        self.assertIn('deletion_lock_request = :request', operations[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(operations[1]['ConditionCheck']['TableName'], 'registry')
        self.assertEqual(operations[2]['Delete']['Key']['sk'], {'S': 'HISTORY#REVISION'})
        self.assertEqual(operations[2]['Delete']['ExpressionAttributeValues'], {':revision': {'N': '7'}})
        shape = get_session().get_service_model('dynamodb').operation_model('TransactWriteItems').input_shape
        validate_parameters({'TransactItems': operations}, shape)
        self.source.query.side_effect = [{'Items': []}, {'Items': []}]
        self.assertEqual(clean_one_meal_listing(**self.args), {'removed': False, 'state': 'VERIFYING'})

    def test_unknown_malformed_or_uncertain_listing_residual_never_claims_empty(self):
        for page in ({'Items': [{'pk': self.row['pk'], 'sk': 'UNKNOWN'}]},
                     {'Items': [{'pk': 'SCOPE#USER#new-account#COMBI_MEALS', 'sk': 'HISTORY#REVISION', 'revision': 7}]},
                     {'Items': [{'pk': self.row['pk'], 'sk': 'HISTORY#REVISION', 'revision': True}]},
                     {'Items': [], 'LastEvaluatedKey': {'pk': self.row['pk'], 'sk': 'HISTORY#REVISION'}}):
            with self.subTest(page=page):
                self.source.query.side_effect = [{'Items': []}, page]
                with self.assertRaises(ValueError): clean_one_meal_listing(**self.args)
        self.client.transact_write_items.assert_not_called()

    def test_changed_counter_or_lost_deletion_lock_cannot_be_reported_removed(self):
        self.source.query.side_effect = [{'Items': []}, {'Items': [
            {'pk': self.row['pk'], 'sk': 'HISTORY#REVISION', 'revision': 7}]}]
        self.client.transact_write_items.side_effect = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict): clean_one_meal_listing(**self.args)


if __name__ == '__main__':
    unittest.main()
