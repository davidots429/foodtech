"""Opt-in synthetic AI reservation guard against development DynamoDB."""
import os
import secrets
from decimal import Decimal
from uuid import uuid4

import boto3
from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_ai_gateway.registry import DynamoRegistry, RegistryConflict


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    resource = session.resource('dynamodb')
    table, auth = resource.Table('foodtech-ai-execution-registry-dev'), resource.Table('foodtech-auth-sessions-dev')
    period = f'{secrets.randbelow(4000) + 5000:04d}-{secrets.randbelow(12) + 1:02d}'
    tenant, provider, run = 'synthetic-' + uuid4().hex, 'synthetic-' + uuid4().hex, str(uuid4())
    auth_key = {'pk': f'USER#{tenant}'}
    keys = [{'pk': f'LIMIT#{period}#{scope}#{value}', 'sk': 'META'} for scope, value in
            (('TENANT', tenant), ('PROVIDER', provider), ('PROJECT', 'GLOBAL'))]
    keys.append({'pk': f'EXEC#{run}', 'sk': 'META'})
    blocked_run, blocked_quote = str(uuid4()), str(uuid4())
    keys += [{'pk': f'EXEC#{blocked_run}', 'sk': 'META'}, {'pk': f'QUOTE#{blocked_quote}', 'sk': 'META'}]
    registry = DynamoRegistry(table.name, period, resource=resource, client=session.client('dynamodb'), auth_table=auth.name)
    auth_created = False
    try:
        registry.create_limit('TENANT', tenant, Decimal('0'), calls_remaining=1)
        registry.create_limit('PROVIDER', provider, Decimal('0'))
        registry.create_limit('PROJECT', 'GLOBAL', Decimal('0'))
        execution = Execution(run, tenant, Feature.GENERAL_GUIDE, 'synthetic-model', None, 'SYNTHETIC', Decimal('0'))
        registry.create_execution(execution)
        auth.put_item(Item={**auth_key, 'deletion_lock_request': str(uuid4())}, ConditionExpression='attribute_not_exists(pk)')
        auth_created = True
        for operation in (
            lambda: registry.create_execution(Execution(blocked_run, tenant, Feature.GENERAL_GUIDE,
                                                        'synthetic-model', None, 'SYNTHETIC', Decimal('0'))),
            lambda: registry.create_quote(blocked_quote, {'tenant_id': tenant}),
        ):
            try:
                operation()
            except RegistryConflict:
                pass
            else:
                raise AssertionError('locked tenant record was created')
        if any(table.get_item(Key=key, ConsistentRead=True).get('Item') for key in keys[4:]):
            raise AssertionError('locked creation left a record')
        registry.begin_request()
        execution = registry.execution(run)
        registry.budget(tenant, provider)
        try:
            registry.reserve(execution, provider)
        except RegistryConflict:
            pass
        else:
            raise AssertionError('locked reservation was accepted')
        if table.get_item(Key=keys[3], ConsistentRead=True)['Item']['state'] != 'PENDING' or \
                table.get_item(Key=keys[0], ConsistentRead=True)['Item']['calls_remaining'] != 1:
            raise AssertionError('locked reservation changed execution or limit')
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        registry.begin_request()
        registry.budget(tenant, provider)
        registry.reserve(registry.execution(run), provider)
        if table.get_item(Key=keys[3], ConsistentRead=True)['Item']['state'] != 'RESERVED':
            raise AssertionError('valid reservation failed')
    finally:
        for key in keys:
            table.delete_item(Key=key)
        if auth_created:
            auth.delete_item(Key=auth_key)
    print('AI reservation lock, unchanged limit, unlocked reservation and cleanup verified')


if __name__ == '__main__':
    main()
