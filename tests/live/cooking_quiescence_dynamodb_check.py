"""Check the unproven-writer fence against isolated synthetic dev records."""
import os
from types import SimpleNamespace
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Attr

from foodtech_business_api.cooking_deletion_recovery import CookingDeletionRecovery
from foodtech_business_api.deletion_progress import DeletionProgressConflict, DeletionProgressStore
from process_flow_dynamodb_check import ACCOUNT, REGION


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_QUIESCENCE_LIVE') != '1':
        raise SystemExit('explicit development profile and opt-in required')
    session = boto3.Session(profile_name='foodtech', region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise SystemExit('unexpected AWS account')
    dynamo = session.resource('dynamodb')
    auth = dynamo.Table('foodtech-auth-sessions-dev')
    registry = dynamo.Table('foodtech-ai-execution-registry-dev')
    progress = dynamo.Table('foodtech-ai-credential-vault-dev')
    source = dynamo.Table('foodtech-cooking-jobs-dev')
    subject, other, request = str(uuid4()), str(uuid4()), str(uuid4())
    auth_key = {'pk': f'USER#{subject}'}
    registry_key = {'pk': 'REGISTRY', 'sk': f'REQ#{request}'}
    sentinel_key = {'pk': f'SCOPE#USER#{subject}#JOB#{uuid4()}', 'sk': 'META'}
    created = []
    try:
        for table, item in ((auth, {**auth_key, 'suspended': True, 'deletion_lock_request': request}),
                            (registry, {**registry_key, 'status': 'DELETING'}),
                            (source, {**sentinel_key, 'provenance': 'SYNTHETIC'})):
            table.put_item(Item=item, ConditionExpression=Attr('pk').not_exists())
            created.append((table, {key: item[key] for key in ('pk', 'sk') if key in item}))
        store = DeletionProgressStore(auth_table=auth.name, registry_table=registry.name,
                                      progress_table=progress.name, client=session.client('dynamodb'))
        cipher = object()
        def forbidden(**_):
            raise AssertionError('cleanup ran without writer fence')
        def dependency():
            return SimpleNamespace(coordinator=SimpleNamespace(store=store, cipher=cipher),
                recovery=SimpleNamespace(store=store), step=forbidden)
        service = CookingDeletionRecovery(profile=dependency(), job=dependency(), meal=dependency(),
                                           aggregate=dependency(), progress_store=store, cipher=cipher)
        if service.step(authenticated_subject=subject, request_id=request) != \
                {'phase': 'QUIESCENCE', 'writer_quiescence_required': True}:
            raise AssertionError('missing writer fence was accepted')
        if not source.get_item(Key=sentinel_key, ConsistentRead=True).get('Item'):
            raise AssertionError('synthetic source changed')
        try:
            service.step(authenticated_subject=other, request_id=request)
        except DeletionProgressConflict:
            pass
        else:
            raise AssertionError('different owner was accepted')
        if progress.get_item(Key={'pk': f'REQ#{request}', 'sk': 'DOMAIN#COOKING_RECOVERY'},
                             ConsistentRead=True).get('Item'):
            raise AssertionError('blocked cleanup wrote progress')
        print('synthetic lock, owner and writer-fence gate verified')
    finally:
        progress_key = {'pk': f'REQ#{request}', 'sk': 'DOMAIN#COOKING_RECOVERY'}
        progress.delete_item(Key=progress_key)
        for table, key in reversed(created):
            table.delete_item(Key=key)
        if progress.get_item(Key=progress_key, ConsistentRead=True).get('Item') or any(
                table.get_item(Key=key, ConsistentRead=True).get('Item') for table, key in created):
            raise RuntimeError('synthetic cleanup incomplete')
        print('synthetic cleanup verified')


if __name__ == '__main__':
    main()
