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
        store.client, store.table, store.serializer = client, SimpleNamespace(name='cooking'), TypeSerializer()
        requests = []
        def capture(model, params, **kwargs):
            requests.append(params)
        client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems', capture)
        with Stubber(client) as stubber:
            stubber.add_response('transact_write_items', {}, {'TransactItems': ANY})
            store.create_condition_profile('alice', {'profile_id': 'p1', 'profile_version': 'v1'})
            stubber.assert_no_pending_responses()
        puts = requests[0]['TransactItems']
        self.assertEqual(len(puts), 2)
        self.assertTrue(all(p['Put']['ConditionExpression'] == 'attribute_not_exists(pk)' for p in puts))
        self.assertEqual(puts[1]['Put']['Item']['pk']['S'], 'SCOPE#USER#alice#CONDITION_PROFILES')

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


if __name__ == '__main__':
    unittest.main()
