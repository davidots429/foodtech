"""Synthetic guarded checkpoint round trip against existing development resources."""
import boto3
from uuid import uuid4

from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_business_api.deletion_progress import DeletionProgressConflict, DeletionProgressStore


def main():
    session = boto3.Session(region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise RuntimeError('unexpected AWS account')
    outputs = session.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']
    key_arn = next(x['OutputValue'] for x in outputs if x['OutputKey'] == 'CredentialStorageKeyArn')
    db = session.resource('dynamodb')
    auth = db.Table('foodtech-auth-sessions-dev')
    registry = db.Table('foodtech-ai-execution-registry-dev')
    progress = db.Table('foodtech-cooking-jobs-dev')
    subject, request_id = str(uuid4()), str(uuid4())
    auth_key = {'pk': f'USER#{subject}'}
    registry_key = {'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}
    progress_key = {'pk': f'REQ#{request_id}', 'sk': 'DOMAIN#COOKING'}
    store = DeletionProgressStore(auth_table=auth.name, registry_table=registry.name,
                                  progress_table=progress.name, client=session.client('dynamodb'))
    cipher = DeletionCheckpointCipher(key_arn, 'dev', kms_client=session.client('kms'))
    scope = dict(subject=subject, request_id=request_id, domain='COOKING')
    created_auth = created_registry = created_progress = False
    try:
        auth.put_item(Item={**auth_key, 'suspended': True, 'deletion_lock_request': request_id},
                      ConditionExpression='attribute_not_exists(pk)')
        created_auth = True
        registry.put_item(Item={**registry_key, 'status': 'DELETING'}, ConditionExpression='attribute_not_exists(pk)')
        created_registry = True
        assert store.load_checkpoint(cipher=cipher, **scope) is None
        assert store.save_checkpoint(cipher=cipher, **scope, expected_version=0, state='IN_PROGRESS',
                                     checkpoint=b'synthetic-cursor') == 1
        created_progress = True
        assert store.load_checkpoint(cipher=cipher, **scope) == {
            'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': b'synthetic-cursor'}
        try:
            store.save_checkpoint(cipher=cipher, **scope, expected_version=0, state='IN_PROGRESS',
                                  checkpoint=b'stale')
        except DeletionProgressConflict:
            pass
        else:
            raise AssertionError('stale write accepted')
        auth.update_item(Key=auth_key, UpdateExpression='SET deletion_lock_request = :other',
                         ExpressionAttributeValues={':other': str(uuid4())})
        try:
            store.load_checkpoint(cipher=cipher, **scope)
        except DeletionProgressConflict:
            pass
        else:
            raise AssertionError('changed lock accepted')
    finally:
        for table, key, created in ((progress, progress_key, created_progress),
                                    (registry, registry_key, created_registry), (auth, auth_key, created_auth)):
            if not created:
                continue
            table.delete_item(Key=key)
            if table.get_item(Key=key, ConsistentRead=True).get('Item') is not None:
                raise RuntimeError('synthetic cleanup failed')
    print('guarded KMS/DynamoDB checkpoint save, recovery, conflict and cleanup passed')


if __name__ == '__main__':
    main()
