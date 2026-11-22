"""Opt-in development GSI visibility check; removes only generated test records."""
import os
import time
from uuid import uuid4

import boto3
from foodtech_business_api.tier_store import DynamoTierStore

REGION = 'ap-southeast-2'
ACCOUNT = '573693340100'
TABLE = os.getenv('FOODTECH_TIER_TEST_TABLE', 'foodtech-tier-requests-dev')
AUTH = 'foodtech-auth-sessions-dev'


def wait_for(observe, expected, *, timeout=45):
    end = time.monotonic() + timeout
    while True:
        value = observe()
        if value == expected:
            return
        if time.monotonic() >= end:
            raise TimeoutError('GSI did not reach the expected observed state')
        time.sleep(1)


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_TIER_GSI_LIVE') != '1' or \
            (TABLE != 'foodtech-tier-requests-dev' and not TABLE.startswith('foodtech-tier-gsi-test-')):
        raise SystemExit('explicit profile, opt-in and approved development table required')
    session = boto3.Session(profile_name='foodtech', region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise RuntimeError('unexpected AWS account')
    client = session.client('dynamodb')
    description = client.describe_table(TableName=TABLE)['Table']
    indexes = [i for i in description.get('GlobalSecondaryIndexes', []) if i['IndexName'] == 'PendingRequests']
    if description['TableStatus'] != 'ACTIVE' or len(indexes) != 1 or indexes[0]['IndexStatus'] != 'ACTIVE' or \
            indexes[0]['KeySchema'] != [{'AttributeName': 'queue_status', 'KeyType': 'HASH'},
                                        {'AttributeName': 'queue_sort', 'KeyType': 'RANGE'}]:
        raise RuntimeError('expected development GSI is not active')
    resource = session.resource('dynamodb')
    table, auth = resource.Table(TABLE), resource.Table(AUTH)
    deletion_registry = resource.Table('foodtech-cooking-jobs-dev')
    subject, request, application = str(uuid4()), str(uuid4()), str(uuid4())
    queue_sort = '2026-11-21T00:00:00Z#' + application
    auth_key = {'pk': 'USER#' + subject}
    item_key = {'pk': 'TENANT#' + subject, 'sk': 'CURRENT'}
    created_auth = created_item = created_deletion = False
    deletion_key = {'pk':'REGISTRY','sk':f'REQ#{request}'}
    store = DynamoTierStore(TABLE)
    store.auth_table_name = AUTH
    store.deletion_registry_table_name = deletion_registry.name
    try:
        auth.put_item(Item={**auth_key, 'suspended': True, 'deletion_lock_request': request},
                      ConditionExpression='attribute_not_exists(pk)')
        created_auth = True
        deletion_registry.put_item(Item={**deletion_key,'status':'DELETING'},ConditionExpression='attribute_not_exists(pk)')
        created_deletion = True
        table.put_item(Item={**item_key, 'subject': subject, 'queue_status': 'REQUESTED', 'queue_sort': queue_sort},
                       ConditionExpression='attribute_not_exists(pk) AND attribute_not_exists(sk)')
        created_item = True
        observe = lambda: store.observe_pending_index(subject, request, queue_sort)
        wait_for(observe, 'PRESENT')
        store.delete_inventory_key(subject, request, 'CURRENT')
        wait_for(observe, 'NOT_OBSERVED')
        # A missing row is a point-in-time observation, not a propagation or deletion guarantee.
        print('synthetic GSI present and later not observed; this is not deletion completion proof')
    finally:
        if created_item:
            table.delete_item(Key=item_key)
            if table.get_item(Key=item_key, ConsistentRead=True).get('Item') is not None:
                raise RuntimeError('synthetic tier record cleanup failed')
        if created_deletion:
            deletion_registry.delete_item(Key=deletion_key)
            if deletion_registry.get_item(Key=deletion_key,ConsistentRead=True).get('Item'):
                raise RuntimeError('synthetic deletion registry cleanup failed')
        if created_auth:
            auth.delete_item(Key=auth_key)
            if auth.get_item(Key=auth_key, ConsistentRead=True).get('Item') is not None:
                raise RuntimeError('synthetic auth record cleanup failed')


if __name__ == '__main__':
    main()
