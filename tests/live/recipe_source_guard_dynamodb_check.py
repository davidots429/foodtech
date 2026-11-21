"""Opt-in synthetic version recheck against existing development tables."""
import asyncio
import os
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import boto3
from boto3.dynamodb.conditions import Attr

from foodtech_ai_gateway.policy import Execution, ExecutionSnapshot, Feature
from foodtech_ai_gateway.recipe_source_guard import DynamoRecipeSourceGuard
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.recipe_scope import RecipeScopeDenied, resolve_recipe_scope, verify_recipe_sources
from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker


def main():
    if os.environ.get('AWS_PROFILE') != 'foodtech':
        raise SystemExit('Use the foodtech development profile')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('Unexpected AWS account')
    resource = session.resource('dynamodb')
    jobs, sources = resource.Table('foodtech-cooking-jobs-dev'), resource.Table('foodtech-data-flow-dev')
    suffix = uuid.uuid4().hex
    owner = f'guard-test-{suffix}'
    current, prior = str(uuid.uuid4()), str(uuid.uuid4())
    source_ids = [f'jeyuk-guard-{suffix}-current', f'jeyuk-guard-{suffix}-prior']
    execution = Execution(str(uuid.uuid4()), owner, Feature.RECIPE_CORRECTION, 'synthetic', None, 'SYNTHETIC',
        Decimal('0'), job_id=current, job_version=7, prior_job_id=prior, prior_job_version=3,
        jeyuk_source_revision=3, prior_source_revision=2)
    created = []
    placed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    try:
        for job, version, batch, revision in zip((current, prior), (7, 3), source_ids, (3, 2)):
            started = placed - timedelta(seconds=60 if job == prior else 0)
            job_key = {'pk': f'SCOPE#USER#{owner}#JOB#{job}', 'sk': 'META'}
            source_key = {'pk': f'JEYUK#{batch}', 'sk': 'SOURCE#REVISION'}
            jobs.put_item(Item={**job_key, 'owner_subject': owner, 'job_id': job, 'version': version,
                'menu': 'JEYUK', 'provenance': 'SYNTHETIC', 'actual_placed_at': started.isoformat(),
                'actual_lifted_at': (started + timedelta(seconds=revision)).isoformat(),
                'actual_cooking_seconds': revision, 'source_binding': {'source_id': 'synthetic-rest',
                'device_id': 'mockprofile-110-a-001', 'batch_id': batch}}, ConditionExpression=Attr('pk').not_exists())
            created.append((jobs, job_key))
            binding_key = {'pk': f'JEYUK#{batch}', 'sk': 'BINDING'}
            jobs.put_item(Item={**binding_key, 'job_pk': job_key['pk'], 'job_id': job},
                          ConditionExpression=Attr('pk').not_exists())
            created.append((jobs, binding_key))
            sources.put_item(Item={**source_key, 'revision': revision}, ConditionExpression=Attr('pk').not_exists())
            created.append((sources, source_key))
            tracker = JeyukCompletionTracker(0)
            for second in range(1, revision + 1):
                raw = json.dumps({'fixtureId': batch, 'provenance': 'SYNTHETIC', 'profileId': 'mockprofile-110',
                    'instanceId': 'mockprofile-110-a-001', 'profile': 'SMALL', 'actualLoadKg': 1.8,
                    'second': second, 'observedAt': (started + timedelta(seconds=second)).isoformat(),
                    'sampleIntervalSeconds': 1, 'surfaceCelsius': 190,
                    'actualRpm': 30, 'targetRpm': 30, 'motorLoadPercent': 40})
                tracker.record(raw)
                raw_key = {'pk': source_key['pk'], 'sk': f'RAW#{second:06d}'}
                sources.put_item(Item={**raw_key, 'raw': raw, 'provenance': 'SYNTHETIC'},
                                 ConditionExpression=Attr('pk').not_exists())
                created.append((sources, raw_key))
            receipt_key = {'pk': source_key['pk'], 'sk': 'COLLECTION#RECEIPT#synthetic'}
            receipt = tracker.finish(cursor=revision, complete=True, explicit=True)
            sources.put_item(Item={**receipt_key, **receipt}, ConditionExpression=Attr('pk').not_exists())
            created.append((sources, receipt_key))
        guard = DynamoRecipeSourceGuard(jobs.name, sources.name, resource=resource)
        if not guard.verify(execution): raise AssertionError('matching versions rejected')
        synthetic_session = Session('customer', owner, None, 'synthetic-csrf', int(time.time()) + 600, 0)
        job_store = DynamoCookingStore(jobs.name)
        def execution_read(_):
            return ExecutionSnapshot(execution, 'PENDING', 1)
        scoped = resolve_recipe_scope(synthetic_session, execution.execution_id, execution_read, job_store.read_meta)
        async def combined():
            with patch('foodtech_business_api.recipe_scope._session', new_callable=AsyncMock,
                       return_value=synthetic_session):
                await verify_recipe_sources(object(), scoped, execution_read, job_store.read_meta, guard)
        asyncio.run(combined())
        job_key = {'pk': f'SCOPE#USER#{owner}#JOB#{current}', 'sk': 'META'}
        jobs.update_item(Key=job_key, UpdateExpression='SET actual_lifted_at = :changed',
                         ExpressionAttributeValues={':changed': (placed + timedelta(seconds=4)).isoformat()})
        if guard.verify(execution): raise AssertionError('mismatched cooking timeline accepted')
        jobs.update_item(Key=job_key, UpdateExpression='SET actual_lifted_at = :original',
                         ExpressionAttributeValues={':original': (placed + timedelta(seconds=3)).isoformat()})
        raw_key = {'pk': f'JEYUK#{source_ids[0]}', 'sk': 'RAW#000001'}
        raw = sources.get_item(Key=raw_key, ConsistentRead=True)['Item']['raw']
        sources.update_item(Key=raw_key, UpdateExpression='SET #raw = :changed',
                            ExpressionAttributeNames={'#raw': 'raw'}, ExpressionAttributeValues={':changed': raw + ' '})
        if guard.verify(execution): raise AssertionError('changed raw with unchanged revision accepted')
        try:
            asyncio.run(combined())
        except RecipeScopeDenied:
            pass
        else:
            raise AssertionError('combined scope and source guard accepted modified raw data')
        sources.update_item(Key=raw_key, UpdateExpression='SET #raw = :original',
                            ExpressionAttributeNames={'#raw': 'raw'}, ExpressionAttributeValues={':original': raw})
        sources.update_item(Key={'pk': f'JEYUK#{source_ids[1]}', 'sk': 'SOURCE#REVISION'},
                            UpdateExpression='ADD revision :one', ExpressionAttributeValues={':one': 1})
        if guard.verify(execution): raise AssertionError('changed previous source accepted')
        print('synthetic source, timeline and job version recheck verified')
    finally:
        for table, key in reversed(created): table.delete_item(Key=key)
        if any(table.get_item(Key=key, ConsistentRead=True).get('Item') for table, key in created):
            raise RuntimeError('synthetic records remain')
        print('synthetic source guard cleanup verified')


if __name__ == '__main__': main()
