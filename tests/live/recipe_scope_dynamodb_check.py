"""Check candidate recipe scope against synthetic development records only.

This uses developer credentials and a synthetic session. It does not verify an
authenticated customer, role isolation, session policy or model authorization.
"""
import os
import time
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Attr

from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_business_api.auth_store import DynamoAuthStore, Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.recipe_scope import RecipeScopeDenied, resolve_recipe_scope


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
            table.delete_item(Key=key, ConditionExpression=condition)
            if table.get_item(Key=key, ConsistentRead=True).get('Item'):
                raise RuntimeError('synthetic record cleanup failed')
    print('synthetic persisted execution and job scope verified; cleanup verified')


if __name__ == '__main__':
    main()
