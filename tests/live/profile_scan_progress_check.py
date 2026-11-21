"""Opt-in synthetic encrypted scan progress check against development AWS."""
import os
from uuid import uuid4

import boto3
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
    from boto3.dynamodb.types import TypeSerializer
    profiles.serializer = TypeSerializer()
    job, subject = str(uuid4()), 'synthetic-owner-' + uuid4().hex
    key = {'pk': f'PROFILE_SCAN#{job}', 'sk': 'META'}
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
        if subject.encode() in bytes(item['ciphertext']):
            raise AssertionError('plaintext scope in encrypted checkpoint')
    finally:
        table.delete_item(Key=key)
    print('encrypted progress persistence, owner binding, scan advance and cleanup verified')


if __name__ == '__main__':
    main()
