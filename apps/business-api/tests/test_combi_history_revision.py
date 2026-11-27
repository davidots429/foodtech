"""History invalidation is atomic with persisted meal mutations, not coverage."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from boto3.dynamodb.types import TypeSerializer
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_business_api.cooking_store import MemoryCookingStore, DynamoCookingStore, Conflict


def records(meal='meal'):
    return [{'sk': 'META', 'meal_batch_id': meal, 'version': 1}, {'sk': 'JOB#rice', 'status': 'PENDING'}]


class Cancelled(Exception):
    def __init__(self, reasons):
        self.response = {'CancellationReasons': [{'Code': value} for value in reasons]}


class CombiHistoryRevisionTests(unittest.TestCase):
    def test_create_advance_and_source_attachment_share_one_owner_revision(self):
        store = MemoryCookingStore(); pk = 'SCOPE#USER#owner#COMBI_MEAL#meal'
        self.assertIsNone(store.meal_history_revision('owner'))
        store.create_meal(pk, records())
        self.assertEqual(store.meal_history_revision('owner'), 1)
        with self.assertRaises(Conflict): store.create_meal(pk, records())
        self.assertEqual(store.meal_history_revision('owner'), 1)
        store.advance_meal(pk, 1, {'sk': 'META', 'version': 2, 'meal_batch_id': 'meal'},
                           {'sk': 'JOB#rice', 'status': 'RUNNING'}, {'sk': 'EVENT#2'})
        self.assertEqual(store.meal_history_revision('owner'), 2)
        before = deepcopy(store.items)
        with self.assertRaises(Conflict):
            store.advance_meal(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'JOB#rice'}, {'sk': 'EVENT#3'})
        self.assertEqual(store.items, before)
        store.attach_meal_source(pk, 2, {'sk': 'META', 'version': 3, 'meal_batch_id': 'meal'},
            {'sk': 'EVENT#3'}, [{'sk': 'RAW#0000', 'records': ['raw']}])
        self.assertEqual(store.meal_history_revision('owner'), 3)
        self.assertIsNone(store.history_revision('owner'))
        self.assertIsNone(store.meal_history_revision('foreign'))

    def test_legacy_listing_advances_only_on_first_successful_insertion(self):
        store = MemoryCookingStore(); pk = 'SCOPE#USER#owner#COMBI_MEAL#legacy'
        store.items[(pk, 'META')] = records('legacy')[0]
        store.ensure_meal_listed('owner', 'legacy')
        self.assertEqual(store.meal_history_revision('owner'), 1)
        store.ensure_meal_listed('owner', 'legacy')
        self.assertEqual(store.meal_history_revision('owner'), 1)
        with self.assertRaises(Conflict): store.ensure_meal_listed('other', 'legacy')
        self.assertIsNone(store.meal_history_revision('other'))
        listed = store.list_meal_refs('owner', 100, None)
        self.assertEqual(listed, {'items': [{'meal_batch_id': 'legacy'}], 'next_cursor': None})

    def test_counter_failure_never_partially_mutates_memory_state(self):
        store = MemoryCookingStore(); pk = 'SCOPE#USER#owner#COMBI_MEAL#meal'
        store.create_meal(pk, records())
        legacy = 'SCOPE#USER#owner#COMBI_MEAL#legacy'
        store.items[(legacy, 'META')] = records('legacy')[0]
        key = ('SCOPE#USER#owner#COMBI_MEALS', 'HISTORY#REVISION')
        for invalid in (True, Decimal('1.5'), 10**18):
            store.items[key]['revision'] = invalid
            before = deepcopy(store.items)
            with self.assertRaises(ValueError):
                store.advance_meal(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'JOB#rice'}, {'sk': 'EVENT#2'})
            self.assertEqual(store.items, before)
            with self.assertRaises(ValueError): store.create_meal(pk + '2', records('meal2'))
            self.assertEqual(store.items, before)
            with self.assertRaises(ValueError):
                store.attach_meal_source(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'EVENT#2'}, [])
            self.assertEqual(store.items, before)
            with self.assertRaises(ValueError): store.ensure_meal_listed('owner', 'legacy')
            self.assertEqual(store.items, before)

    def test_a_different_meal_by_the_same_owner_invalidates_the_history_revision(self):
        store = MemoryCookingStore()
        first, second = 'SCOPE#USER#owner#COMBI_MEAL#one', 'SCOPE#USER#owner#COMBI_MEAL#two'
        store.create_meal(first, records('one'))
        observed = store.meal_history_revision('owner')
        store.create_meal(second, records('two'))
        self.assertNotEqual(store.meal_history_revision('owner'), observed)
        observed = store.meal_history_revision('owner')
        store.advance_meal(second, 1, {'sk': 'META', 'version': 2}, {'sk': 'JOB#rice'}, {'sk': 'EVENT#2'})
        self.assertNotEqual(store.meal_history_revision('owner'), observed)
        self.assertEqual(store.read(first)[0]['version'], 1)

    def dynamo(self):
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name='cooking', get_item=Mock())
        store.serializer = TypeSerializer(); store.auth_table_name = 'auth'
        store.client = SimpleNamespace(transact_write_items=Mock(), exceptions=SimpleNamespace(TransactionCanceledException=Cancelled))
        return store

    def test_all_dynamo_mutations_include_counter_in_the_same_valid_transaction(self):
        store = self.dynamo(); pk = 'SCOPE#USER#owner#COMBI_MEAL#meal'
        store.create_meal(pk, records(), 2)
        store.advance_meal(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'JOB#rice'}, {'sk': 'EVENT#2'}, 2)
        store.attach_meal_source(pk, 1, {'sk': 'META', 'version': 2}, {'sk': 'EVENT#2'},
                                 [{'sk': 'RAW#0000', 'records': ['raw']}], None, 2)
        store.ensure_meal_listed('owner', 'meal')
        shape = get_session().get_service_model('dynamodb').operation_model('TransactWriteItems').input_shape
        self.assertEqual(store.client.transact_write_items.call_count, 4)
        for call in store.client.transact_write_items.call_args_list:
            validate_parameters(call.kwargs, shape)
            ops = call.kwargs['TransactItems']
            self.assertIn('deletion_lock_request', ops[0]['ConditionCheck']['ConditionExpression'])
            counter = ops[-1]['Update']
            self.assertEqual(counter['Key'], {'pk': {'S': 'SCOPE#USER#owner#COMBI_MEALS'},
                                               'sk': {'S': 'HISTORY#REVISION'}})
            self.assertEqual(counter['UpdateExpression'], 'ADD revision :one')
            self.assertEqual(counter['ExpressionAttributeValues'][':one'], {'N': '1'})
            self.assertIn('attribute_type(revision, :number)', counter['ConditionExpression'])
            self.assertIn('revision >= :one', counter['ConditionExpression'])

    def test_reconciliation_only_accepts_an_exact_existing_listing_with_no_other_failure(self):
        store = self.dynamo()
        store.client.transact_write_items.side_effect = Cancelled(['None', 'None', 'ConditionalCheckFailed', 'None'])
        listing = {'pk': 'SCOPE#USER#owner#COMBI_MEALS', 'sk': 'MEAL#legacy', 'meal_batch_id': 'legacy'}
        store.table.get_item.return_value = {'Item': listing}
        store.ensure_meal_listed('owner', 'legacy')
        store.table.get_item.assert_called_once_with(Key={'pk': listing['pk'], 'sk': listing['sk']}, ConsistentRead=True)
        store.table.get_item.return_value = {'Item': {**listing, 'meal_batch_id': 'foreign'}}
        with self.assertRaises(Conflict): store.ensure_meal_listed('owner', 'legacy')
        store.table.get_item.reset_mock()
        store.client.transact_write_items.side_effect = Cancelled(['ConditionalCheckFailed', 'None', 'ConditionalCheckFailed', 'None'])
        with self.assertRaises(Conflict): store.ensure_meal_listed('owner', 'legacy')
        store.table.get_item.assert_not_called()

    def test_history_read_is_strongly_consistent_and_validates_counter(self):
        store = self.dynamo()
        store.table.get_item.return_value = {'Item': {'revision': Decimal(3)}}
        self.assertEqual(store.meal_history_revision('owner'), 3)
        store.table.get_item.assert_called_once_with(
            Key={'pk': 'SCOPE#USER#owner#COMBI_MEALS', 'sk': 'HISTORY#REVISION'}, ConsistentRead=True)
        store.table.get_item.return_value = {'Item': {'revision': Decimal('1.5')}}
        with self.assertRaises(ValueError): store.meal_history_revision('owner')
        with self.assertRaises(ValueError): store.meal_history_revision('owner#foreign')

    def test_shared_counter_transaction_contention_retries_but_failed_conditions_do_not(self):
        store = self.dynamo(); pk = 'SCOPE#USER#owner#COMBI_MEAL#meal'
        store.client.transact_write_items.side_effect = [Cancelled(['TransactionConflict', 'None', 'None', 'None', 'None']), None]
        with patch('foodtech_business_api.cooking_store.time.sleep') as sleep:
            store.create_meal(pk, records())
            self.assertEqual(store.client.transact_write_items.call_count, 2)
            sleep.assert_called_once()
        store.client.transact_write_items.reset_mock()
        store.client.transact_write_items.side_effect = Cancelled(['ConditionalCheckFailed', 'None', 'None', 'None', 'None'])
        with patch('foodtech_business_api.cooking_store.time.sleep') as sleep:
            with self.assertRaises(Conflict): store.create_meal(pk, records())
            self.assertEqual(store.client.transact_write_items.call_count, 1)
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
