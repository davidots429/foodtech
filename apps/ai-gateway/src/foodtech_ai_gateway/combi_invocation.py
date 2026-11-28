"""Dedicated broker-mTLS Combi invocation; no browser-supplied model inputs."""
import asyncio
from datetime import datetime, timezone
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .combi_read_handoff import require_combi_transport, receive_internal_combi_handoff
from .policy import Caller, Execution, ExecutionSnapshot, Feature
from .registry import DynamoRegistry
from .service import GatewayService


class CombiRuntimeUnavailable(Exception): pass


def _close_runtime(runtime):
    if runtime is None: return
    _, _, client, registry = runtime
    seen = set()
    for value in (client, getattr(getattr(registry.resource, 'meta', None), 'client', None)):
        if value is None or id(value) in seen: continue
        seen.add(id(value))
        try: value.close()
        except (BotoCoreError, ClientError, OSError): pass


def configured_combi_runtime():
    """Verify the Gateway container identity; source reads use the separate lease."""
    return configured_recipe_runtime('combi')


def configured_recipe_runtime(recipe):
    if recipe not in ('combi', 'tonkatsu'):
        raise CombiRuntimeUnavailable(f'{recipe} runtime unavailable')
    prefix = 'FOODTECH_' + recipe.upper() + '_'
    env = os.environ
    stage = env.get('FOODTECH_STAGE', '')
    role = env.get(prefix+'GATEWAY_TASK_ROLE_ARN', '')
    role_id = env.get(prefix+'GATEWAY_TASK_ROLE_ID', '')
    read_role_id = env.get(prefix+'SOURCE_READ_ROLE_ID', '')
    match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_-]{1,64})', role)
    if env.get(prefix+'INVOCATION_ENABLED') != 'true' or env.get(prefix+'LISTENER_ENABLED') != 'true' or \
            env.get('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') != 'true' or env.get('AWS_EXECUTION_ENV') != 'AWS_ECS_FARGATE' or \
            not re.fullmatch('[a-z0-9-]{1,32}', stage) or not match or \
            not re.fullmatch('[A-Z0-9]{16,128}', role_id) or not re.fullmatch('[A-Z0-9]{16,128}', read_role_id) or \
            env.get('FOODTECH_AI_REGISTRY_TABLE') != 'foodtech-ai-execution-registry-'+stage or \
            env.get('FOODTECH_AUTH_TABLE') != 'foodtech-auth-sessions-'+stage:
        raise CombiRuntimeUnavailable(f'{recipe} runtime unavailable')
    read_role = f'arn:aws:iam::{match[1]}:role/foodtech-{recipe}-source-read-{stage}'
    if env.get(prefix+'SOURCE_READ_ROLE_ARN') != read_role or role == read_role or role_id == read_role_id:
        raise CombiRuntimeUnavailable(f'{recipe} runtime unavailable')
    client, resource, sts = None, None, None
    try:
        session = boto3.Session(region_name='ap-southeast-2')
        credentials = session.get_credentials()
        if credentials is None or credentials.method != 'container-role': raise ValueError
        config = Config(connect_timeout=5, read_timeout=15, proxies={}, ignore_configured_endpoint_urls=True,
            account_id_endpoint_mode='disabled', retries={'mode':'standard','total_max_attempts':2})
        sts = session.client('sts', region_name='ap-southeast-2', config=config,
            endpoint_url='https://sts.ap-southeast-2.amazonaws.com')
        if sts.meta.endpoint_url != 'https://sts.ap-southeast-2.amazonaws.com': raise ValueError
        identity = sts.get_caller_identity()
        assumed = re.fullmatch(r'arn:aws:sts::'+match[1]+':assumed-role/'+re.escape(match[2])+r'/([A-Za-z0-9+=,.@_-]{2,64})',
            identity.get('Arn', ''))
        if identity.get('Account') != match[1] or not assumed or identity.get('UserId') != role_id+':'+assumed[1]:
            raise ValueError
        client = session.client('dynamodb', region_name='ap-southeast-2', config=config,
            endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        resource = session.resource('dynamodb', region_name='ap-southeast-2', config=config,
            endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        if client.meta.endpoint_url != 'https://dynamodb.ap-southeast-2.amazonaws.com' or \
                resource.meta.client.meta.endpoint_url != 'https://dynamodb.ap-southeast-2.amazonaws.com': raise ValueError
        registry = DynamoRegistry(env['FOODTECH_AI_REGISTRY_TABLE'], datetime.now(timezone.utc).strftime('%Y-%m'),
            resource=resource, client=client, auth_table=env['FOODTECH_AUTH_TABLE'], local_models={'llama3.1:8b'})
        return read_role, read_role_id, client, registry
    except (ValueError, TypeError, AttributeError, BotoCoreError, ClientError, OSError):
        seen = set()
        for value in (client, getattr(getattr(resource, 'meta', None), 'client', None)):
            if value is not None and id(value) not in seen:
                seen.add(id(value))
                try: value.close()
                except (BotoCoreError, ClientError, OSError): pass
        raise CombiRuntimeUnavailable(f'{recipe} runtime unavailable') from None
    finally:
        if sts is not None:
            try: sts.close()
            except (BotoCoreError, ClientError, OSError): pass


async def invoke_observed_combi(request, execution_id, *, ollama):
    # Authenticate before registry initialization, reads or credential frame parsing.
    require_combi_transport(request)
    if os.getenv('FOODTECH_COMBI_INVOCATION_ENABLED') != 'true':
        raise HTTPException(503, 'COMBI_INVOCATION_UNAVAILABLE', headers={'Cache-Control':'no-store'})
    identifier = str(execution_id)
    runtime, guard = None, None
    try:
        pending = asyncio.create_task(asyncio.to_thread(configured_combi_runtime))
        try: runtime = await asyncio.shield(pending)
        except BaseException:
            def discard(task):
                if not task.cancelled():
                    try: _close_runtime(task.result())
                    except Exception: pass
            pending.add_done_callback(discard)
            raise
        role, role_id, client, registry = runtime
        snapshot = await asyncio.to_thread(registry.execution_snapshot, identifier)
        if type(snapshot) is not ExecutionSnapshot or type(snapshot.execution) is not Execution or \
                snapshot.execution.execution_id != identifier or snapshot.execution.feature != Feature.RECIPE_CORRECTION or \
                snapshot.execution.combi_binding is None:
            raise HTTPException(404, 'EXECUTION_NOT_FOUND', headers={'Cache-Control':'no-store'})
        if snapshot.state != 'PENDING':
            raise HTTPException(409, 'EXECUTION_CHANGED', headers={'Cache-Control':'no-store'})
        guard = await receive_internal_combi_handoff(request, snapshot=snapshot, expected_read_role_arn=role,
            expected_read_role_id=role_id, condition_client=client)
        if await asyncio.to_thread(registry.execution_snapshot, identifier) != snapshot:
            raise HTTPException(409, 'EXECUTION_CHANGED', headers={'Cache-Control':'no-store'})
        service = GatewayService(registry)
        outcome = await service.execute_observed_combi_recipe(Caller('business-api', snapshot.execution.tenant_id),
            identifier, combi_source_guard=guard, ollama=ollama)
        return JSONResponse({'execution_id':outcome.execution_id, 'state':outcome.state,
            'response_text':outcome.response_text, 'error_code':outcome.error_code}, headers={'Cache-Control':'no-store'})
    except (CombiRuntimeUnavailable, BotoCoreError, ClientError):
        raise HTTPException(503, 'COMBI_INVOCATION_UNAVAILABLE', headers={'Cache-Control':'no-store'}) from None
    finally:
        if guard is not None: guard.close()
        await asyncio.to_thread(_close_runtime, runtime)
