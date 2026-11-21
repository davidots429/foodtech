"""Opt-in test against a separately created temporary GSI table; no application data."""
import os
import time
from uuid import uuid4
import boto3
from foodtech_business_api.cooking_store import DynamoCookingStore, meal_aggregate_shard
from foodtech_business_api.combi_refresh_worker import process_pending_page
from boto3.dynamodb.conditions import Key

name = os.environ['FOODTECH_TEST_TABLE']
if not name.startswith('foodtech-refresh-test-') or os.environ.get('AWS_PROFILE') != 'foodtech' or \
        os.environ.get('FOODTECH_COMBI_INDEX_LIVE') != '1':
    raise SystemExit('Explicit development profile, temporary table and opt-in required')
session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
assert session.client('sts').get_caller_identity()['Account'] == '573693340100'
auth = session.resource('dynamodb').Table('foodtech-auth-sessions-dev')
store = DynamoCookingStore(name)
subject = 'synthetic-index-' + uuid4().hex
meal_id = str(uuid4())
pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{meal_id}'
auth_key = {'pk': f'USER#{subject}'}
meta = {'sk': 'META', 'version': 1, 'meal_batch_id': meal_id}
auth_created = False
meal_created = False
try:
    auth.put_item(Item={**auth_key, 'epoch': 0, 'suspended': False}, ConditionExpression='attribute_not_exists(pk)')
    auth_created = True
    store.create_meal(pk, [meta, {'sk': 'JOB', 'status': 'PENDING'}])
    meal_created = True
    store.advance_meal(pk, 1, {**meta, 'version': 2}, {'sk': 'JOB', 'status': 'DONE'}, {'sk': 'EVENT#2'})
    shard = int(meal_aggregate_shard(pk).split('#')[1], 16)
    for _ in range(30):
        result = process_pending_page(store, auth, shard, limit=1)
        if result['processed']:
            assert result['processed'] == 1 and result['failed'] == 0
            break
        assert result['failed'] == 0
        time.sleep(1)
    else:
        raise AssertionError('pending marker was not visible in GSI')
    assert store.read_meal_aggregate_dirty(pk) is None
    for _ in range(30):
        page = store.table.query(IndexName='MealAggregatePending', KeyConditionExpression=Key('aggregate_refresh_shard').eq(meal_aggregate_shard(pk)))
        if not page.get('Items'):
            break
        time.sleep(1)
    else:
        raise AssertionError('deleted marker remains in GSI')
    print('real GSI discovery, actual worker acknowledgement and eventual removal verified')
finally:
    if meal_created:
        with store.table.batch_writer() as batch:
            for row in store.table.query(KeyConditionExpression=Key('pk').eq(pk), ConsistentRead=True).get('Items', []):
                batch.delete_item(Key={'pk': row['pk'], 'sk': row['sk']})
        assert not store.table.query(KeyConditionExpression=Key('pk').eq(pk), ConsistentRead=True).get('Items')
    if auth_created:
        auth.delete_item(Key=auth_key)
        assert not auth.get_item(Key=auth_key, ConsistentRead=True).get('Item')
    print('created synthetic rows cleaned')
