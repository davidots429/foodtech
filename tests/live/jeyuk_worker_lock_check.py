"""Opt-in synthetic Jeyuk bridge deletion-lock check; no SQS messages."""
import os
from uuid import uuid4

import boto3
from foodtech_standardization_worker.jeyuk_job_bridge import DynamoJeyukJobBridge


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_AUTH_TABLE') != 'foodtech-auth-sessions-dev':
        raise SystemExit('explicit development profile and auth table required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    resource = session.resource('dynamodb')
    table = resource.Table('foodtech-cooking-jobs-dev')
    auth = resource.Table('foodtech-auth-sessions-dev')
    suffix = uuid4().hex
    owner, job_id, fixture = 'synthetic-' + suffix, 'job-' + suffix, 'jeyuk-' + suffix
    job_pk, stream_pk = f'SCOPE#USER#{owner}#JOB#{job_id}', f'JEYUK#{fixture}'
    auth_key = {'pk': f'USER#{owner}'}
    keys = [{'pk': job_pk, 'sk': 'META'}, {'pk': stream_pk, 'sk': 'BINDING'},
            {'pk': job_pk, 'sk': 'EVENT#JEYUK#000001'},
            {'pk': f'SCOPE#USER#{owner}#JOBS', 'sk': 'HISTORY#REVISION'}]
    meta = {**keys[0], 'owner_subject': owner, 'job_id': job_id, 'menu': 'JEYUK',
            'status': 'RUNNING', 'version': 1, 'job_state_version': 1,
            'evaluation_blocked': True, 'process_assessment_status': 'INSUFFICIENT_DATA',
            'source_binding': {'source_id': 'synthetic-rest', 'device_id': 'mockprofile-110-a-001', 'batch_id': fixture},
            'jeyuk_size_profile': 'SMALL', 'actual_load_kg': '1.8'}
    result = {'fixture_id': fixture, 'second': 1, 'profile': 'SMALL', 'actual_load_kg': '1.8',
              'process_status': 'NORMAL', 'reason_codes': [], 'surface_band': 'NORMAL',
              'rpm_band': 'NORMAL', 'load_status': 'NORMAL'}
    auth_created = False
    try:
        table.put_item(Item=meta, ConditionExpression='attribute_not_exists(pk)')
        table.put_item(Item={**keys[1], 'job_pk': job_pk}, ConditionExpression='attribute_not_exists(pk)')
        auth.put_item(Item={**auth_key, 'epoch': 1, 'suspended': False, 'deletion_lock_request': str(uuid4())},
                      ConditionExpression='attribute_not_exists(pk)')
        auth_created = True
        bridge = DynamoJeyukJobBridge(table.name)
        try:
            bridge.apply(result)
        except RuntimeError as exc:
            if 'deletion-locked' not in str(exc): raise
        else:
            raise AssertionError('locked owner accepted')
        if table.get_item(Key=keys[0], ConsistentRead=True)['Item']['version'] != 1 or \
                table.get_item(Key=keys[2], ConsistentRead=True).get('Item'):
            raise AssertionError('locked process wrote job or event')
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        if bridge.apply(result) != 'NORMAL' or table.get_item(Key=keys[2], ConsistentRead=True).get('Item') is None:
            raise AssertionError('unlocked process did not commit')
    finally:
        for key in keys:
            table.delete_item(Key=key)
        if auth_created:
            auth.delete_item(Key=auth_key)
    print('jeyuk worker deletion lock, retry path, unlocked commit and cleanup verified')


if __name__ == '__main__':
    main()
