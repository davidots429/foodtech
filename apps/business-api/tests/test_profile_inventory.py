import unittest
from types import SimpleNamespace

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore, MemoryCookingStore


class ProfileInventoryTests(unittest.TestCase):
    def test_memory_pages_are_owner_scoped_and_duplicate_is_atomic(self):
        store = MemoryCookingStore()
        for profile, version in [('p1', 'v1'), ('p1', 'v2'), ('p2', 'v1')]:
            store.create_condition_profile('alice', {'profile_id': profile, 'profile_version': version})
        store.create_condition_profile('bob', {'profile_id': 'private', 'profile_version': 'v1'})
        first = store.list_condition_profile_refs('alice', 2, None)
        second = store.list_condition_profile_refs('alice', 2, first['next_cursor'])
        self.assertEqual(first['items'] + second['items'], [
            {'profile_id': 'p1', 'profile_version': 'v1'}, {'profile_id': 'p1', 'profile_version': 'v2'},
            {'profile_id': 'p2', 'profile_version': 'v1'}])
        self.assertIsNone(second['next_cursor'])
        with self.assertRaises(Conflict):
            store.create_condition_profile('alice', {'profile_id': 'p1', 'profile_version': 'v1'})
        self.assertEqual(store.condition_profile('alice', 'p1', 'v1')['profile_id'], 'p1')
        for limit, cursor in [(0, None), (101, None), (2, 'untrusted')]:
            with self.assertRaises(ValueError):
                store.list_condition_profile_refs('alice', limit, cursor)

    def test_dynamo_creation_is_a_single_conditional_transaction(self):
        client = boto3.client('dynamodb', region_name='ap-southeast-2',
                              aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.client, store.table, store.serializer, store.auth_table_name = client, SimpleNamespace(name='cooking'), TypeSerializer(), 'auth'
        requests = []
        def capture(model, params, **kwargs):
            requests.append(params)
        client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems', capture)
        with Stubber(client) as stubber:
            stubber.add_response('transact_write_items', {}, {'TransactItems': ANY})
            store.create_condition_profile('alice', {'profile_id': 'p1', 'profile_version': 'v1'}, 3)
            stubber.assert_no_pending_responses()
        puts = requests[0]['TransactItems']
        self.assertEqual(len(puts), 3)
        self.assertIn('deletion_lock_request', puts[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(puts[0]['ConditionCheck']['TableName'], 'auth')
        self.assertEqual(puts[0]['ConditionCheck']['ExpressionAttributeValues'][':expected'], {'N': '3'})
        self.assertTrue(all(p['Put']['ConditionExpression'] == 'attribute_not_exists(pk)' for p in puts[1:]))
        self.assertEqual(puts[2]['Put']['Item']['pk']['S'], 'SCOPE#USER#alice#CONDITION_PROFILES')

    def test_dynamo_write_fails_closed_without_auth_table_or_epoch(self):
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.auth_table_name = ''
        with self.assertRaises(ValueError):
            store.create_condition_profile('alice', {'profile_id': 'p1', 'profile_version': 'v1'})
        store.auth_table_name = 'auth'
        with self.assertRaises(ValueError):
            store.create_condition_profile('alice', {'profile_id': 'p1', 'profile_version': 'v1'}, True)

    def test_dynamo_inventory_uses_consistent_page_and_scoped_cursor(self):
        calls = []
        def query(**kwargs):
            calls.append(kwargs)
            return {'Items': [{'profile_id': 'p1', 'profile_version': 'v1'}],
                    'LastEvaluatedKey': {'pk': 'SCOPE#USER#alice#CONDITION_PROFILES',
                                         'sk': 'PROFILE#p1#VERSION#v1'}}
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(query=query)
        result = store.list_condition_profile_refs('alice', 1, 'PROFILE#p0#VERSION#v1')
        self.assertTrue(calls[0]['ConsistentRead'])
        self.assertEqual(calls[0]['ExclusiveStartKey']['pk'], 'SCOPE#USER#alice#CONDITION_PROFILES')
        self.assertEqual(result['items'], [{'profile_id': 'p1', 'profile_version': 'v1'}])
        self.assertEqual(result['next_cursor'], 'PROFILE#p1#VERSION#v1')

    def test_legacy_reference_requires_existing_matching_source_and_is_idempotent(self):
        store = MemoryCookingStore()
        source_key = ('SCOPE#USER#alice#CONDITION_PROFILE#legacy', 'VERSION#v1')
        store.items[source_key] = {'pk': source_key[0], 'sk': source_key[1],
                                   'profile_id': 'legacy', 'profile_version': 'v1'}
        store.ensure_condition_profile_listed('alice', 'legacy', 'v1')
        store.ensure_condition_profile_listed('alice', 'legacy', 'v1')
        self.assertEqual(store.list_condition_profile_refs('alice', 10, None)['items'],
                         [{'profile_id': 'legacy', 'profile_version': 'v1'}])
        with self.assertRaises(Conflict):
            store.ensure_condition_profile_listed('bob', 'legacy', 'v1')
        store.items[source_key]['profile_version'] = 'different'
        with self.assertRaises(Conflict):
            store.ensure_condition_profile_listed('alice', 'legacy', 'v1')

    def test_dynamo_reconciliation_is_single_transaction(self):
        client = boto3.client('dynamodb', region_name='ap-southeast-2',
                              aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.client, store.table, store.serializer = client, SimpleNamespace(name='cooking'), TypeSerializer()
        requests = []
        client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
                                    lambda model, params, **kwargs: requests.append(params))
        with Stubber(client) as stubber:
            stubber.add_response('transact_write_items', {}, {'TransactItems': ANY})
            store.ensure_condition_profile_listed('alice', 'legacy', 'v1')
        operations = requests[0]['TransactItems']
        self.assertIn('ConditionCheck', operations[0])
        self.assertIn('Update', operations[1])
        self.assertIn('profile_version = :version', operations[0]['ConditionCheck']['ConditionExpression'])


if __name__ == '__main__':
    unittest.main()
