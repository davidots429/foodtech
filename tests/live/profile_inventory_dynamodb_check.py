"""Opt-in isolated profile inventory transaction check; synthetic data only."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore
from foodtech_business_api.legacy_profile_scan import reconcile_legacy_page


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
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') for k in (locked_key, locked_ref)):
            raise AssertionError('blocked write left a profile or reference')
        table.put_item(Item={**legacy, 'profile_id': 'legacy', 'profile_version': 'v1'},
                       ConditionExpression='attribute_not_exists(pk)')
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
    finally:
        for key in keys:
            table.delete_item(Key=key)
        if auth_created:
            auth_table.delete_item(Key=auth_key)
    print('profile transaction, concurrent write and deletion lock rejection, owner isolation, pagination, bounded scan, legacy reconciliation and cleanup verified')


if __name__ == '__main__':
    main()
