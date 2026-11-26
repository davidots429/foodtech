"""Trusted BFF issuance of key-bounded recipe reads; never an HTTP credential API.

Only issue_authenticated_reads may connect a customer request to issuance.
RecipeReadScope values alone are not authorization. Deployment must independently
restrict AssumeRole to the reviewed broker task role and deny it to the Gateway.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
import re
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .recipe_scope import (RecipeReadScope, RecipeScopeDenied, authenticated_recipe_scope,
                           revalidate_recipe_scope)
from .subject_identity import valid_subject
from .source_read_authority import DynamoSourceReadAuthority

REGION = 'ap-southeast-2'
# Retained from the Autopilot read baseline. Replication/KMS wildcard grants in
# that baseline are not needed for these existing AWS-managed regional tables.
READ_ACTIONS = ['dynamodb:GetItem', 'dynamodb:Query']


def _validate_scope(scope):
    if type(scope) is not RecipeReadScope or not valid_subject(scope.owner):
        raise RecipeScopeDenied('recipe read scope unavailable')
    try:
        if str(UUID(scope.execution_id)) != scope.execution_id or any(
                str(UUID(pk.rsplit('#', 1)[-1])) != pk.rsplit('#', 1)[-1] for pk in scope.job_partitions):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RecipeScopeDenied('recipe read scope unavailable') from None
    if not isinstance(scope.job_partitions, tuple) or len(scope.job_partitions) != 2 or \
            len(set(scope.job_partitions)) != 2 or any(
                not isinstance(pk, str) or not re.fullmatch(
                    r'SCOPE#USER#' + re.escape(scope.owner) + r'#JOB#[0-9a-f-]{36}', pk)
                for pk in scope.job_partitions):
        raise RecipeScopeDenied('recipe read scope unavailable')
    if not isinstance(scope.source_partitions, tuple) or len(scope.source_partitions) != 2 or \
            len(set(scope.source_partitions)) != 2 or any(
                not isinstance(pk, str) or not re.fullmatch(r'JEYUK#jeyuk-[A-Za-z0-9-]{1,121}', pk)
                for pk in scope.source_partitions):
        raise RecipeScopeDenied('recipe read scope unavailable')


class RecipeReadLease:
    """Private, in-memory capability. Never serialize or return to a browser."""
    __slots__ = ('__scope', '__expires', '__session', '__config')

    def __init__(self, scope, expires_at, session, config):
        self.__scope, self.__expires = scope, expires_at
        self.__session, self.__config = session, config

    @property
    def scope(self):
        return self.__scope

    @property
    def expires_at(self):
        return self.__expires

    def __repr__(self):
        return 'RecipeReadLease()'

    def __reduce_ex__(self, _protocol):
        raise TypeError('recipe read leases cannot be serialized')

    def resource(self, expected_scope):
        if expected_scope != self.scope or self.expires_at <= datetime.now(timezone.utc):
            raise RecipeScopeDenied('recipe read lease unavailable')
        return self.__session.resource('dynamodb', config=self.__config)


class RecipeReadBroker:
    """Internal issuance primitive; the authenticated wrapper is the entry point."""
    def __init__(self, *, account, stage, read_role_arn, sts, session_factory=boto3.Session, source_authority=None):
        if not isinstance(account, str) or not re.fullmatch(r'[0-9]{12}', account) or \
                not isinstance(stage, str) or not re.fullmatch(r'[a-z0-9-]{1,32}', stage) or \
                read_role_arn != f'arn:aws:iam::{account}:role/foodtech-recipe-source-read-{stage}':
            raise ValueError('explicit reviewed recipe read role required')
        self.account, self.stage, self.read_role_arn = account, stage, read_role_arn
        self.sts, self.session_factory = sts, session_factory
        self.source_authority = source_authority
        self.config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True,
            retries={'mode': 'standard', 'total_max_attempts': 2})

    def session_policy(self, scope):
        _validate_scope(scope)
        statements = []
        for table, keys in [
            ('cooking-jobs', scope.job_partitions + scope.source_partitions),
            ('data-flow', scope.source_partitions),
        ]:
            statements.append({'Effect': 'Allow', 'Action': READ_ACTIONS,
                'Resource': f'arn:aws:dynamodb:{REGION}:{self.account}:table/foodtech-{table}-{self.stage}',
                'Condition': {'ForAllValues:StringEquals': {'dynamodb:LeadingKeys': sorted(set(keys))},
                    'Null': {'dynamodb:LeadingKeys': 'false'},
                    'StringEquals': {'aws:RequestedRegion': REGION}}})
        text = json.dumps({'Version': '2012-10-17', 'Statement': statements}, separators=(',', ':'))
        if len(text) > 2048:
            raise RecipeScopeDenied('recipe read scope exceeds session policy limit')
        return text

    def issue(self, scope):
        policy = self.session_policy(scope)
        session_name = 'recipe-' + uuid4().hex
        response = self.sts.assume_role(RoleArn=self.read_role_arn, RoleSessionName=session_name,
            DurationSeconds=900, Policy=policy)
        # No caller-selected tags, policies, source identity, duration or role.
        identity = response.get('AssumedRoleUser', {})
        expected_arn = f'arn:aws:sts::{self.account}:assumed-role/foodtech-recipe-source-read-{self.stage}/{session_name}'
        credentials = response.get('Credentials', {})
        expires = credentials.get('Expiration')
        now = datetime.now(timezone.utc)
        if identity.get('Arn') != expected_arn or not isinstance(expires, datetime) or expires.tzinfo is None or \
                not 30 <= (expires - now).total_seconds() <= 930 or any(
                    not isinstance(credentials.get(key), str) or not 1 <= len(credentials[key]) <= 16384
                    for key in ('AccessKeyId', 'SecretAccessKey', 'SessionToken')):
            raise RecipeScopeDenied('recipe read credentials unavailable')
        session = self.session_factory(region_name=REGION, aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'], aws_session_token=credentials['SessionToken'])
        return RecipeReadLease(scope, expires, session, self.config)


def configured_broker():
    env = os.environ
    if env.get('FOODTECH_RECIPE_SOURCE_BROKER_ENABLED') != 'true' or env.get('AWS_EXECUTION_ENV') != 'AWS_ECS_FARGATE':
        raise RecipeScopeDenied('recipe read broker unavailable')
    stage = env.get('FOODTECH_STAGE', '')
    read_role = env.get('FOODTECH_RECIPE_SOURCE_READ_ROLE_ARN', '')
    match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-recipe-source-read-([a-z0-9-]{1,32})', read_role)
    caller = env.get('FOODTECH_RECIPE_BROKER_TASK_ROLE_ARN', '')
    if not match or match[2] != stage or not re.fullmatch(
            r'arn:aws:iam::' + match[1] + r':role/[A-Za-z0-9+=,.@_-]{1,64}', caller) or caller == read_role:
        raise RecipeScopeDenied('recipe read broker unavailable')
    if env.get('FOODTECH_AUTH_TABLE') != f'foodtech-auth-sessions-{stage}' or \
            env.get('FOODTECH_SOURCE_ACCESS_TABLE') != f'foodtech-source-access-{stage}':
        raise RecipeScopeDenied('recipe source authority unavailable')
    session = boto3.Session(region_name=REGION)
    credentials = session.get_credentials()
    if credentials is None or credentials.method != 'container-role':
        raise RecipeScopeDenied('recipe broker task credentials required')
    config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True,
        retries={'mode': 'standard', 'total_max_attempts': 2})
    sts = session.client('sts', config=config)
    identity = sts.get_caller_identity()
    role_name = caller.rsplit('/', 1)[1]
    if identity.get('Account') != match[1] or not re.fullmatch(
            r'arn:aws:sts::' + match[1] + ':assumed-role/' + re.escape(role_name) +
            r'/[A-Za-z0-9+=,.@_-]{2,64}', identity.get('Arn', '')):
        raise RecipeScopeDenied('unexpected recipe broker task identity')
    authority = DynamoSourceReadAuthority(auth_table=env['FOODTECH_AUTH_TABLE'],
        catalog_table=env['FOODTECH_SOURCE_ACCESS_TABLE'], client=session.client('dynamodb', config=config))
    return RecipeReadBroker(account=match[1], stage=stage, read_role_arn=read_role, sts=sts, source_authority=authority)


async def issue_authenticated_reads(request, execution_id, *, execution_reader, job_reader, broker,
                                    source_authority=None):
    """Reauthenticate around STS issuance; do not expose the resulting lease."""
    if type(broker) is not RecipeReadBroker:
        raise RecipeScopeDenied('recipe read broker unavailable')
    # A customer can name an unbound batch in a job request. That reverse
    # binding is not independent authority to disclose the source payload.
    authority = broker.source_authority if source_authority is None else source_authority
    authorize = getattr(authority, 'authorize_read', None)
    if not callable(authorize):
        raise RecipeScopeDenied('independent source authority unavailable')

    async def require_source_authority():
        try:
            allowed = await asyncio.to_thread(authorize, scope)
        except (BotoCoreError, ClientError, ValueError, TypeError, AttributeError):
            raise RecipeScopeDenied('source authority unavailable') from None
        if allowed is not True:
            raise RecipeScopeDenied('source authority unavailable')

    scope = await authenticated_recipe_scope(request, execution_id, execution_reader, job_reader)
    await require_source_authority()
    try:
        lease = await asyncio.to_thread(broker.issue, scope)
    except (BotoCoreError, ClientError, ValueError):
        raise RecipeScopeDenied('recipe read lease unavailable') from None
    # A revoke/change during issuance discards the capability before any read.
    await revalidate_recipe_scope(request, scope, execution_reader, job_reader)
    await require_source_authority()
    return lease
