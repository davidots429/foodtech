"""Opt-in selected-meal aggregate check using synthetic development rows only."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import boto3
import httpx

from foodtech_business_api import cooking
from foodtech_business_api.assessment_aggregate import _hash
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from foodtech_contracts.combi_quality_load import RULE_VERSION
from process_flow_dynamodb_check import ACCOUNT, REGION, remove_partition

TABLE = 'foodtech-cooking-jobs-dev'


async def scenario(store, partitions, auth, auth_keys):
    subject = 'synthetic-meal-aggregate-' + uuid4().hex
    meal_id, rice_id, transition_id, fish_id = (str(uuid4()) for _ in range(4))
    meal_pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{meal_id}'
    partitions.append(meal_pk)
    recipe = 'synthetic-' + uuid4().hex
    event_sk = 'EVENT#000000000002'
    pointer = {'version': 1, 'safety_version': 1, 'event_sk': event_sk, 'usable': True,
               'quality_status': 'QUALITY_PASSED', 'score_percent': '80.00', 'quality_target': True}
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    completed = start + timedelta(hours=1)
    assessment = {'version': 1, 'safety_version': 1, 'menu': 'MACKEREL', 'status': 'COMPLETED',
        'scale_version': 'mackerel-common-v1', 'scores': {name: 4 for name in cooking.SCORE_ITEMS},
        'quality_status': 'QUALITY_PASSED', 'quality_passed': True, 'quality_target': True,
        'score_percent': '80.00', 'actual_mass_kg': '7.5', 'actual_portions': 50,
        'completed_at': completed.isoformat()}
    records = [
        {'pk': meal_pk, 'sk': 'META', 'version': 1, 'meal_batch_id': meal_id, 'status': 'MACKEREL_UNLOADED',
         'equipment_size': 'MEDIUM_FIXED_RACK_10', 'rice_cooking_job_id': rice_id,
         'mode_transition_id': transition_id, 'mackerel_cooking_job_id': fish_id, 'mackerel_recipe_version': recipe},
        {'pk': meal_pk, 'sk': f'JOB#RICE#{rice_id}', 'status': 'PENDING'},
        {'pk': meal_pk, 'sk': f'TRANSITION#{transition_id}', 'status': 'PENDING'},
        {'pk': meal_pk, 'sk': f'JOB#MACKEREL#{fish_id}', 'status': 'UNLOADED', 'safety_status': 'PASSED',
         'safety_version': 1, 'quality_version': 1, 'quality_current': pointer},
        {'pk': meal_pk, 'sk': event_sk, 'event': 'MACKEREL_QUALITY_ASSESSED', 'assessment': assessment},
    ]
    selection = {'meal_ids': [meal_id], 'menu': 'MACKEREL', 'equipment_size': 'MEDIUM_FIXED_RACK_10',
        'recipe_version': recipe, 'load_band': 'PARTIAL', 'period_start': start.isoformat(),
        'period_end': (start + timedelta(days=1)).isoformat(), 'load_rule_version': RULE_VERSION}
    group = _hash(selection)
    aggregate_pk = f'SCOPE#USER#{subject}#MEAL_AGGREGATE#{group}'
    partitions.append(aggregate_pk)
    partitions.append(f'SCOPE#USER#{subject}#MEAL_AGGREGATE_GROUPS')
    candidate_selection = {**selection, 'period_start': (start - timedelta(days=1)).isoformat()}
    candidate_group = _hash(candidate_selection)
    partitions.append(f'SCOPE#USER#{subject}#MEAL_AGGREGATE#{candidate_group}')
    for record in records:
        store.table.put_item(Item=record, ConditionExpression='attribute_not_exists(pk)')
    owner = [subject]
    auth_key = {'pk': f'USER#{subject}'}
    auth.put_item(Item={**auth_key, 'epoch': 0, 'suspended': False}, ConditionExpression='attribute_not_exists(pk)')
    auth_keys.append(auth_key)

    async def session(_request, surface):
        assert surface == 'customer'
        return Session('customer', owner[0], None, 'test-csrf', 9999999999, 0)

    with patch.dict(os.environ, {'FOODTECH_SECURE_COOKIES': 'false'}), \
            patch.object(cooking, '_session', side_effect=session), \
            patch.object(cooking, 'get_cooking_store', return_value=store):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            body = {key: value for key, value in selection.items() if key != 'load_rule_version'}
            headers = {'X-CSRF-Token': 'test-csrf'}
            response = await client.post('/api/customer/combi-quality-aggregates', headers=headers, json=body)
            response.raise_for_status()
            result = response.json()
            assert result['group_id'] == group and result['sample_count'] == 1
            assert result['mean_quality_score_percent'] == '80.00'
            groups = await client.get('/api/customer/combi-quality-aggregates/groups')
            groups.raise_for_status()
            assert [item['group_id'] for item in groups.json()['items']] == [group]
            dependency_path = f'/api/customer/combi-quality-aggregates/meals/{meal_id}/groups'
            assert [item['group_id'] for item in (await client.get(dependency_path)).json()['items']] == [group]
            retry = await client.post('/api/customer/combi-quality-aggregates', headers=headers, json=body)
            retry.raise_for_status()
            assert retry.json()['result_id'] == result['result_id']
            group_key = {'pk': f'SCOPE#USER#{subject}#MEAL_AGGREGATE_GROUPS', 'sk': f'GROUP#{group}'}
            store.table.delete_item(Key=group_key)
            dependency_key = {'pk': meal_pk, 'sk': f'MEAL_AGGREGATE#DEP#{group}'}
            store.table.delete_item(Key=dependency_key)
            repaired = await client.post('/api/customer/combi-quality-aggregates', headers=headers, json=body)
            repaired.raise_for_status()
            assert store.table.get_item(Key=group_key, ConsistentRead=True).get('Item')
            assert store.table.get_item(Key=dependency_key, ConsistentRead=True).get('Item')
            auth.update_item(Key=auth_key, UpdateExpression='SET deletion_lock_request = :request',
                             ExpressionAttributeValues={':request': str(uuid4())})
            blocked = await client.post('/api/customer/combi-quality-aggregates', headers=headers, json=body)
            assert blocked.status_code == 409, blocked.text
            auth.update_item(Key=auth_key, UpdateExpression='SET #epoch = :epoch REMOVE deletion_lock_request',
                             ExpressionAttributeNames={'#epoch': 'epoch'}, ExpressionAttributeValues={':epoch': 1})
            stale = await client.post('/api/customer/combi-quality-aggregates', headers=headers, json=body)
            assert stale.status_code == 409, stale.text
            auth.update_item(Key=auth_key, UpdateExpression='SET #epoch = :epoch',
                             ExpressionAttributeNames={'#epoch': 'epoch'}, ExpressionAttributeValues={':epoch': 0})
            link = f"/api/customer/combi-quality-aggregates/{group}/{result['result_id']}"
            assert (await client.get(link)).status_code == 200
            assert (await client.get(link + '/freshness')).json()['status'] == 'UNCHANGED_AS_OBSERVED'
            candidate_body = {key: value for key, value in candidate_selection.items() if key != 'load_rule_version'}
            candidate_response = await client.post('/api/customer/combi-quality-aggregates', headers=headers,
                                                   json=candidate_body)
            candidate_response.raise_for_status()
            candidate = candidate_response.json()
            compare_body = {'baseline_group_id': group, 'baseline_result_id': result['result_id'],
                            'candidate_group_id': candidate_group, 'candidate_result_id': candidate['result_id']}
            compared = await client.post('/api/customer/combi-quality-aggregates/compare', headers=headers,
                                         json=compare_body)
            compared.raise_for_status()
            assert compared.json()['deltas']['mean_quality_score_percent'] == '0.00'
            owner[0] = 'synthetic-other-' + uuid4().hex
            assert (await client.get(link)).status_code == 404
            assert (await client.get(link + '/freshness')).status_code == 404
            assert (await client.get('/api/customer/combi-quality-aggregates/groups')).json()['items'] == []
            assert (await client.get(dependency_path)).status_code == 404
            assert (await client.post('/api/customer/combi-quality-aggregates', headers=headers, json=body)).status_code == 404
            assert (await client.post('/api/customer/combi-quality-aggregates/compare', headers=headers, json=compare_body)).status_code == 404
            owner[0] = subject
            original = store.save_meal_aggregate

            def change_before_save(*args):
                store.advance_meal(meal_pk, 1, {**records[0], 'version': 2}, records[3],
                                   {'sk': 'EVENT#000000000003', 'event': 'SYNTHETIC_CHANGE'})
                return original(*args)

            with patch.object(store, 'save_meal_aggregate', side_effect=change_before_save):
                changed = await client.post('/api/customer/combi-quality-aggregates', headers=headers, json=body)
            assert changed.status_code == 409, changed.text
            dirty = store.table.get_item(Key={'pk': meal_pk, 'sk': 'MEAL_AGGREGATE#DIRTY'}, ConsistentRead=True)['Item']
            assert int(dirty['version']) == 2 and dirty['state'] == 'PENDING'
            freshness = await client.get(link + '/freshness')
            assert freshness.json()['status'] == 'CHANGED'
            assert freshness.json()['changed'] == [{'meal_id': meal_id, 'reason': 'VERSION_CHANGED'}]
            refreshed = await client.post(link + '/recalculate', headers=headers)
            refreshed.raise_for_status()
            assert refreshed.json()['group_id'] == group and refreshed.json()['result_id'] != result['result_id']
            assert (await client.get(link)).json()['result_id'] == result['result_id']
            list_path = f'/api/customer/combi-quality-aggregates/groups/{group}/results?limit=1'
            first_page = await client.get(list_path)
            first_page.raise_for_status()
            assert len(first_page.json()['items']) == 1 and first_page.json()['next_cursor']
            second_page = await client.get(list_path + '&cursor=' + first_page.json()['next_cursor'])
            second_page.raise_for_status()
            assert len(second_page.json()['items']) == 1 and second_page.json()['next_cursor'] is None
            owner[0] = 'synthetic-other-' + uuid4().hex
            assert (await client.get(list_path)).json()['items'] == []
            owner[0] = subject
            compared = await client.post('/api/customer/combi-quality-aggregates/compare', headers=headers,
                                         json=compare_body)
            assert compared.json()['baseline']['freshness']['status'] == 'CHANGED'
    print('synthetic meal aggregate, retry, version conflict and customer scope verified')


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_COMBI_AGGREGATE_LIVE') != '1':
        raise SystemExit('explicit development profile and FOODTECH_COMBI_AGGREGATE_LIVE=1 required')
    session = boto3.Session(profile_name='foodtech', region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise SystemExit('unexpected account')
    store, partitions, auth_keys = DynamoCookingStore(TABLE), [], []
    store.auth_table_name = 'foodtech-auth-sessions-dev'
    auth = session.resource('dynamodb').Table(store.auth_table_name)
    try:
        asyncio.run(scenario(store, partitions, auth, auth_keys))
    finally:
        for pk in reversed(partitions):
            remove_partition(store.table, pk)
        for auth_key in auth_keys:
            auth.delete_item(Key=auth_key)
            assert not auth.get_item(Key=auth_key, ConsistentRead=True).get('Item')
        print('synthetic partition cleanup verified')


if __name__ == '__main__':
    main()
