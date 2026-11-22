"""Opt-in synthetic fryer bridge lock check using existing development tables."""
import os
from datetime import datetime, timezone
from uuid import uuid4

import boto3
from foodtech_contracts import stream_binding_key
from foodtech_standardization_worker.process_bridge import DynamoProcessBridge


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_AUTH_TABLE') != 'foodtech-auth-sessions-dev':
        raise SystemExit('explicit development profile and auth table required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    resource = session.resource('dynamodb')
    table, auth = resource.Table('foodtech-cooking-jobs-dev'), resource.Table('foodtech-auth-sessions-dev')
    suffix = uuid4().hex
    owner, job_id = 'synthetic-' + suffix, 'job-' + suffix
    job_pk, auth_key = f'SCOPE#USER#{owner}#JOB#{job_id}', {'pk': f'USER#{owner}'}
    binding = {'source_id': 'synthetic-rest', 'device_id': 'mockprofile-100-a-001', 'batch_id': 'batch-a-' + suffix}
    stream_pk = stream_binding_key(**binding)
    keys = [{'pk': job_pk, 'sk': 'META'}, {'pk': stream_pk, 'sk': 'BINDING'},
            {'pk': job_pk, 'sk': 'EVENT#PROCESS#sample'}, {'pk': job_pk, 'sk': 'RESTATEMENT#REQUEST#late'},
            {'pk': f'SCOPE#USER#{owner}#JOBS', 'sk': 'HISTORY#REVISION'}]
    job = {**keys[0], 'owner_subject': owner, 'job_id': job_id, 'status': 'RUNNING', 'version': 1,
           'source_binding': binding, 'process_thickness_status': 'IN_RANGE', 'process_time_status': 'NORMAL',
           'process_assessment_status': 'INSUFFICIENT_DATA'}
    normalized = {**binding, 'sequence_number': 1, 'device_status': 'RUNNING',
                  'event_time': datetime.now(timezone.utc).isoformat(),
                  'CHAMBER_TEMPERATURE': {'value': '170', 'unit': '°C'}}
    sample = {'pk': 'EVENT#sample', 'ingestion_event_id': 'sample', 'dedup_status': 'UNIQUE',
              'quality_status': 'VALID', 'finding_codes': [], 'normalized': normalized}
    late = {**sample, 'pk': 'EVENT#late', 'ingestion_event_id': 'late', 'finding_codes': ['LATE_AFTER_GAP']}
    auth_created = False
    try:
        table.put_item(Item=job, ConditionExpression='attribute_not_exists(pk)')
        table.put_item(Item={**keys[1], 'job_pk': job_pk}, ConditionExpression='attribute_not_exists(pk)')
        auth.put_item(Item={**auth_key, 'epoch': 1, 'suspended': False, 'deletion_lock_request': str(uuid4())},
                      ConditionExpression='attribute_not_exists(pk)')
        auth_created = True
        bridge = DynamoProcessBridge(table.name)
        for item in (sample, late):
            try:
                bridge.apply(item)
            except RuntimeError as exc:
                if 'deletion-locked' not in str(exc): raise
            else:
                raise AssertionError('locked owner accepted')
        if table.get_item(Key=keys[0], ConsistentRead=True)['Item']['version'] != 1 or any(
                table.get_item(Key=key, ConsistentRead=True).get('Item') for key in keys[2:]):
            raise AssertionError('locked worker wrote job, event, request or history')
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        if bridge.apply(sample) not in ('NORMAL', 'INSUFFICIENT_DATA', 'WARNING'):
            raise AssertionError('unlocked sample was not applied')
        if bridge.apply(late) != 'RESTATEMENT_PENDING':
            raise AssertionError('unlocked late event did not create request')
    finally:
        for key in keys:
            table.delete_item(Key=key)
        if auth_created:
            auth.delete_item(Key=auth_key)
    print('fryer current and late-request locks, unlocked writes and cleanup verified')


if __name__ == '__main__':
    main()
