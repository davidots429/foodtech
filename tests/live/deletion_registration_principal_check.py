"""Opt-in principal-absence check using isolated synthetic development records."""
import os
import uuid

import boto3
from boto3.dynamodb.conditions import Attr

from foodtech_business_api.deletion_registry_store import DeletionRegistryConflict, DeletionRegistryStore


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_DELETION_PRINCIPAL_LIVE') != '1':
        raise SystemExit('explicit development profile and opt-in required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise RuntimeError('unexpected account')
    dynamo = session.resource('dynamodb')
    auth = dynamo.Table('foodtech-auth-sessions-dev')
    registry = dynamo.Table('foodtech-ai-execution-registry-dev')
    subject, request_id = str(uuid.uuid4()), str(uuid.uuid4())
    auth_key = {'pk': 'USER#' + subject}
    registry_key = {'pk': 'REGISTRY', 'sk': 'REQ#' + request_id}
    store = DeletionRegistryStore(registry.name, auth.name, client=session.client('dynamodb'))
    args = dict(subject=subject, request_id=request_id, encrypted_user_id=b'synthetic-ciphertext',
                encrypted_capsule_locator=b'synthetic-ciphertext-2', requested_at='2026-11-22T00:00:00Z',
                policy_version='synthetic-v1')
    created = False
    try:
        try:
            store.lock_and_register(**args)
        except DeletionRegistryConflict:
            pass
        else:
            raise AssertionError('missing principal accepted')
        if auth.get_item(Key=auth_key, ConsistentRead=True).get('Item') or \
                registry.get_item(Key=registry_key, ConsistentRead=True).get('Item'):
            raise AssertionError('unexpected write')
        auth.put_item(Item={**auth_key, 'epoch': 0, 'provenance': 'SYNTHETIC'},
                      ConditionExpression='attribute_not_exists(pk)')
        created = True
        if not store.lock_and_register(**args):
            raise AssertionError('existing principal rejected')
    finally:
        record = registry.get_item(Key=registry_key, ConsistentRead=True).get('Item')
        if record:
            if record.get('policy_version') != 'synthetic-v1' or record.get('request_id') != request_id:
                raise RuntimeError('unexpected registry item')
            registry.delete_item(Key=registry_key, ConditionExpression=Attr('request_id').eq(request_id) &
                                 Attr('policy_version').eq('synthetic-v1'))
        if created:
            auth.delete_item(Key=auth_key, ConditionExpression=Attr('provenance').eq('SYNTHETIC'))
        if registry.get_item(Key=registry_key, ConsistentRead=True).get('Item') or \
                auth.get_item(Key=auth_key, ConsistentRead=True).get('Item'):
            raise RuntimeError('synthetic cleanup failed')
    print('missing principal rejected; existing principal registered; synthetic cleanup verified')


if __name__ == '__main__':
    main()
