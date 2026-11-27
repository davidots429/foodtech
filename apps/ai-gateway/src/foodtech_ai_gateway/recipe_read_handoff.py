"""Receive a bounded read lease over the dedicated internal mTLS listener.

No public route is mounted here. The opt-in internal invocation handler provides
a fresh authoritative execution snapshot and the independently reviewed
condition-only client. A matching STS role is not proof of its session policy;
the trusted issuer and real AWS allow/deny validation remain required.
"""
import asyncio
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from foodtech_contracts.recipe_read_handoff import CONTENT_TYPE, MAX_BYTES, RecipeReadHandoff
from .recipe_read_authorization import DynamoRecipeReadAuthorization
from .prior_history_conditions import DynamoPriorHistoryConditions
from .recipe_source_guard import BoundRecipeSourceGuard, DynamoRecipeSourceGuard

REGION = 'ap-southeast-2'


class RecipeHandoffDenied(Exception):
    pass


def _matches_snapshot(scope, snapshot):
    execution = getattr(snapshot, 'execution', None)
    if any(type(value) is not int for value in (getattr(snapshot, 'version', None),
            getattr(execution, 'job_version', None), getattr(execution, 'prior_job_version', None),
            getattr(execution, 'jeyuk_source_revision', None), getattr(execution, 'prior_source_revision', None))):
        return False
    return (getattr(snapshot, 'state', None), getattr(snapshot, 'version', None),
            getattr(execution, 'execution_id', None), getattr(execution, 'tenant_id', None),
            getattr(execution, 'feature', None), getattr(execution, 'job_version', None),
            getattr(execution, 'prior_job_version', None), getattr(execution, 'jeyuk_source_revision', None),
            getattr(execution, 'prior_source_revision', None)) == \
           (scope.execution_state, scope.execution_version, scope.execution_id, scope.owner, 'RECIPE_CORRECTION',
            *scope.job_versions, *scope.source_revisions) and scope.job_partitions == (
                f'SCOPE#USER#{scope.owner}#JOB#{getattr(execution, "job_id", None)}',
                f'SCOPE#USER#{scope.owner}#JOB#{getattr(execution, "prior_job_id", None)}')


def _accept(raw, *, snapshot, expected_read_role_arn, condition_client, session_factory=boto3.Session):
    try:
        handoff = RecipeReadHandoff._decode_from_internal_tls(raw)
        expected = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-recipe-source-read-([a-z0-9-]{1,32})',
            expected_read_role_arn) if isinstance(expected_read_role_arn, str) else None
        if not expected or handoff.read_role_arn != expected_read_role_arn or not _matches_snapshot(handoff.scope, snapshot):
            raise ValueError
        authorization = DynamoRecipeReadAuthorization(handoff.authorization, client=condition_client)
        history = (DynamoPriorHistoryConditions(handoff.history, auth_table=handoff.authorization.auth_table,
            cooking_table=f'foodtech-cooking-jobs-{expected[2]}', account_id=expected[1], client=condition_client)
            if handoff.history is not None else None)
        def authorized():
            return (history.verify(snapshot.execution, authorization, handoff.scope.source_partitions)
                    if history is not None else authorization.verify(snapshot.execution, handoff.scope.source_partitions))
        if not authorized():
            raise ValueError
        config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True,
            proxies={}, account_id_endpoint_mode='disabled', retries={'mode': 'standard', 'total_max_attempts': 2})
        session = session_factory(region_name=REGION, **handoff._sdk_credentials())
        credentials = session.get_credentials()
        if credentials is None or credentials.method != 'explicit':
            raise ValueError
        sts = session.client('sts', config=config)
        if sts.meta.endpoint_url != f'https://sts.{REGION}.amazonaws.com':
            raise ValueError
        identity = sts.get_caller_identity()
        if identity.get('Account') != expected[1] or identity.get('Arn') != handoff.assumed_role_arn or \
                not authorized():
            raise ValueError
        # Refresh local expiry checks after network calls, without falling back
        # to a profile or the Gateway's long-lived source-read credentials.
        handoff._validate()
        resource = session.resource('dynamodb', config=config)
        if resource.meta.client.meta.endpoint_url != f'https://dynamodb.{REGION}.amazonaws.com':
            raise ValueError
        guard = DynamoRecipeSourceGuard(f'foodtech-cooking-jobs-{expected[2]}',
            f'foodtech-data-flow-{expected[2]}', resource=resource)
        return BoundRecipeSourceGuard(guard, snapshot.execution, handoff.scope.job_partitions,
                                       handoff.scope.source_partitions, authorization, history)
    except (ValueError, TypeError, AttributeError, BotoCoreError, ClientError):
        raise RecipeHandoffDenied('recipe read handoff unavailable') from None


async def receive_internal_recipe_handoff(request, *, snapshot, expected_read_role_arn, condition_client,
                                          session_factory=boto3.Session):
    """Consume internal TLS bytes, never Pydantic errors containing credentials."""
    if request.scope.get('scheme') != 'https' or not getattr(request.app.state, 'mtls_listener', False):
        raise HTTPException(401, 'internal mTLS authentication required')
    if request.headers.get('Content-Type') != CONTENT_TYPE or request.headers.get('Content-Encoding', 'identity') != 'identity':
        raise HTTPException(400, 'RECIPE_READ_HANDOFF_UNAVAILABLE', headers={'Cache-Control': 'no-store'})
    length = request.headers.get('Content-Length')
    if length is not None and (not 1 <= len(length) <= 5 or not length.isascii() or not length.isdigit() or
                               not 1 <= int(length) <= MAX_BYTES):
        raise HTTPException(400, 'RECIPE_READ_HANDOFF_UNAVAILABLE', headers={'Cache-Control': 'no-store'})
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BYTES:
                raise RecipeHandoffDenied('recipe read handoff unavailable')
            raw.extend(chunk)
        if length is not None and len(raw) != int(length):
            raise RecipeHandoffDenied('recipe read handoff unavailable')
        return await asyncio.to_thread(_accept, bytes(raw), snapshot=snapshot,
            expected_read_role_arn=expected_read_role_arn, condition_client=condition_client, session_factory=session_factory)
    except (RecipeHandoffDenied, ClientDisconnect):
        raise HTTPException(400, 'RECIPE_READ_HANDOFF_UNAVAILABLE', headers={'Cache-Control': 'no-store'}) from None
    finally:
        raw[:] = b'\x00' * len(raw)
