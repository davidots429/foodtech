"""Opt-in synthetic primitive check, not worker authorization or writer-fencing proof."""
import os
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_business_api.deletion_progress import DeletionProgressConflict, DeletionProgressStore
from foodtech_business_api.meal_aggregate_deletion_inventory import MealAggregateDeletionInventory
from foodtech_business_api.meal_aggregate_deletion_residual import MealAggregateResidualScan
from foodtech_business_api.meal_aggregate_deletion_worker import MealAggregateDeletionWorker
from foodtech_business_api.meal_aggregate_group_deletion import clean_one_meal_aggregate_group
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_MEAL_AGGREGATE_DELETE_LIVE') != '1':
        raise SystemExit('explicit development profile and FOODTECH_MEAL_AGGREGATE_DELETE_LIVE=1 required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected account')
    db = session.resource('dynamodb')
    auth, registry = db.Table('foodtech-auth-sessions-dev'), db.Table('foodtech-ai-execution-registry-dev')
    source, progress = db.Table('foodtech-cooking-jobs-dev'), db.Table('foodtech-ai-credential-vault-dev')
    outputs = session.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']
    key = next(item['OutputValue'] for item in outputs if item['OutputKey'] == 'CredentialStorageKeyArn')
    subject, request, other = str(uuid4()), str(uuid4()), str(uuid4())
    group, first, second = (uuid4().hex + uuid4().hex for _ in range(3))
    group_pk = f'SCOPE#USER#{subject}#MEAL_AGGREGATE#{group}'
    index_pk = f'SCOPE#USER#{subject}#MEAL_AGGREGATE_GROUPS'
    other_pk = f'SCOPE#USER#{other}#MEAL_AGGREGATE#{group}'
    reg_key = {'pk': 'REGISTRY', 'sk': f'REQ#{request}'}
    entries = [(auth, {'pk': f'USER#{subject}', 'suspended': True, 'deletion_lock_request': request}),
               (registry, {**reg_key, 'status': 'DELETING'}),
               (source, {'pk': group_pk, 'sk': f'RESULT#{first}', 'group_id': group, 'result_id': first}),
               (source, {'pk': group_pk, 'sk': f'RESULT#{second}', 'group_id': group, 'result_id': second}),
               (source, {'pk': index_pk, 'sk': f'GROUP#{group}', 'group_id': group}),
               (source, {'pk': other_pk, 'sk': f'RESULT#{first}', 'group_id': group, 'result_id': first})]
    created = []
    try:
        for table, item in entries:
            table.put_item(Item=item, ConditionExpression='attribute_not_exists(pk)')
            created.append((table, {name: item[name] for name in ('pk', 'sk') if name in item}))
        store = DeletionProgressStore(auth_table=auth.name, registry_table=registry.name,
                                      progress_table=progress.name, client=session.client('dynamodb'))
        cipher = DeletionCheckpointCipher(key, 'dev', source_table_arn=source.table_arn, kms_client=session.client('kms'))
        inventory = MealAggregateDeletionInventory(source_table=source, progress_store=store, cipher=cipher)
        worker = MealAggregateDeletionWorker(inventory=inventory)
        for _ in range(1000):
            if inventory.capture_page(subject=subject, request_id=request, limit=20)['complete']:
                break
        else:
            raise AssertionError('bounded aggregate capture incomplete')
        assert {target.result_id for target in inventory.targets(subject=subject, request_id=request)} == {first, second}
        group_args = dict(source_table=source, progress_store=store, cipher=cipher, subject=subject, request_id=request)
        try:
            clean_one_meal_aggregate_group(**group_args)
        except ValueError:
            pass
        else:
            raise AssertionError('nonempty group accepted')
        registry.update_item(Key=reg_key, UpdateExpression='SET #s = :paused',
                             ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':paused': 'PAUSED'})
        try:
            worker.delete_one(subject=subject, request_id=request, index=0)
        except DeletionProgressConflict:
            pass
        else:
            raise AssertionError('inactive deletion request accepted')
        registry.update_item(Key=reg_key, UpdateExpression='SET #s = :active',
                             ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':active': 'DELETING'})
        original_transact = store.client.transact_write_items
        deletion_calls = []
        def pause_after_first(*args, **kwargs):
            result = original_transact(*args, **kwargs)
            if any('Delete' in item and item['Delete']['TableName'] == source.name
                   for item in kwargs.get('TransactItems', [])):
                deletion_calls.append(1)
                if len(deletion_calls) == 1:
                    registry.update_item(Key=reg_key, UpdateExpression='SET #s = :paused',
                        ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':paused': 'PAUSED'})
            return result
        store.client.transact_write_items = pause_after_first
        try:
            worker.delete_batch(subject=subject, request_id=request, start=0, count=2)
        except ProfileDeletionConflict:
            pass
        else:
            raise AssertionError('batch continued after deletion request paused')
        finally:
            store.client.transact_write_items = original_transact
        remaining = [source.get_item(Key={'pk': group_pk, 'sk': f'RESULT#{identifier}'},
                                     ConsistentRead=True).get('Item') for identifier in (first, second)]
        if len(deletion_calls) != 1 or sum(item is not None for item in remaining) != 1:
            raise AssertionError('inactive deletion request did not stop the batch')
        registry.update_item(Key=reg_key, UpdateExpression='SET #s = :active',
                             ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':active': 'DELETING'})
        assert worker.delete_batch(subject=subject, request_id=request, start=0, count=2) == {
            'state': 'VERIFYING', 'count': 2}
        assert worker.delete_batch(subject=subject, request_id=request, start=0, count=2) == {
            'state': 'VERIFYING', 'count': 2}
        assert clean_one_meal_aggregate_group(**group_args) == {'state': 'IN_PROGRESS', 'removed': True}
        assert clean_one_meal_aggregate_group(**group_args) == {'state': 'VERIFYING', 'removed': False}
        residual = MealAggregateResidualScan(source_table=source, progress_store=store, cipher=cipher)
        for _ in range(1000):
            observed = residual.observe_page(subject=subject, request_id=request, limit=100)
            if observed['residual_found']:
                raise AssertionError('unexpected synthetic aggregate residual')
            if observed['scan_complete']:
                break
        else:
            raise AssertionError('bounded aggregate residual scan incomplete')
        assert source.get_item(Key={'pk': other_pk, 'sk': f'RESULT#{first}'}, ConsistentRead=True).get('Item')
        print('synthetic aggregate capture, lock, removal, residual and other customer isolation verified')
    finally:
        cursor = None
        while True:
            args = {'KeyConditionExpression': Key('pk').eq(f'REQ#{request}'), 'ConsistentRead': True}
            if cursor:
                args['ExclusiveStartKey'] = cursor
            page = progress.query(**args)
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


if __name__ == '__main__':
    main()
