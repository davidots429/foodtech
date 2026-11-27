"""Opt-in local recipe execution with request-scoped source capabilities."""
import asyncio
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .policy import Caller, Feature
from .registry import DynamoRegistry
from .recipe_read_handoff import receive_internal_recipe_handoff
from .service import GatewayService


class RecipeRuntimeUnavailable(Exception):
    pass


def configured_recipe_conditions(registry):
    """Use the verified Gateway task identity only for condition checks."""
    env = os.environ
    stage = env.get('FOODTECH_STAGE', '')
    role = env.get('FOODTECH_RECIPE_GATEWAY_TASK_ROLE_ARN', '')
    match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_-]{1,64})', role)
    read_role = env.get('FOODTECH_RECIPE_SOURCE_READ_ROLE_ARN', '')
    if env.get('FOODTECH_RECIPE_INVOCATION_ENABLED') != 'true' or \
            env.get('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') != 'true' or \
            env.get('AWS_EXECUTION_ENV') != 'AWS_ECS_FARGATE' or \
            not re.fullmatch('[a-z0-9-]{1,32}', stage) or not match or \
            read_role != f'arn:aws:iam::{match[1]}:role/foodtech-recipe-source-read-{stage}' or role == read_role or \
            type(registry) is not DynamoRegistry or registry.require_principal is not True or \
            registry.table_name != f'foodtech-ai-execution-registry-{stage}' or \
            registry.auth_table != f'foodtech-auth-sessions-{stage}' or \
            env.get('FOODTECH_AUTH_TABLE') != registry.auth_table:
        raise RecipeRuntimeUnavailable('recipe runtime unavailable')
    try:
        session = boto3.Session(region_name='ap-southeast-2')
        credentials = session.get_credentials()
        if credentials is None or credentials.method != 'container-role':
            raise ValueError
        config = Config(connect_timeout=5, read_timeout=15, proxies={}, ignore_configured_endpoint_urls=True,
                        account_id_endpoint_mode='disabled', retries={'mode': 'standard', 'total_max_attempts': 2})
        sts = session.client('sts', config=config, endpoint_url='https://sts.ap-southeast-2.amazonaws.com')
        try:
            identity = sts.get_caller_identity()
        finally:
            sts.close()
        if identity.get('Account') != match[1] or not re.fullmatch(
                r'arn:aws:sts::' + match[1] + ':assumed-role/' + re.escape(match[2]) + r'/[A-Za-z0-9+=,.@_-]{2,64}',
                identity.get('Arn', '')):
            raise ValueError
        client = session.client('dynamodb', config=config, endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        resource = session.resource('dynamodb', config=config, endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        observed_registry = DynamoRegistry(registry.table_name, registry.period, resource=resource, client=client,
                                           local_models=registry.local_models, auth_table=registry.auth_table)
        return read_role, client, observed_registry
    except (ValueError, TypeError, AttributeError, BotoCoreError, ClientError):
        raise RecipeRuntimeUnavailable('recipe runtime unavailable') from None


async def invoke_observed_recipe(request, execution_id, *, service, ollama):
    """The caller authenticates mTLS before entering; no browser inputs are accepted."""
    identifier = str(execution_id)
    if os.getenv('FOODTECH_RECIPE_INVOCATION_ENABLED') != 'true':
        raise HTTPException(503, 'RECIPE_INVOCATION_UNAVAILABLE', headers={'Cache-Control': 'no-store'})
    client, registry = None, None
    try:
        read_role, client, registry = await asyncio.to_thread(configured_recipe_conditions, service.registry)
        snapshot = await asyncio.to_thread(registry.execution_snapshot, identifier)
        execution = getattr(snapshot, 'execution', None)
        if not execution or execution.execution_id != identifier or execution.feature != Feature.RECIPE_CORRECTION:
            raise HTTPException(404, 'EXECUTION_NOT_FOUND', headers={'Cache-Control': 'no-store'})
        guard = await receive_internal_recipe_handoff(request, snapshot=snapshot,
                    expected_read_role_arn=read_role, condition_client=client)
        if await asyncio.to_thread(registry.execution_snapshot, identifier) != snapshot:
            raise HTTPException(409, 'EXECUTION_CHANGED', headers={'Cache-Control': 'no-store'})
        # Never replace the shared service's guard: concurrent tenants get separate services.
        scoped = GatewayService(registry, recipe_source_guard=guard)
        outcome = await scoped.execute_observed_local_recipe(Caller('business-api', execution.tenant_id),
                                                            identifier, ollama=ollama)
        return JSONResponse({'execution_id': outcome.execution_id, 'state': outcome.state,
                             'response_text': outcome.response_text, 'error_code': outcome.error_code},
                            headers={'Cache-Control': 'no-store'})
    except (RecipeRuntimeUnavailable, BotoCoreError, ClientError):
        raise HTTPException(503, 'RECIPE_INVOCATION_UNAVAILABLE', headers={'Cache-Control': 'no-store'}) from None
    finally:
        if client is not None:
            await asyncio.to_thread(client.close)
        if registry is not None:
            await asyncio.to_thread(registry.resource.meta.client.close)
