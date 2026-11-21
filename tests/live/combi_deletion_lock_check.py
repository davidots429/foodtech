"""Opt-in synthetic meal write deletion-lock check."""
import os
from uuid import uuid4

import boto3
from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    db = session.resource('dynamodb')
    table, auth = db.Table('foodtech-cooking-jobs-dev'), db.Table('foodtech-auth-sessions-dev')
    subject = 'synthetic-' + uuid4().hex
    pk, auth_key = f'SCOPE#USER#{subject}#COMBI_MEAL#meal', {'pk': f'USER#{subject}'}
    store = DynamoCookingStore(table.name, auth.name)
    keys = [{'pk': pk, 'sk': sk} for sk in ('META', 'STEP#rice', 'EVENT#1', 'EVENT#2')]
    keys += [{'pk': pk + '#LIVE', 'sk': sk} for sk in ('META', 'CHUNK#000000')]
    keys += [{'pk': pk + '#SOURCE', 'sk': 'RAW#0000'}]
    listing_key = {'pk': f'SCOPE#USER#{subject}#COMBI_MEALS', 'sk': 'MEAL#meal'}
    created = False
    try:
        auth.put_item(Item={**auth_key, 'epoch': 0, 'deletion_lock_request': str(uuid4())},
                      ConditionExpression='attribute_not_exists(pk)')
        created = True
        def blocked(operation):
            try:
                operation()
            except Conflict:
                return
            raise AssertionError('guarded meal write was accepted')
        records = [{'sk': 'META', 'version': 1, 'meal_batch_id': 'meal'}, {'sk': 'STEP#rice', 'status': 'PENDING'}]
        blocked(lambda: store.create_meal(pk, records, 0))
        if any(table.get_item(Key=key, ConsistentRead=True).get('Item') for key in [*keys, listing_key]):
            raise AssertionError('locked meal creation left records')
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        blocked(lambda: store.create_meal(pk, records, 1))
        store.create_meal(pk, records, 0)
        if store.list_meal_refs(subject, 1, None)['items'] != [{'meal_batch_id': 'meal'}]:
            raise AssertionError('meal inventory missing')
        auth.update_item(Key=auth_key, UpdateExpression='SET deletion_lock_request = :id',
                         ExpressionAttributeValues={':id': str(uuid4())})
        table.delete_item(Key=listing_key)
        blocked(lambda: store.ensure_meal_listed(subject, 'meal'))
        if table.get_item(Key=listing_key, ConsistentRead=True).get('Item'):
            raise AssertionError('locked reconciliation created inventory')
        advance = lambda: store.advance_meal(pk, 1, {'sk': 'META', 'version': 2},
                                             {'sk': 'STEP#rice', 'status': 'DONE'}, {'sk': 'EVENT#1'}, 0)
        append = lambda: store.append_meal_live(pk, 0, {'sk': 'META', 'count': 1}, {'sk': 'CHUNK#000000'}, 0)
        attach = lambda: store.attach_meal_source(pk, 1, {'sk': 'META', 'version': 2, 'source': 'synthetic'},
                                                {'sk': 'EVENT#2'}, [{'sk': 'RAW#0000'}], None, 0)
        for operation in (advance, append, attach):
            blocked(operation)
        if any(table.get_item(Key=key, ConsistentRead=True).get('Item') for key in keys[2:]):
            raise AssertionError('locked meal update left records')
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        store.ensure_meal_listed(subject, 'meal')
        store.ensure_meal_listed(subject, 'meal')
        blocked(lambda: store.ensure_meal_listed(subject, 'missing'))
        if store.list_meal_refs(subject, 1, None)['items'] != [{'meal_batch_id': 'meal'}]:
            raise AssertionError('legacy meal inventory repair failed')
        append()
        store.attach_meal_source(pk, 1, {'sk': 'META', 'version': 2, 'source': 'synthetic'},
                                 {'sk': 'EVENT#2'}, [{'sk': 'RAW#0000'}], 1, 0)
        store.advance_meal(pk, 2, {'sk': 'META', 'version': 3},
                           {'sk': 'STEP#rice', 'status': 'DONE'}, {'sk': 'EVENT#1'}, 0)
    finally:
        for key in keys:
            table.delete_item(Key=key)
        table.delete_item(Key=listing_key)
        if created:
            auth.delete_item(Key=auth_key)
    print('meal create, advance, append and attachment guard and cleanup verified')


if __name__ == '__main__':
    main()
