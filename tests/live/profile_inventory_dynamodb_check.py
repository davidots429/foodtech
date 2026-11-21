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
    store = DynamoCookingStore.__new__(DynamoCookingStore)
    store.table, store.client, store.serializer = table, session.client('dynamodb'), TypeSerializer()
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
    print('profile transaction, duplicate and concurrent write rejection, owner isolation, pagination, bounded scan, legacy reconciliation and cleanup verified')


if __name__ == '__main__':
    main()
