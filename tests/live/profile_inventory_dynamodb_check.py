"""Opt-in isolated profile inventory transaction check; synthetic data only."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore
from foodtech_business_api.legacy_profile_scan import reconcile_legacy_page
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict, delete_profile_pair


def main():
    table_name = 'foodtech-cooking-jobs-dev'
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_COOKING_TABLE') != table_name:
        raise SystemExit('explicit development profile and cooking table required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    table = session.resource('dynamodb').Table(table_name)
    auth_table = session.resource('dynamodb').Table('foodtech-auth-sessions-dev')
    store = DynamoCookingStore.__new__(DynamoCookingStore)
    store.table, store.client, store.serializer = table, session.client('dynamodb'), TypeSerializer()
    store.auth_table_name = 'foodtech-auth-sessions-dev'
    subject = 'synthetic-' + uuid4().hex
    owner = f'SCOPE#USER#{subject}'
    keys = [{'pk': f'{owner}#CONDITION_PROFILE#p1', 'sk': f'VERSION#{v}'} for v in ('v1', 'v2')]
    keys += [{'pk': f'{owner}#CONDITION_PROFILES', 'sk': f'PROFILE#p1#VERSION#{v}'} for v in ('v1', 'v2')]
    legacy = {'pk': f'{owner}#CONDITION_PROFILE#legacy', 'sk': 'VERSION#v1'}
    legacy_ref = {'pk': f'{owner}#CONDITION_PROFILES', 'sk': 'PROFILE#legacy#VERSION#v1'}
    keys += [legacy, legacy_ref]
    concurrent = {'pk': f'{owner}#CONDITION_PROFILE#race', 'sk': 'VERSION#v1'}
    concurrent_ref = {'pk': f'{owner}#CONDITION_PROFILES', 'sk': 'PROFILE#race#VERSION#v1'}
    keys += [concurrent, concurrent_ref]
    locked_key = {'pk': f'{owner}#CONDITION_PROFILE#blocked', 'sk': 'VERSION#v1'}
    locked_ref = {'pk': f'{owner}#CONDITION_PROFILES', 'sk': 'PROFILE#blocked#VERSION#v1'}
    auth_key = {'pk': f'USER#{subject}'}
    job_pk = f'{owner}#JOB#synthetic-job'
    job_index = f'{owner}#JOBS'
    timestamp = '2026-11-21T00:00:00Z'
    job_meta = {'owner_subject': subject, 'job_id': 'synthetic-job', 'created_at': timestamp,
                'recipe_version': 'synthetic-v1', 'planned_portions': 2, 'version': 1}
    job_keys = [{'pk': job_pk, 'sk': 'META'},
                {'pk': job_index, 'sk': f'CREATED#{timestamp}#synthetic-job'},
                {'pk': job_index, 'sk': 'HISTORY#REVISION'}]
    recook_pk = f'{owner}#JOB#synthetic-recook'
    recook_meta = {**job_meta, 'job_id': 'synthetic-recook', 'source_job_id': 'synthetic-job'}
    recook_keys = [{'pk': recook_pk, 'sk': 'META'},
                   {'pk': job_index, 'sk': f'CREATED#{timestamp}#synthetic-recook'}]
    assessment = {'sk': 'ASSESSMENT#REV#000000000001', 'version': 1}
    pointer = {'version': 1, 'usable': True}
    event = {'sk': 'EVENT#SYNTHETIC#1', 'trigger': 'SAFETY'}
    commit_keys = [{'pk': job_pk, 'sk': assessment['sk']}, {'pk': job_pk, 'sk': 'ASSESSMENT#CURRENT'},
                   {'pk': job_pk, 'sk': event['sk']}]
    stream_pk = 'SYNTHETIC_STREAM#' + uuid4().hex
    stream_key = {'pk': stream_pk, 'sk': 'BINDING'}
    auth_created = False
    try:
        for version in ('v1', 'v2'):
            store.create_condition_profile(subject, {'profile_id': 'p1', 'profile_version': version})
        try:
            store.create_condition_profile(subject, {'profile_id': 'p1', 'profile_version': 'v1'})
        except Conflict:
            pass
        else:
            raise AssertionError('duplicate profile was accepted')
        page = store.list_condition_profile_refs(subject, 1, None)
        next_page = store.list_condition_profile_refs(subject, 1, page['next_cursor'])
        if [x['profile_version'] for x in page['items'] + next_page['items']] != ['v1', 'v2']:
            raise AssertionError('inventory pages mismatch')
        if store.list_condition_profile_refs(subject + '-other', 1, None)['items']:
            raise AssertionError('cross-owner inventory leak')
        def create_racing_profile(_):
            try:
                store.create_condition_profile(subject, {'profile_id': 'race', 'profile_version': 'v1'})
                return 'created'
            except Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as executor:
            if sorted(executor.map(create_racing_profile, range(2))) != ['conflict', 'created']:
                raise AssertionError('concurrent writes did not select exactly one winner')
        auth_table.put_item(Item={**auth_key, 'epoch': 0, 'suspended': False, 'deletion_lock_request': str(uuid4())},
                            ConditionExpression='attribute_not_exists(pk)')
        auth_created = True
        for state in ('locked', 'suspended', 'stale-epoch'):
            if state == 'suspended':
                auth_table.update_item(Key=auth_key, UpdateExpression='SET suspended = :yes REMOVE deletion_lock_request',
                                       ExpressionAttributeValues={':yes': True})
            elif state == 'stale-epoch':
                auth_table.update_item(Key=auth_key, UpdateExpression='SET suspended = :no, #epoch = :epoch',
                                       ExpressionAttributeNames={'#epoch': 'epoch'},
                                       ExpressionAttributeValues={':no': False, ':epoch': 1})
            try:
                store.create_condition_profile(subject, {'profile_id': 'blocked', 'profile_version': 'v1'}, 0)
            except Conflict:
                pass
            else:
                raise AssertionError(f'profile write accepted for {state}')
            try:
                store.create(job_pk, job_meta, 0)
            except Conflict:
                pass
            else:
                raise AssertionError(f'job write accepted for {state}')
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') for k in (locked_key, locked_ref)):
            raise AssertionError('blocked write left a profile or reference')
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') for k in job_keys):
            raise AssertionError('blocked write left a job, listing or history revision')
        store.create(job_pk, job_meta, 1)
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') is None for k in job_keys):
            raise AssertionError('valid session did not create complete job history')
        auth_table.update_item(Key=auth_key, UpdateExpression='SET deletion_lock_request = :request',
                               ExpressionAttributeValues={':request': str(uuid4())})
        table.put_item(Item={**legacy, 'profile_id': 'legacy', 'profile_version': 'v1'},
                       ConditionExpression='attribute_not_exists(pk)')
        try:
            store.ensure_condition_profile_listed(subject, 'legacy', 'v1')
        except Conflict:
            pass
        else:
            raise AssertionError('legacy reference accepted after deletion lock')
        if table.get_item(Key=legacy_ref, ConsistentRead=True).get('Item'):
            raise AssertionError('blocked reconciliation created a reference')
        try:
            store.bind_stream(job_pk, 1, {**job_meta, 'version': 2}, stream_pk, 1)
        except Conflict:
            pass
        else:
            raise AssertionError('source binding accepted after deletion lock')
        if table.get_item(Key=stream_key, ConsistentRead=True).get('Item') or \
                table.get_item(Key=job_keys[0], ConsistentRead=True)['Item']['version'] != 1:
            raise AssertionError('blocked binding changed job or created a stream reference')
        try:
            store.commit(job_pk, 1, {**job_meta, 'version': 2}, assessment, pointer, event, 1)
        except Conflict:
            pass
        else:
            raise AssertionError('job mutation accepted after deletion lock')
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') for k in commit_keys) or \
                table.get_item(Key=job_keys[0], ConsistentRead=True)['Item']['version'] != 1:
            raise AssertionError('blocked job mutation changed job or assessment')
        try:
            store.create_recook(job_pk, 1, recook_pk, recook_meta, 1)
        except Conflict:
            pass
        else:
            raise AssertionError('recook accepted after deletion lock')
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') for k in recook_keys) or \
                table.get_item(Key=job_keys[2], ConsistentRead=True)['Item']['revision'] != 1:
            raise AssertionError('blocked recook changed data or history')
        auth_table.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        store.create_recook(job_pk, 1, recook_pk, recook_meta, 1)
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') is None for k in recook_keys):
            raise AssertionError('valid recook was not created')
        store.commit(job_pk, 1, {**job_meta, 'version': 2}, assessment, pointer, event, 1)
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') is None for k in commit_keys):
            raise AssertionError('valid job mutation did not save its assessment')
        store.bind_stream(job_pk, 2, {**job_meta, 'version': 3}, stream_pk, 1)
        if table.get_item(Key=stream_key, ConsistentRead=True).get('Item') is None:
            raise AssertionError('valid source binding was not saved')
        checkpoint, found = None, False
        for _ in range(20):
            result = reconcile_legacy_page(table, store, subject=subject, limit=100, checkpoint=checkpoint)
            found |= ('legacy', 'v1') in result.references
            checkpoint = result.checkpoint
            if checkpoint is None:
                break
        if not found:
            raise AssertionError('legacy profile not found within the bounded scan')
        store.ensure_condition_profile_listed(subject, 'legacy', 'v1')
        try:
            store.ensure_condition_profile_listed(subject, 'missing', 'v1')
        except Conflict:
            pass
        else:
            raise AssertionError('missing legacy profile was listed')
        if any(table.get_item(Key=key, ConsistentRead=True).get('Item') is None for key in keys):
            raise AssertionError('missing profile or reference')
        request = str(uuid4())
        auth_table.update_item(Key=auth_key, UpdateExpression='SET deletion_lock_request = :request, suspended = :yes',
                               ExpressionAttributeValues={':request': request, ':yes': True})
        def remove_profile(request_id, version):
            delete_profile_pair(table, store.client, auth_table=auth_table.name, subject=subject,
                                request_id=request_id, profile_id='p1', version=version)
        try:
            remove_profile(str(uuid4()), 'v1')
        except ProfileDeletionConflict:
            pass
        else:
            raise AssertionError('wrong deletion request accepted')
        table.update_item(Key=keys[2], UpdateExpression='SET profile_version = :wrong',
                          ExpressionAttributeValues={':wrong': 'wrong'})
        try:
            remove_profile(request, 'v1')
        except ProfileDeletionConflict:
            pass
        else:
            raise AssertionError('changed profile reference accepted')
        if table.get_item(Key=keys[0], ConsistentRead=True).get('Item') is None:
            raise AssertionError('failed pair deletion removed source')
        table.update_item(Key=keys[2], UpdateExpression='SET profile_version = :right',
                          ExpressionAttributeValues={':right': 'v1'})
        remove_profile(request, 'v1')
        remove_profile(request, 'v1')
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') for k in (keys[0], keys[2])) or \
                any(not table.get_item(Key=k, ConsistentRead=True).get('Item') for k in (keys[1], keys[3])):
            raise AssertionError('profile pair deletion crossed version boundary')
    finally:
        for key in keys:
            table.delete_item(Key=key)
        for key in job_keys:
            table.delete_item(Key=key)
        for key in recook_keys:
            table.delete_item(Key=key)
        for key in commit_keys:
            table.delete_item(Key=key)
        table.delete_item(Key=stream_key)
        if auth_created:
            auth_table.delete_item(Key=auth_key)
    print('profile and job guards, profile pair deletion, owner isolation and cleanup verified')


if __name__ == '__main__':
    main()
