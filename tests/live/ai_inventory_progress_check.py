"""Opt-in synthetic AI inventory checkpoint test against development AWS."""
import os
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from foodtech_ai_gateway.inventory_checkpoint_cipher import InventoryCheckpointCipher
from foodtech_ai_gateway.inventory_scan_progress import InventoryScanProgressStore
from foodtech_ai_gateway.legacy_tenant_inventory import ActiveReconciliationConflict


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    db = session.resource('dynamodb')
    source = db.Table('foodtech-ai-execution-registry-dev')
    progress_table = db.Table('foodtech-cooking-jobs-dev')
    auth = db.Table('foodtech-auth-sessions-dev')
    deletion_registry = db.Table('foodtech-ai-credential-vault-dev')
    kms, client = session.client('kms'), session.client('dynamodb')
    key_arn = kms.describe_key(KeyId='alias/foodtech/ai-credential-dev-2026-11')['KeyMetadata']['Arn']
    cipher = InventoryCheckpointCipher(key_arn, 'dev', kms_client=kms)
    progress = InventoryScanProgressStore(progress_table, client, cipher, auth_table=auth.name, deletion_registry_table=deletion_registry.name)
    subject, job, request = 'synthetic-' + uuid4().hex, str(uuid4()), str(uuid4())
    auth_key, progress_key = {'pk': f'USER#{subject}'}, {'pk': f'AI_SCAN#{job}', 'sk': 'META'}
    auth_created = False
    progress_created = False
    deletion_created = False
    deletion_key = {'pk':'REGISTRY','sk':f'REQ#{request}'}
    try:
        auth.put_item(Item={**auth_key, 'deletion_lock_request': request, 'suspended':True}, ConditionExpression='attribute_not_exists(pk)')
        auth_created = True
        deletion_registry.put_item(Item={**deletion_key,'status':'DELETING'},ConditionExpression='attribute_not_exists(pk)')
        deletion_created = True
        progress.start(job, subject, request, source.table_arn)
        progress_created = True
        result = progress.run_page(job_id=job, subject=subject, request_id=request, source_table=source, limit=1)
        if result.version != 2 or progress.read_progress(job, subject, request, source.table_arn) != result:
            raise AssertionError('encrypted progress did not advance')
        saved = progress_table.get_item(Key=progress_key, ConsistentRead=True)['Item']
        if subject.encode() in bytes(saved['ciphertext']):
            raise AssertionError('scope leaked in ciphertext')
        try:
            progress.read_progress(job, subject, str(uuid4()), source.table_arn)
        except ValueError:
            pass
        else:
            raise AssertionError('different deletion request opened progress')
        if result.phase == 'CONTINUE':
            deletion_registry.update_item(Key=deletion_key,UpdateExpression='SET #s=:s',ExpressionAttributeNames={'#s':'status'},ExpressionAttributeValues={':s':'PAUSED'})
            try:
                progress.run_page(job_id=job,subject=subject,request_id=request,source_table=source,limit=1)
            except (ClientError,ActiveReconciliationConflict):pass
            else:raise AssertionError('scan advanced without active deletion request')
            deletion_registry.update_item(Key=deletion_key,UpdateExpression='SET #s=:s',ExpressionAttributeNames={'#s':'status'},ExpressionAttributeValues={':s':'DELETING'})
            auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
            try:
                progress.run_page(job_id=job, subject=subject, request_id=request, source_table=source, limit=1)
            except (ClientError, ActiveReconciliationConflict):
                pass
            else:
                raise AssertionError('scan advanced without deletion lock')
            if progress.read_progress(job, subject, request, source.table_arn).version != 2:
                raise AssertionError('failed lock check advanced progress')
    finally:
        if progress_created:progress_table.delete_item(Key=progress_key)
        if deletion_created:deletion_registry.delete_item(Key=deletion_key)
        if auth_created:auth.delete_item(Key=auth_key)
        if progress_created and progress_table.get_item(Key=progress_key,ConsistentRead=True).get('Item'):raise RuntimeError('progress cleanup failed')
        if deletion_created and deletion_registry.get_item(Key=deletion_key,ConsistentRead=True).get('Item'):raise RuntimeError('registry cleanup failed')
        if auth_created and auth.get_item(Key=auth_key,ConsistentRead=True).get('Item'):raise RuntimeError('auth cleanup failed')
    print('encrypted AI progress, request binding, lock rejection and cleanup verified')


if __name__ == '__main__': main()
