"""Opt-in test against a separately created temporary GSI table; no application data."""
import asyncio
import os
import time
from uuid import uuid4
import boto3
from foodtech_business_api.cooking_store import DynamoCookingStore, meal_aggregate_shard
from foodtech_business_api.combi_refresh_worker import process_pending_page
from foodtech_business_api.combi_quality_aggregate import MealAggregateRequest, create_meal_aggregate_for_subject
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
meta = {'sk': 'META', 'version': 1, 'meal_batch_id': meal_id,
        'rice_cooking_job_id': 'rice', 'mode_transition_id': 'transition',
        'mackerel_cooking_job_id': 'fish', 'equipment_size': 'MEDIUM_FIXED_RACK_10'}
extra_partitions = []
auth_created = False
meal_created = False
try:
    auth.put_item(Item={**auth_key, 'epoch': 0, 'suspended': False}, ConditionExpression='attribute_not_exists(pk)')
    auth_created = True
    store.create_meal(pk, [meta, {'sk': 'JOB#RICE#rice', 'status': 'PENDING'},
                           {'sk': 'TRANSITION#transition'}, {'sk': 'JOB#MACKEREL#fish', 'safety_status': 'NOT_MEASURED'}])
    meal_created = True
    request = MealAggregateRequest.model_validate({'meal_ids': [meal_id], 'menu': 'MACKEREL',
        'equipment_size': 'MEDIUM_FIXED_RACK_10', 'recipe_version': 'synthetic', 'load_band': 'PARTIAL',
        'period_start': '2026-11-01T00:00:00Z', 'period_end': '2026-12-01T00:00:00Z'})
    original = asyncio.run(create_meal_aggregate_for_subject(store, subject, 0, request))
    group_pk = f"SCOPE#USER#{subject}#MEAL_AGGREGATE#{original['group_id']}"
    extra_partitions = [group_pk, f'SCOPE#USER#{subject}#MEAL_AGGREGATE_GROUPS']
    store.advance_meal(pk, 1, {**meta, 'version': 2}, {'sk': 'JOB#RICE#rice', 'status': 'DONE'}, {'sk': 'EVENT#2'})
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
    versions = store.list_aggregate_results(group_pk, 10, None)['items']
    assert len(versions) == 2
    updated_id = next(row['result_id'] for row in versions if row['result_id'] != original['result_id'])
    updated = store.aggregate(group_pk, updated_id)
    assert updated['references'][0]['meal_version'] == 2
    assert store.aggregate(group_pk, original['result_id'])['references'][0]['meal_version'] == 1
    for _ in range(30):
        page = store.table.query(IndexName='MealAggregatePending', KeyConditionExpression=Key('aggregate_refresh_shard').eq(meal_aggregate_shard(pk)))
        if not page.get('Items'):
            break
        time.sleep(1)
    else:
        raise AssertionError('deleted marker remains in GSI')
    print('real GSI discovery, aggregate recalculation, snapshot preservation and eventual removal verified')
finally:
    if meal_created:
        with store.table.batch_writer() as batch:
            for row in store.table.query(KeyConditionExpression=Key('pk').eq(pk), ConsistentRead=True).get('Items', []):
                batch.delete_item(Key={'pk': row['pk'], 'sk': row['sk']})
        assert not store.table.query(KeyConditionExpression=Key('pk').eq(pk), ConsistentRead=True).get('Items')
    for extra_pk in extra_partitions:
        with store.table.batch_writer() as batch:
            for row in store.table.query(KeyConditionExpression=Key('pk').eq(extra_pk), ConsistentRead=True).get('Items', []):
                batch.delete_item(Key={'pk': row['pk'], 'sk': row['sk']})
        assert not store.table.query(KeyConditionExpression=Key('pk').eq(extra_pk), ConsistentRead=True).get('Items')
    if auth_created:
        auth.delete_item(Key=auth_key)
        assert not auth.get_item(Key=auth_key, ConsistentRead=True).get('Item')
    print('created synthetic rows cleaned')
