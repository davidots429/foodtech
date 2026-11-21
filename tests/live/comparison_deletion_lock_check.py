"""Opt-in synthetic comparison lock and idempotent-repair check."""
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
    owner, result = 'synthetic-' + uuid4().hex, uuid4().hex
    baseline, candidate = (f'SCOPE#USER#{owner}#JOB#{name}' for name in ('baseline', 'candidate'))
    auth_key = {'pk': f'USER#{owner}'}
    dependency = 'COMPARISON#DEP#candidate#baseline'
    keys = [{'pk': pk, 'sk': sk} for pk in (baseline, candidate)
            for sk in ('META', 'ASSESSMENT#CURRENT', dependency)]
    keys += [{'pk': candidate, 'sk': 'COMPARISON#CURRENT#baseline'},
             {'pk': candidate, 'sk': f'COMPARISON#{result}'}]
    meta = {'version': 1, 'safety_version': 1, 'status': 'COMPLETED',
            'safety_status': 'PASSED', 'evaluation_blocked': False}
    pointer = {'version': 1, 'safety_version': 1, 'usable': True}
    record = {'comparison_id': result, 'baseline_job_id': 'baseline',
              'candidate_job_id': 'candidate', 'compared_at': '2026-11-21T00:00:00Z'}
    store = DynamoCookingStore(table.name, auth.name)
    created = False
    try:
        for pk in (baseline, candidate):
            for sk, value in (('META', meta), ('ASSESSMENT#CURRENT', pointer)):
                table.put_item(Item={'pk': pk, 'sk': sk, **value}, ConditionExpression='attribute_not_exists(pk)')
        auth.put_item(Item={**auth_key, 'epoch': 0, 'suspended': False, 'deletion_lock_request': str(uuid4())},
                      ConditionExpression='attribute_not_exists(pk)')
        created = True

        def blocked(epoch=None):
            try:
                store.save_comparison(baseline, candidate, meta, meta, pointer, pointer, record, epoch)
            except Conflict:
                return
            raise AssertionError('comparison was written or repaired despite guard')

        blocked()
        if any(table.get_item(Key=k, ConsistentRead=True).get('Item') for k in keys
               if k['sk'] not in ('META', 'ASSESSMENT#CURRENT')):
            raise AssertionError('locked comparison left records')
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        blocked(1)
        store.save_comparison(baseline, candidate, meta, meta, pointer, pointer, record, 0)
        store.save_comparison(baseline, candidate, meta, meta, pointer, pointer, record)
        auth.update_item(Key=auth_key, UpdateExpression='SET deletion_lock_request = :request',
                         ExpressionAttributeValues={':request': str(uuid4())})
        blocked()
    finally:
        for key in keys:
            table.delete_item(Key=key)
        if created:
            auth.delete_item(Key=auth_key)
    print('comparison lock, epoch, unlocked save, idempotent repair, relock and cleanup verified')


if __name__ == '__main__':
    main()
