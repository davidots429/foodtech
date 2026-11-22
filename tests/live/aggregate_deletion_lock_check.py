"""Opt-in synthetic aggregate lock and idempotent-repair check."""
import os
from uuid import uuid4

import boto3
from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    table = session.resource('dynamodb').Table('foodtech-cooking-jobs-dev')
    auth = session.resource('dynamodb').Table('foodtech-auth-sessions-dev')
    owner, group, result = 'synthetic-' + uuid4().hex, 'a' * 64, 'b' * 64
    job_pk, group_pk = f'SCOPE#USER#{owner}#JOB#job', f'SCOPE#USER#{owner}#AGGREGATE#{group}'
    auth_key = {'pk': f'USER#{owner}'}
    keys = [{'pk': job_pk, 'sk': 'META'}, {'pk': job_pk, 'sk': f'AGGREGATE#DEP#{group}'},
            {'pk': group_pk, 'sk': 'CURRENT'}, {'pk': group_pk, 'sk': f'RESULT#{result}'}]
    meta = {'version': 1, 'status': 'COMPLETED', 'safety_status': 'PASSED', 'evaluation_blocked': False}
    record = {'group_id': group, 'result_id': result, 'selection': {}, 'created_at': '2026-11-21T00:00:00Z'}
    store = DynamoCookingStore(table.name, auth.name)
    created = False
    try:
        table.put_item(Item={**keys[0], **meta}, ConditionExpression='attribute_not_exists(pk)')
        auth.put_item(Item={**auth_key, 'epoch': 0, 'suspended': False, 'deletion_lock_request': str(uuid4())},
                      ConditionExpression='attribute_not_exists(pk)')
        created = True
        def attempt_blocked():
            try:
                store.save_aggregate(group_pk, [(job_pk, meta, None, None)], record)
            except Conflict:
                return
            raise AssertionError('locked aggregate was written or repaired')
        attempt_blocked()
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') for k in keys[1:]):
            raise AssertionError('locked aggregate left records')
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        store.save_aggregate(group_pk, [(job_pk, meta, None, None)], record)
        store.save_aggregate(group_pk, [(job_pk, meta, None, None)], record)
        auth.update_item(Key=auth_key, UpdateExpression='SET deletion_lock_request = :request',
                         ExpressionAttributeValues={':request': str(uuid4())})
        attempt_blocked()
    finally:
        for key in keys: table.delete_item(Key=key)
        if created: auth.delete_item(Key=auth_key)
    print('aggregate lock, unlocked save, idempotent repair, relock rejection and cleanup verified')


if __name__ == '__main__':
    main()
