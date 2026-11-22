"""Synthetic enrollment/retirement fence race on existing development tables.

Uses a unique test control key, not the production enrollment control. The
registry COMPLETED row is synthetic evidence for transaction testing only.
No Cognito user is created, changed or deleted; only random-subject absence is
checked. This does not prove full erasure or email-based re-registration.
"""
import argparse
from pathlib import Path
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api.principal_enrollment import (
    FENCE_KEY, DynamoPrincipalEnrollment, DynamoPrincipalRetirement, PrincipalEnrollmentConflict,
)
from foodtech_business_api.cooking_store import DynamoCookingStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--business-env', type=Path, required=True)
    args = parser.parse_args()
    # Read this public configuration field only; never print secret-bearing env
    # contents, load provider credentials, or put them in the process environment.
    issuer = None
    for line in args.business_env.read_text().splitlines():
        name, separator, value = line.partition('=')
        if separator and name.strip() == 'FOODTECH_COGNITO_ISSUER':
            issuer = value.strip().strip('"').rstrip('/')
            break
    if not issuer or not issuer.startswith('https://cognito-idp.ap-southeast-2.amazonaws.com/'):
        raise RuntimeError('existing development Cognito issuer configuration is required')
    pool_id = issuer.rsplit('/', 1)[-1]
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    resource = session.resource('dynamodb')
    auth, registry = resource.Table('foodtech-auth-sessions-dev'), resource.Table('foodtech-ai-execution-registry-dev')
    old, new, request = str(uuid4()), str(uuid4()), str(uuid4())
    fence_key = FENCE_KEY + '#' + str(uuid4())
    auth_keys = [{'pk': fence_key}, {'pk': f'USER#{old}'}, {'pk': f'USER#{new}'}]
    registry_key = {'pk': 'REGISTRY', 'sk': f'REQ#{request}'}
    cooking = resource.Table('foodtech-cooking-jobs-dev')
    write_key = {'pk': 'SYNTHETIC#ENROLLMENT#' + str(uuid4()), 'sk': 'CHECK'}
    if any('Item' in auth.get_item(Key=key, ConsistentRead=True) for key in auth_keys) or \
            'Item' in registry.get_item(Key=registry_key, ConsistentRead=True) or \
            'Item' in cooking.get_item(Key=write_key, ConsistentRead=True):
        raise RuntimeError('generated synthetic keys already exist')
    enrollment = DynamoPrincipalEnrollment(table=auth, fence_key=fence_key)
    retire = DynamoPrincipalRetirement(auth_table=auth, registry_table=registry.name,
        cognito_client=session.client('cognito-idp'), pool_id=pool_id, fence_key=fence_key)
    started = False
    guard = DynamoCookingStore.__new__(DynamoCookingStore)
    guard.auth_table_name, guard.serializer = auth.name, TypeSerializer()
    raw = session.client('dynamodb')
    def guarded_business_write():
        raw.transact_write_items(TransactItems=[guard._owner_guard(new, 0),
            {'Put': {'TableName': cooking.name, 'Item': guard._item({**write_key, 'synthetic_enrollment_test': True})}}])
    try:
        enrollment.bootstrap()
        started = True
        observed = enrollment.fence()
        enrollment.enroll(subject=old, expected_generation=observed)
        auth.update_item(Key={'pk': f'USER#{old}'}, UpdateExpression='SET suspended = :yes, deletion_lock_request = :request, #epoch = :epoch',
            ExpressionAttributeNames={'#epoch': 'epoch'}, ExpressionAttributeValues={':yes': True, ':request': request, ':epoch': 2})
        registry.put_item(Item={**registry_key, 'request_id': request, 'status': 'DELETING', 'synthetic_enrollment_test': True},
                          ConditionExpression='attribute_not_exists(pk)')
        try:
            retire.retire(subject=old, request_id=request, expected_epoch=2)
        except PrincipalEnrollmentConflict:
            assert enrollment.fence() == observed
            assert 'Item' in auth.get_item(Key={'pk': f'USER#{old}'}, ConsistentRead=True)
        else:
            raise AssertionError('incomplete deletion must not retire a principal')
        registry.update_item(Key=registry_key, UpdateExpression='SET #status = :completed',
            ExpressionAttributeNames={'#status': 'status'}, ExpressionAttributeValues={':completed': 'COMPLETED'})
        retire.retire(subject=old, request_id=request, expected_epoch=2)
        assert enrollment.fence() == observed + 1
        assert 'Item' not in auth.get_item(Key={'pk': f'USER#{old}'}, ConsistentRead=True)
        try:
            enrollment.enroll(subject=old, expected_generation=observed)
        except PrincipalEnrollmentConflict:
            pass
        else:
            raise AssertionError('stale pre-retirement enrollment must fail')
        try:
            guarded_business_write()
        except raw.exceptions.TransactionCanceledException:
            assert 'Item' not in cooking.get_item(Key=write_key, ConsistentRead=True)
        else:
            raise AssertionError('missing principal must block protected business writes')
        enrollment.enroll(subject=new, expected_generation=enrollment.fence())
        guarded_business_write()
        assert auth.get_item(Key={'pk': f'USER#{new}'}, ConsistentRead=True)['Item']['epoch'] == 0
        assert 'Item' not in auth.get_item(Key={'pk': f'USER#{old}'}, ConsistentRead=True)
        assert 'Item' in cooking.get_item(Key=write_key, ConsistentRead=True)
        print('Conditional enrollment, completion guard, fence retirement, stale enrollment rejection and protected write passed')
    finally:
        if started:
            for key in auth_keys:
                auth.delete_item(Key=key)
            registry.delete_item(Key=registry_key)
            cooking.delete_item(Key=write_key)
            assert all('Item' not in auth.get_item(Key=key, ConsistentRead=True) for key in auth_keys)
            assert 'Item' not in registry.get_item(Key=registry_key, ConsistentRead=True)
            assert 'Item' not in cooking.get_item(Key=write_key, ConsistentRead=True)
            print('Generated test control and account/registry items removed; absence verified')


if __name__ == '__main__':
    main()
