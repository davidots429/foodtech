"""Opt-in synthetic meal cleanup using existing dev tables; no deployment.

Requires deletion progress modules from the dependent privacy branch. This
checks developer credentials, not a deployed worker role or deletion receipt.
"""
import os
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_business_api.deletion_progress import DeletionProgressStore
from foodtech_business_api.meal_deletion_coordinator import MealDeletionCoordinator
from foodtech_business_api.meal_deletion_inventory import MealDeletionInventory
from foodtech_business_api.meal_deletion_residual import MealFinalResidualScan, MealResidualScan
from foodtech_business_api.meal_deletion_worker import MealDeletionWorker
from foodtech_business_api.meal_meta_deletion import MealMetaDeletion
from foodtech_business_api.meal_source_snapshot import MealSourceSnapshot

if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_MEAL_DELETION_LIVE') != '1':
    raise SystemExit('explicit development profile and FOODTECH_MEAL_DELETION_LIVE=1 required')
session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
if session.client('sts').get_caller_identity()['Account'] != '573693340100':
    raise RuntimeError('unexpected account')
db = session.resource('dynamodb')
auth = db.Table('foodtech-auth-sessions-dev')
registry = db.Table('foodtech-ai-execution-registry-dev')
source = db.Table('foodtech-cooking-jobs-dev')
progress = db.Table('foodtech-ai-credential-vault-dev')
outputs = session.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']
key = next(item['OutputValue'] for item in outputs if item['OutputKey'] == 'CredentialStorageKeyArn')
subject, request, meal, other_subject = (str(uuid4()) for _ in range(4))
meal_pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{meal}'
other_pk = f'SCOPE#USER#{other_subject}#COMBI_MEAL#{meal}'
listing_pk = f'SCOPE#USER#{subject}#COMBI_MEALS'
entries = [
    (auth, {'pk': f'USER#{subject}', 'suspended': True, 'deletion_lock_request': request}),
    (registry, {'pk': 'REGISTRY', 'sk': f'REQ#{request}', 'status': 'DELETING'}),
    (source, {'pk': meal_pk, 'sk': 'META', 'meal_batch_id': meal, 'version': 1,
              'source': {'device_id': 'synthetic'}}),
    (source, {'pk': meal_pk, 'sk': 'EVENT#1'}),
    (source, {'pk': listing_pk, 'sk': f'MEAL#{meal}', 'meal_batch_id': meal}),
    (source, {'pk': other_pk, 'sk': 'EVENT#1'}),
]
created = []
try:
    for table, item in entries:
        table.put_item(Item=item, ConditionExpression='attribute_not_exists(pk)')
        created.append((table, {name: item[name] for name in ('pk', 'sk') if name in item}))
    store = DeletionProgressStore(auth_table=auth.name, registry_table=registry.name,
                                  progress_table=progress.name, client=session.client('dynamodb'))
    cipher = DeletionCheckpointCipher(key, 'dev', source_table_arn=source.table_arn, kms_client=session.client('kms'))
    inventory = MealDeletionInventory(source_table=source, progress_store=store, cipher=cipher)
    worker = MealDeletionWorker(inventory=inventory, source_table=source, progress_store=store, cipher=cipher)
    residual = MealResidualScan(source_table=source, progress_store=store, cipher=cipher)
    final = MealFinalResidualScan(source_table=source, progress_store=store, cipher=cipher)
    meta = MealMetaDeletion(inventory=inventory, residual=residual)
    coordinator = MealDeletionCoordinator(meals=inventory, worker=worker, residual=residual, metadata=meta,
                                          final_residual=final, sources=MealSourceSnapshot(inventory=inventory))
    for _ in range(1000):
        result = coordinator.step(subject=subject, request_id=request, limit=100)
        if result.get('source_ownership_required'):
            break
        if result.get('residual_found') or result['phase'] == 'VERIFYING':
            raise AssertionError('unexpected residual or unreviewed source')
    else:
        raise AssertionError('bounded meal cleanup did not reach source review')
    for sk in ('META', 'EVENT#1'):
        assert not source.get_item(Key={'pk': meal_pk, 'sk': sk}, ConsistentRead=True).get('Item')
    assert not source.get_item(Key={'pk': listing_pk, 'sk': f'MEAL#{meal}'}, ConsistentRead=True).get('Item')
    assert source.get_item(Key={'pk': other_pk, 'sk': 'EVENT#1'}, ConsistentRead=True).get('Item')
    snapshot = store.load_checkpoint(cipher=cipher, subject=subject, request_id=request,
                                     domain='MEAL_META_000000000000')
    assert b'source' in snapshot['checkpoint']
    print('synthetic meal cleanup, source review stop and other customer isolation verified')
finally:
    cursor = None
    while True:
        kwargs = {'KeyConditionExpression': Key('pk').eq(f'REQ#{request}'), 'ConsistentRead': True}
        if cursor:
            kwargs['ExclusiveStartKey'] = cursor
        page = progress.query(**kwargs)
        for item in page['Items']:
            progress.delete_item(Key={'pk': item['pk'], 'sk': item['sk']})
        cursor = page.get('LastEvaluatedKey')
        if not cursor:
            break
    assert not progress.query(KeyConditionExpression=Key('pk').eq(f'REQ#{request}'), ConsistentRead=True)['Items']
    for table, item_key in reversed(created):
        table.delete_item(Key=item_key)
    assert all(not table.get_item(Key=item_key, ConsistentRead=True).get('Item') for table, item_key in created)
    print('synthetic cleanup verified')
