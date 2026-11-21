"""Opt-in synthetic encrypted scan progress check against development AWS."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.profile_checkpoint_cipher import ProfileCheckpointCipher
from foodtech_business_api.profile_scan_progress import ProfileScanProgressStore


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    table = session.resource('dynamodb').Table('foodtech-cooking-jobs-dev')
    kms = session.client('kms')
    key_arn = kms.describe_key(KeyId='alias/foodtech/ai-credential-dev-2026-11')['KeyMetadata']['Arn']
    cipher = ProfileCheckpointCipher(key_arn, 'dev', kms_client=kms)
    progress = ProfileScanProgressStore(table, cipher)
    profiles = DynamoCookingStore.__new__(DynamoCookingStore)
    profiles.table, profiles.client = table, session.client('dynamodb')
    profiles.serializer = TypeSerializer()
    job, subject = str(uuid4()), 'synthetic-owner-' + uuid4().hex
    key = {'pk': f'PROFILE_SCAN#{job}', 'sk': 'META'}
    race_job = str(uuid4())
    race_key = {'pk': f'PROFILE_SCAN#{race_job}', 'sk': 'META'}
    try:
        progress.start(job, subject)
        try:
            progress.run_page(job_id=job, subject='different-owner', source_table=table, profile_store=profiles, limit=1)
        except ValueError:
            pass
        else:
            raise AssertionError('wrong owner accepted')
        result = progress.run_page(job_id=job, subject=subject, source_table=table, profile_store=profiles, limit=1)
        item = table.get_item(Key=key, ConsistentRead=True)['Item']
        if result.version != 2 or item['version'] != 2 or result.phase not in ('CONTINUE', 'SCAN_COMPLETE'):
            raise AssertionError('scan checkpoint did not advance')
        if progress.read_progress(job, subject) != result:
            raise AssertionError('persisted progress could not be recovered')
        if subject.encode() in bytes(item['ciphertext']):
            raise AssertionError('plaintext scope in encrypted checkpoint')
        progress.start(race_job, subject)
        barrier = Barrier(2, timeout=30)
        def run_competitor(_):
            worker_session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
            worker_table = worker_session.resource('dynamodb').Table('foodtech-cooking-jobs-dev')
            worker_profiles = DynamoCookingStore.__new__(DynamoCookingStore)
            worker_profiles.table, worker_profiles.client = worker_table, worker_session.client('dynamodb')
            worker_profiles.serializer = TypeSerializer()
            worker_cipher = ProfileCheckpointCipher(key_arn, 'dev', kms_client=worker_session.client('kms'))
            worker = ProfileScanProgressStore(worker_table, worker_cipher)
            class SynchronizedScan:
                def scan(self, **kwargs):
                    barrier.wait()
                    return worker_table.scan(**kwargs)
            try:
                worker.run_page(job_id=race_job, subject=subject, source_table=SynchronizedScan(),
                                profile_store=worker_profiles, limit=1)
                return 'advanced'
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
                    raise
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as executor:
            if sorted(executor.map(run_competitor, range(2))) != ['advanced', 'conflict']:
                raise AssertionError('concurrent checkpoint update did not select one winner')
        if progress.read_progress(race_job, subject).version != 2:
            raise AssertionError('unexpected race checkpoint version')
    finally:
        table.delete_item(Key=key)
        table.delete_item(Key=race_key)
    print('encrypted progress persistence, owner binding, scan advance, concurrent update and cleanup verified')


if __name__ == '__main__':
    main()
