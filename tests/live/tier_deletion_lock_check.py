"""Opt-in tier transaction guard check using isolated keys in a dev table."""
import os
from uuid import uuid4

import boto3
from foodtech_business_api.tier_store import DynamoTierStore, TierConflict


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    dynamo = session.resource('dynamodb')
    table, auth = dynamo.Table('foodtech-cooking-jobs-dev'), dynamo.Table('foodtech-auth-sessions-dev')
    customer, admin, app = ('synthetic-' + uuid4().hex for _ in range(3))
    auth_keys = [{'pk': f'USER#{value}'} for value in (customer, admin)]
    keys = [{'pk': f'TENANT#{customer}', 'sk': sk} for sk in ('CURRENT', f'APPLICATION#{app}', f'REVIEW#{app}')]
    store = DynamoTierStore(table.name)
    store.auth_table_name = auth.name
    item = {'subject': customer, 'application_id': app, 'submitted_at': '2026-11-21T00:00:00Z',
            'status': 'REQUESTED', 'tier': 'FREE', 'version': 1}
    created = []
    try:
        for key in auth_keys:
            auth.put_item(Item={**key, 'epoch': 0}, ConditionExpression='attribute_not_exists(pk)')
            created.append(key)
        def blocked(operation):
            try:
                operation()
            except TierConflict:
                return
            raise AssertionError('guarded tier write succeeded')
        auth.update_item(Key=auth_keys[0], UpdateExpression='SET deletion_lock_request = :id',
                         ExpressionAttributeValues={':id': str(uuid4())})
        blocked(lambda: store.submit(customer, item, 0))
        if any(table.get_item(Key=key, ConsistentRead=True).get('Item') for key in keys):
            raise AssertionError('locked submission left data')
        auth.update_item(Key=auth_keys[0], UpdateExpression='REMOVE deletion_lock_request')
        blocked(lambda: store.submit(customer, item, 1))
        store.submit(customer, item, 0)
        auth.update_item(Key=auth_keys[0], UpdateExpression='SET deletion_lock_request = :id',
                         ExpressionAttributeValues={':id': str(uuid4())})
        blocked(lambda: store.decide(customer, app, admin, 'REJECTED', 'now', 0))
        auth.update_item(Key=auth_keys[0], UpdateExpression='REMOVE deletion_lock_request')
        blocked(lambda: store.decide(customer, app, admin, 'REJECTED', 'now', 1))
        auth.update_item(Key=auth_keys[1], UpdateExpression='SET admin_blocked = :yes',
                         ExpressionAttributeValues={':yes': True})
        blocked(lambda: store.decide(customer, app, admin, 'REJECTED', 'now', 0))
        auth.update_item(Key=auth_keys[1], UpdateExpression='REMOVE admin_blocked')
        store.decide(customer, app, admin, 'REJECTED', 'now', 0)
        if store.current(customer)['status'] != 'REJECTED':
            raise AssertionError('valid review not saved')
        cursor, inventory = None, []
        while True:
            page = store.inventory_keys(customer, 1, cursor)
            inventory.extend(page['keys'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        if set(inventory) != {key['sk'] for key in keys} or store.inventory_keys(admin, 10, None)['keys']:
            raise AssertionError('tier history inventory incomplete or cross-owner')
        request = str(uuid4())
        auth.update_item(Key=auth_keys[0], UpdateExpression='SET deletion_lock_request = :id',
                         ExpressionAttributeValues={':id': request})
        blocked(lambda: store.delete_inventory_key(customer, str(uuid4()), inventory[0]))
        if not table.get_item(Key={'pk': keys[0]['pk'], 'sk': inventory[0]}, ConsistentRead=True).get('Item'):
            raise AssertionError('wrong request removed a record')
        if store.delete_inventory_page(customer, request, 1) != 1:
            raise AssertionError('first bounded deletion did not remove one key')
        if len(store.inventory_keys(customer, 10, None)['keys']) != 2:
            raise AssertionError('bounded deletion removed an unexpected number of keys')
        if store.delete_inventory_page(customer, request, 10) != 2:
            raise AssertionError('resumed deletion did not remove remaining keys')
        store.delete_inventory_key(customer, request, inventory[0])
        if store.inventory_keys(customer, 10, None)['keys']:
            raise AssertionError('tier records remain after deletion')
        if store.delete_inventory_page(customer, request, 10) != 0:
            raise AssertionError('empty retry was not idempotent')
        blocked(lambda: store.delete_inventory_page(customer, str(uuid4()), 10))
    finally:
        for key in keys:
            table.delete_item(Key=key)
        for key in created:
            auth.delete_item(Key=key)
    print('tier submission and review deletion/epoch guards and cleanup verified')


if __name__ == '__main__':
    main()
