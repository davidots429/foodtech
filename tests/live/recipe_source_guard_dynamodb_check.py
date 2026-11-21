"""Opt-in synthetic version recheck against existing development tables."""
import asyncio
import os
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from starlette.requests import Request

from foodtech_ai_gateway.policy import Caller, Execution, Feature, Model, Tier
from foodtech_ai_gateway.ollama import OllamaResult
from foodtech_ai_gateway.recipe_source_guard import BoundRecipeSourceGuard, DynamoRecipeSourceGuard
from foodtech_ai_gateway.registry import DynamoRegistry, RegistryConflict
from foodtech_ai_gateway.service import GatewayService
from foodtech_business_api import auth as auth_module
from foodtech_business_api.auth_store import DynamoAuthStore, Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.recipe_scope import RecipeScopeDenied, resolve_recipe_scope, verify_recipe_sources
from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import JeyukBaseline


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
        auth_store = DynamoAuthStore.__new__(DynamoAuthStore)
        auth_store.table = resource.Table('foodtech-auth-sessions-dev')
        principal_key = {'pk': f'USER#{owner}'}
        auth_store.table.put_item(Item={**principal_key, 'epoch': 0, 'test_marker': suffix},
                                  ConditionExpression=Attr('pk').not_exists())
        created.append((auth_store.table, principal_key))
        session_id = str(uuid.uuid4())
        synthetic_session = Session('customer', owner, None, 'synthetic-csrf', int(time.time()) + 600, 0)
        auth_store.save_session(session_id, synthetic_session)
        created.append((auth_store.table, {'pk': f'SESSION#{session_id}'}))
        registry = DynamoRegistry('foodtech-ai-execution-registry-dev', '2026-11', resource=resource,
                                  client=session.client('dynamodb'))
        registry.create_execution(execution)
        created.append((registry.table, {'pk': f'EXEC#{execution.execution_id}', 'sk': 'META'}))
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
        job_store = DynamoCookingStore(jobs.name)
        execution_read = registry.execution_snapshot
        scoped = resolve_recipe_scope(auth_store.read_session(session_id), execution.execution_id,
                                      execution_read, job_store.read_meta)
        bound = BoundRecipeSourceGuard(guard, execution, scoped.job_partitions, scoped.source_partitions)
        if not bound.verify(execution) or bound.verify(replace(execution, tenant_id='other-customer')):
            raise AssertionError('bound source guard rejected owner or accepted another execution')
        checks = bound.settlement_checks()
        session.client('dynamodb').transact_write_items(TransactItems=checks)
        receipt_key = {'pk': f'JEYUK#{source_ids[0]}', 'sk': 'COLLECTION#RECEIPT#synthetic'}
        original_digest = sources.get_item(Key=receipt_key, ConsistentRead=True)['Item']['ordered_raw_sha256']
        sources.update_item(Key=receipt_key, UpdateExpression='SET ordered_raw_sha256 = :digest',
                            ExpressionAttributeValues={':digest': '0' * 64})
        try:
            session.client('dynamodb').transact_write_items(TransactItems=checks)
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') != 'TransactionCanceledException':
                raise
        else:
            raise AssertionError('changed receipt passed settlement conditions')
        sources.update_item(Key=receipt_key, UpdateExpression='SET ordered_raw_sha256 = :digest',
                            ExpressionAttributeValues={':digest': original_digest})
        request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [
            (b'cookie', f'{auth_module.SESSION_COOKIE}={session_id}'.encode())]})
        async def combined():
            with patch.object(auth_module, 'get_store', return_value=auth_store), \
                 patch('foodtech_business_api.recipe_scope.get_store', return_value=auth_store), \
                 patch.object(auth_module.Settings, 'from_env', return_value=SimpleNamespace()), \
                 patch.object(auth_module, '_cognito_access', return_value=True):
                await verify_recipe_sources(request, scoped, execution_read, job_store.read_meta, guard)
        asyncio.run(combined())
        job_key = {'pk': f'SCOPE#USER#{owner}#JOB#{current}', 'sk': 'META'}
        jobs.update_item(Key=job_key, UpdateExpression='SET #binding.#batch = :value',
            ExpressionAttributeNames={'#binding': 'source_binding', '#batch': 'batch_id'},
            ExpressionAttributeValues={':value': f'jeyuk-foreign-{suffix}'})
        try:
            session.client('dynamodb').transact_write_items(TransactItems=checks)
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') != 'TransactionCanceledException':
                raise
        else:
            raise AssertionError('changed binding passed settlement conditions')
        if guard.verify_scoped(execution, scoped.job_partitions, scoped.source_partitions):
            raise AssertionError('source outside scoped binding accepted')
        jobs.update_item(Key=job_key, UpdateExpression='SET #binding.#batch = :value',
            ExpressionAttributeNames={'#binding': 'source_binding', '#batch': 'batch_id'},
            ExpressionAttributeValues={':value': source_ids[0]})
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
        if bound.verify(execution): raise AssertionError('bound source guard accepted changed raw data')
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
        try:
            session.client('dynamodb').transact_write_items(TransactItems=checks)
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') != 'TransactionCanceledException':
                raise
        else:
            raise AssertionError('changed source revision passed settlement conditions')
        # Isolated budget keys avoid modifying any real tenant, provider or project limit.
        original_limit_id = registry._limit_id
        registry._limit_id = lambda scope, identifier: original_limit_id(scope, f'synthetic-{suffix}-{identifier}')
        for scope, identifier in (('TENANT', owner), ('PROVIDER', 'ollama'), ('PROJECT', 'GLOBAL')):
            limit_id = registry._limit_id(scope, identifier)
            fields = {'remaining_usd': Decimal('1'), 'period': registry.period}
            if scope == 'TENANT':
                fields['calls_remaining'] = 2
            registry._put_new('LIMIT', limit_id, fields)
            created.append((registry.table, {'pk': f'LIMIT#{limit_id}', 'sk': 'META'}))
        execution_key = {'pk': f'EXEC#{execution.execution_id}', 'sk': 'META'}
        result_key = {'pk': execution_key['pk'], 'sk': 'RESULT'}
        created.append((registry.table, result_key))
        registry.table.update_item(Key=execution_key,
            UpdateExpression='SET #state = :reserved, reserved_provider = :provider ADD #version :one',
            ExpressionAttributeNames={'#state': 'state', '#version': 'version'},
            ExpressionAttributeValues={':reserved': 'RESERVED', ':provider': 'ollama', ':one': 1},
            ConditionExpression=Attr('tenant_id').eq(owner))
        try:
            registry.finish(execution.execution_id, succeeded=True, actual_cost_usd=Decimal('0'),
                            response_text='synthetic', expected_execution=execution, recipe_source_guard=bound)
        except RegistryConflict:
            pass
        else:
            raise AssertionError('stale source allowed result settlement')
        if registry.table.get_item(Key=result_key, ConsistentRead=True).get('Item') or \
                registry.table.get_item(Key=execution_key, ConsistentRead=True)['Item']['state'] != 'RESERVED':
            raise AssertionError('failed source condition partially committed settlement')
        for scope, identifier in (('TENANT', owner), ('PROVIDER', 'ollama'), ('PROJECT', 'GLOBAL')):
            key = {'pk': f'LIMIT#{registry._limit_id(scope, identifier)}', 'sk': 'META'}
            if int(registry.table.get_item(Key=key, ConsistentRead=True)['Item']['version']) != 1:
                raise AssertionError('failed source condition changed a synthetic limit')
        sources.update_item(Key={'pk': f'JEYUK#{source_ids[1]}', 'sk': 'SOURCE#REVISION'},
                            UpdateExpression='SET revision = :original', ExpressionAttributeValues={':original': 2})
        registry.finish(execution.execution_id, succeeded=True, actual_cost_usd=Decimal('0'),
                        response_text='synthetic', expected_execution=execution, recipe_source_guard=bound)
        if registry.table.get_item(Key=execution_key, ConsistentRead=True)['Item']['state'] != 'SUCCEEDED' or \
                not registry.table.get_item(Key=result_key, ConsistentRead=True).get('Item'):
            raise AssertionError('valid source settlement did not commit')
        model_id = 'synthetic-tier1-' + suffix
        now = datetime.now(timezone.utc)
        registry.create_model(Model(model_id, 'ollama', Tier.TIER_1, 'ACTIVE', True, True, now, now))
        created.append((registry.table, {'pk': f'MODEL#{model_id}', 'sk': 'META'}))
        registry.local_models.add(model_id)
        class SyntheticModel:
            async def structured_chat(self, *_):
                return OllamaResult('{"temperature_c":200,"rpm":30,"duration_seconds":480,"load_kg":1.8,"vegetable_interval_seconds":60,"sauce_interval_seconds":60}', 1, 1)
        def start_execution():
            next_execution = replace(execution, execution_id=str(uuid.uuid4()), model_id=model_id)
            registry.create_execution(next_execution)
            created.append((registry.table, {'pk': f'EXEC#{next_execution.execution_id}', 'sk': 'META'}))
            created.append((registry.table, {'pk': f'EXEC#{next_execution.execution_id}', 'sk': 'RESULT'}))
            next_guard = BoundRecipeSourceGuard(guard, next_execution, scoped.job_partitions, scoped.source_partitions)
            return next_execution, next_guard
        def invoke(service, next_execution):
            return asyncio.run(service.execute_local_recipe(Caller('business-api', owner), next_execution.execution_id,
                evidence=Evidence('COMPLETE', 'PASSED', True, False),
                baseline=JeyukBaseline('SMALL', Decimal('200'), Decimal('30'), 480, Decimal('1.8'), 60, 60, False),
                context={}, ollama=SyntheticModel()))
        next_execution, next_guard = start_execution()
        outcome = invoke(GatewayService(registry, recipe_source_guard=next_guard), next_execution)
        if outcome.state != 'SUCCEEDED':
            raise AssertionError(f'synthetic guarded gateway failed: {outcome.error_code}')
        racing_execution, racing_guard = start_execution()
        original_finish = registry.finish
        def race_before_settlement(*args, **kwargs):
            sources.update_item(Key={'pk': f'JEYUK#{source_ids[1]}', 'sk': 'SOURCE#REVISION'},
                                UpdateExpression='ADD revision :one', ExpressionAttributeValues={':one': 1})
            return original_finish(*args, **kwargs)
        registry.finish = race_before_settlement
        outcome = invoke(GatewayService(registry, recipe_source_guard=racing_guard), racing_execution)
        if (outcome.state, outcome.error_code, outcome.response_text) != \
                ('FAILED', 'SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED', None) or \
                registry.execution_result(racing_execution.execution_id) is not None:
            raise AssertionError('source race allowed a gateway result')
        print('synthetic source, timeline and job version recheck verified')
    finally:
        for table, key in reversed(created): table.delete_item(Key=key)
        if any(table.get_item(Key=key, ConsistentRead=True).get('Item') for table, key in created):
            raise RuntimeError('synthetic records remain')
        print('synthetic source guard cleanup verified')


if __name__ == '__main__': main()
