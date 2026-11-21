"""Check candidate recipe scope against synthetic development records only.

This uses developer credentials and a synthetic session. It does not verify an
authenticated customer, role isolation, session policy or model authorization.
"""
import asyncio
import os
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Attr
from fastapi import HTTPException
from starlette.requests import Request

from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_business_api import auth as auth_module
from foodtech_business_api.auth_store import DynamoAuthStore, Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.recipe_scope import RecipeScopeDenied, authenticated_recipe_scope, resolve_recipe_scope


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('Use AWS_PROFILE=foodtech')
    aws = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if aws.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('Unexpected AWS account')
    resource = aws.resource('dynamodb')
    auth = DynamoAuthStore.__new__(DynamoAuthStore)
    auth.table = resource.Table('foodtech-auth-sessions-dev')
    registry = DynamoRegistry('foodtech-ai-execution-registry-dev', '2026-11', resource=resource,
                              client=aws.client('dynamodb'))
    jobs = DynamoCookingStore('foodtech-cooking-jobs-dev')
    session_id, execution_id, current, prior = (str(uuid4()) for _ in range(4))
    subject = 'scope-test-' + uuid4().hex
    session = Session('customer', subject, None, 'synthetic-csrf', int(time.time()) + 600, 0)
    execution = Execution(execution_id, subject, Feature.RECIPE_CORRECTION, 'synthetic', None,
                          'SYNTHETIC', Decimal('0'), job_id=current, job_version=2,
                          prior_job_id=prior, prior_job_version=1,
                          jeyuk_source_revision=4, prior_source_revision=3)
    created = []
    try:
        auth.save_session(session_id, session)
        created.append((auth.table, {'pk': f'SESSION#{session_id}'}, Attr('csrf').eq('synthetic-csrf')))
        registry.create_execution(execution)
        created.append((registry.table, {'pk': f'EXEC#{execution_id}', 'sk': 'META'}, Attr('tenant_id').eq(subject)))
        for identifier, version in ((current, 2), (prior, 1)):
            key = {'pk': f'SCOPE#USER#{subject}#JOB#{identifier}', 'sk': 'META'}
            jobs.table.put_item(Item={**key, 'owner_subject': subject, 'job_id': identifier, 'version': version,
                'menu': 'JEYUK', 'provenance': 'SYNTHETIC', 'source_binding': {
                    'source_id': 'synthetic-rest', 'device_id': 'mockprofile-110-a-001',
                    'batch_id': 'jeyuk-' + identifier}}, ConditionExpression=Attr('pk').not_exists())
            created.append((jobs.table, key, Attr('owner_subject').eq(subject)))
        saved = auth.read_session(session_id)
        scope = resolve_recipe_scope(saved, execution_id, registry.execution, jobs.read_meta)
        assert scope.source_partitions == ('JEYUK#jeyuk-' + current, 'JEYUK#jeyuk-' + prior)
        try:
            resolve_recipe_scope(replace(saved, subject='another-customer'), execution_id,
                                 registry.execution, jobs.read_meta)
        except RecipeScopeDenied:
            pass
        else:
            raise AssertionError('other customer accepted')
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [
            (b'cookie', f'{auth_module.SESSION_COOKIE}={session_id}'.encode())]})
        def scope_read():
            return authenticated_recipe_scope(request, execution_id, registry.execution, jobs.read_meta)
        with patch.object(auth_module, 'get_store', return_value=auth), \
             patch.object(auth_module.Settings, 'from_env', return_value=SimpleNamespace()), \
             patch.object(auth_module, '_cognito_access', return_value=True):
            assert asyncio.run(scope_read()).owner == subject
            calls = 0
            def suspend_during_read(key):
                nonlocal calls
                calls += 1
                if calls == 4:
                    auth.suspend_user(subject)
                    created.append((auth.table, {'pk': f'USER#{subject}'}, Attr('suspended').eq(True)))
                return jobs.read_meta(key)
            try:
                asyncio.run(authenticated_recipe_scope(request, execution_id, registry.execution, suspend_during_read))
            except HTTPException as error:
                if error.status_code != 401:
                    raise
            else:
                raise AssertionError('suspended session accepted')
            if auth.read_session(session_id) is not None:
                raise AssertionError('revoked session remains')
        current_key = {'pk': f'SCOPE#USER#{subject}#JOB#{current}', 'sk': 'META'}
        jobs.table.update_item(Key=current_key, UpdateExpression='SET #v = :new',
            ExpressionAttributeNames={'#v': 'version'}, ExpressionAttributeValues={':new': 3})
        try:
            resolve_recipe_scope(saved, execution_id, registry.execution, jobs.read_meta)
        except RecipeScopeDenied:
            pass
        else:
            raise AssertionError('stale job accepted')
    finally:
        for table, key, condition in reversed(created):
            if table.get_item(Key=key, ConsistentRead=True).get('Item'):
                table.delete_item(Key=key, ConditionExpression=condition)
            if table.get_item(Key=key, ConsistentRead=True).get('Item'):
                raise RuntimeError('synthetic record cleanup failed')
    print('synthetic session revocation and persisted execution scope verified; cleanup verified')


if __name__ == '__main__':
    main()
