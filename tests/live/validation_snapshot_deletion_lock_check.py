"""Opt-in synthetic validation snapshot deletion-lock check."""
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
    dynamo = session.resource('dynamodb')
    cooking = dynamo.Table('foodtech-cooking-jobs-dev')
    source = dynamo.Table('foodtech-data-flow-dev')
    auth = dynamo.Table('foodtech-auth-sessions-dev')
    subject, batch = 'synthetic-' + uuid4().hex, 'synthetic-' + uuid4().hex
    job_pk, stream_pk = f'SCOPE#USER#{subject}#JOB#job', f'JEYUK#{batch}'
    auth_key = {'pk': f'USER#{subject}'}
    job_key = {'pk': job_pk, 'sk': 'META'}
    profile_key = {'pk': f'SCOPE#USER#{subject}#CONDITION_PROFILE#p1', 'sk': 'VERSION#v1'}
    receipt = {'pk': stream_pk, 'sk': 'COLLECTION#RECEIPT#test', 'record_count': 1,
               'ordered_raw_sha256': 'a' * 64}
    source_keys = [{'pk': stream_pk, 'sk': 'SOURCE#REVISION'}, {'pk': stream_pk, 'sk': receipt['sk']}]
    profile = {'profile_id': 'p1', 'profile_version': 'v1', 'equipment_profile': 'jeyuk-small',
               'heating_setpoint_celsius': '200', 'target_rpm': '30'}
    snapshot = {'sk': f'VALIDATION#EVIDENCE#{uuid4()}', 'status': 'LINKED_UNVERIFIED', 'job_version': 1,
                'source_revision': 1, 'source_receipt_reference': receipt, 'condition_profile_reference': profile}
    snapshot_key = {'pk': job_pk, 'sk': snapshot['sk']}
    store = DynamoCookingStore(cooking.name, auth.name)
    auth_created = False
    try:
        cooking.put_item(Item={**job_key, 'version': 1}, ConditionExpression='attribute_not_exists(pk)')
        cooking.put_item(Item={**profile_key, **profile, 'provenance': 'SYNTHETIC', 'status': 'DECLARED_UNVERIFIED'},
                         ConditionExpression='attribute_not_exists(pk)')
        source.put_item(Item={**source_keys[0], 'revision': 1}, ConditionExpression='attribute_not_exists(pk)')
        source.put_item(Item={**receipt, 'receipt_version': 'synthetic-jeyuk-collection-v1', 'provenance': 'SYNTHETIC',
                              'profile_id': 'mockprofile-110', 'fixture_id': batch, 'starting_cursor': 0,
                              'ending_cursor': 1}, ConditionExpression='attribute_not_exists(pk)')
        auth.put_item(Item={**auth_key, 'epoch': 0, 'suspended': False, 'deletion_lock_request': str(uuid4())},
                      ConditionExpression='attribute_not_exists(pk)')
        auth_created = True

        def blocked(epoch):
            try:
                store.save_validation_evidence(job_pk, 1, snapshot, source.name, 1, receipt, epoch)
            except Conflict:
                return
            raise AssertionError('snapshot written despite guard')

        blocked(0)
        if cooking.get_item(Key=snapshot_key, ConsistentRead=True).get('Item'):
            raise AssertionError('locked snapshot persisted')
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        blocked(1)
        store.save_validation_evidence(job_pk, 1, snapshot, source.name, 1, receipt, 0)
        if not cooking.get_item(Key=snapshot_key, ConsistentRead=True).get('Item'):
            raise AssertionError('valid snapshot missing')
    finally:
        for key in (job_key, profile_key, snapshot_key):
            cooking.delete_item(Key=key)
        for key in source_keys:
            source.delete_item(Key=key)
        if auth_created:
            auth.delete_item(Key=auth_key)
    print('snapshot lock and epoch rejection, valid save and cleanup verified')


if __name__ == '__main__':
    main()
