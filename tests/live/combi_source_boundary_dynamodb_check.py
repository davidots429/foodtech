"""Opt-in source-boundary and historical reassessment check on development DynamoDB."""
import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import httpx

from combi_live_dynamodb_check import records_for, remove_and_verify
from foodtech_business_api import combi_meal, cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app

TABLE = 'foodtech-cooking-jobs-dev'


async def scenario(store, partitions):
    subject = 'synthetic-source-boundary-' + uuid.uuid4().hex
    ids = [uuid.uuid4() for _ in range(4)]
    pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{ids[0]}'
    partitions.extend((pk, pk + '#LIVE', pk + '#SOURCE'))
    async def session(_request, surface):
        assert surface == 'customer'
        return Session('customer', subject, None, 'test-csrf', 9999999999, 0)
    with patch.dict(os.environ, {'FOODTECH_SECURE_COOKIES': 'false'}), \
         patch.object(cooking, '_session', side_effect=session), \
         patch.object(cooking, 'get_cooking_store', return_value=store), \
         patch.object(combi_meal, 'uuid4', side_effect=ids):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            headers = {'X-CSRF-Token': 'test-csrf'}
            created = await client.post('/api/customer/combi-meals', headers=headers, json={
                'equipment_size': 'MEDIUM_FIXED_RACK_10', 'rack_count': 1, 'pan_count': 10,
                'dry_rice_kg': '20', 'drained_rice_kg': '24', 'added_water_kg': '20',
                'pan_tare_kg': '10', 'rack_tare_kg': '0'})
            created.raise_for_status()
            meal = created.json()['meal']
            path = '/api/customer/combi-meals/' + meal['meal_batch_id']
            imported = await client.post(path + '/source-observations', headers=headers, json={
                'expected_version': 1, 'device_id': 'synthetic-combi-validation', 'records': records_for(meal)})
            imported.raise_for_status()
            assert imported.json()['meal']['version'] == 2
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            def update(sk, **fields):
                key = {'pk': pk, 'sk': sk}
                item = store.table.get_item(Key=key, ConsistentRead=True)['Item']
                store.table.put_item(Item={**item, **fields}, ConditionExpression='attribute_exists(pk)')
            update('META', status='MACKEREL_READY', last_observed_at=(start + timedelta(seconds=240)).isoformat())
            rice_key = 'JOB#RICE#' + meal['rice_cooking_job_id']
            transition_key = 'TRANSITION#' + meal['mode_transition_id']
            fish_key = 'JOB#MACKEREL#' + meal['mackerel_cooking_job_id']
            update(rice_key, status='UNLOADED', active_cooking_started_at=start.isoformat(),
                   program_completed_at=(start + timedelta(seconds=60)).isoformat(), program_cooking_duration=60,
                   time_status='NORMAL', safety_status='PASSED', safety_version=1,
                   monitored_process_assessment={'status': 'NORMAL', 'reason_codes': [], 'fan_observation': {'status': 'MATCHED'}},
                   monitored_process_samples=[{'second': 1}], quality_current={'version': 1, 'usable': True})
            update(transition_key, status='DRY_HEAT_READY', mackerel_load_allowed=True,
                   started_at=(start + timedelta(seconds=60)).isoformat(), samples=[{'second': 1}])
            visible = (await client.get(path)).json()
            assert visible['rice']['safety_status'] == 'INCOMPLETE' and not visible['rice']['quality_current']['usable']
            rice = await client.post(path + '/source-assessment/rice', headers=headers,
                                     json={'expected_version': 2, 'reassess_manual_rice': True})
            rice.raise_for_status()
            assert rice.json()['event']['previous_samples'] == [{'second': 1}]
            assert rice.json()['step']['safety_status'] == 'INCOMPLETE' and not rice.json()['step']['quality_current']['usable']
            blocked = await client.post(path + '/events', headers=headers, json={'expected_version': 3,
                'event': 'MACKEREL_STARTED', 'observed_at': (start + timedelta(seconds=241)).isoformat()})
            assert blocked.status_code == 409
            transition = await client.post(path + '/source-assessment/transition', headers=headers,
                json={'expected_version': 3, 'reassess_manual_transition': True})
            transition.raise_for_status()
            assert transition.json()['event']['previous_transition']['samples'] == [{'second': 1}]
            assert transition.json()['meal']['status'] == 'MACKEREL_READY'
            update('META', status='MACKEREL_SAFETY_PASSED')
            update(fish_key, status='UNLOADED', active_cooking_started_at=(start + timedelta(seconds=240)).isoformat(),
                   program_completed_at=(start + timedelta(seconds=301)).isoformat(), program_cooking_duration=61,
                   time_status='NORMAL', safety_status='PASSED', monitored_process_assessment={'status': 'NORMAL'},
                   monitored_process_samples=[{'second': 1}], quality_current={'version': 1, 'usable': True})
            assert not (await client.get(path)).json()['mackerel']['quality_current']['usable']
            fish = await client.post(path + '/source-assessment/mackerel', headers=headers,
                                     json={'expected_version': 4, 'reassess_manual_mackerel': True})
            fish.raise_for_status()
            assert fish.json()['event']['previous_samples'] == [{'second': 1}]
            assert not fish.json()['step']['quality_current']['usable']
            assert fish.json()['step']['monitored_process_source']['phase'] == 'MACKEREL'
            return {'rice': 'REASSESSED', 'transition': 'REASSESSED', 'mackerel': 'REASSESSED',
                    'prior_quality_restored': False}


async def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_COOKING_TABLE') != TABLE:
        raise SystemExit('Use the foodtech profile and development cooking table')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('Unexpected AWS account')
    store = DynamoCookingStore(TABLE)
    partitions = []
    try:
        result = await scenario(store, partitions)
    finally:
        remove_and_verify(store.table, partitions)
    print(json.dumps({**result, 'cleanup': 'verified'}))


if __name__ == '__main__':
    asyncio.run(main())
