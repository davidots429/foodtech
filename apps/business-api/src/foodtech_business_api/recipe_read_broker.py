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
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .recipe_scope import (RecipeReadScope, RecipeScopeDenied, authenticated_recipe_scope,
                           revalidate_recipe_scope)
from .source_read_authority import DynamoSourceReadAuthority
from foodtech_contracts.source_read_authorization import SourceReadAuthorization
from foodtech_contracts.prior_history_witness import PriorHistoryWitness
from .prior_history_authority import DynamoPriorHistoryAuthority, PriorHistoryUnavailable
from foodtech_contracts.recipe_read_scope import validate_recipe_read_scope

REGION = 'ap-southeast-2'
# Retained from the Autopilot read baseline. Replication/KMS wildcard grants in
# that baseline are not needed for these existing AWS-managed regional tables.
READ_ACTIONS = ['dynamodb:GetItem', 'dynamodb:Query']


def _validate_scope(scope):
    try:
        validate_recipe_read_scope(scope)
    except ValueError:
        raise RecipeScopeDenied('recipe read scope unavailable') from None


class RecipeReadLease:
    """Private capability. Only explicit internal TLS encoding is permitted."""
    __slots__ = ('__scope', '__expires', '__session', '__config', '__authorization', '__read_role', '__assumed_role', '__history', '__history_authority')

    def __init__(self, scope, expires_at, session, config, *, read_role_arn=None, assumed_role_arn=None):
        self.__scope, self.__expires = scope, expires_at
        self.__session, self.__config = session, config
        self.__authorization = None
        self.__history, self.__history_authority = None, None
        self.__read_role, self.__assumed_role = read_role_arn, assumed_role_arn

    @property
    def scope(self):
        return self.__scope

    @property
    def expires_at(self):
        return self.__expires

    @property
    def authorization(self):
        return self.__authorization

    def _bind_authorization(self, observation):
        if self.__authorization is not None or type(observation) is not SourceReadAuthorization or \
                (observation.subject, observation.execution_id, observation.auth_epoch, observation.source_partitions) != \
                (self.scope.owner, self.scope.execution_id, self.scope.auth_epoch, self.scope.source_partitions):
            raise RecipeScopeDenied('source authorization observation unavailable')
        self.__authorization = observation

    def _bind_history(self, witness, authority):
        _validate_scope(self.scope)
        if self.__history is not None or type(witness) is not PriorHistoryWitness or type(authority) is not DynamoPriorHistoryAuthority:
            raise RecipeScopeDenied('trusted history observation required')
        role = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-recipe-source-read-([a-z0-9-]{1,32})',
                            self.__read_role or '')
        if not role or authority.auth != 'foodtech-auth-sessions-' + role[2] or \
                authority.cooking_arn != f'arn:aws:dynamodb:{REGION}:{role[1]}:table/foodtech-cooking-jobs-{role[2]}' or \
                (witness.owner, witness.auth_epoch, witness.job_versions, witness.current_job_id, witness.prior_job_id) != \
                (self.scope.owner, self.scope.auth_epoch, self.scope.job_versions,
                 *(key.rsplit('#', 1)[-1] for key in self.scope.job_partitions)):
            raise RecipeScopeDenied('history read scope changed')
        try:
            if authority.revalidate(witness) != witness:
                raise ValueError
        except (ValueError, PriorHistoryUnavailable, BotoCoreError, ClientError):
            raise RecipeScopeDenied('history observation unavailable') from None
        self.__history, self.__history_authority = witness, authority

    def __repr__(self):
        return 'RecipeReadLease()'

    def __reduce_ex__(self, _protocol):
        raise TypeError('recipe read leases cannot be serialized')

    def resource(self, expected_scope):
        if expected_scope != self.scope or self.expires_at <= datetime.now(timezone.utc):
            raise RecipeScopeDenied('recipe read lease unavailable')
        return self.__session.resource('dynamodb', config=self.__config)

    def _internal_handoff(self):
        from foodtech_contracts.recipe_read_handoff import RecipeReadHandoff
        if self.authorization is None:
            raise RecipeScopeDenied('verified source grant observation required')
        try:
            if self.__history is not None and self.__history_authority.revalidate(self.__history) != self.__history:
                raise ValueError
            credentials = self.__session.get_credentials()
            if credentials is None or credentials.method != 'explicit':
                raise ValueError('explicit scoped credentials required')
            frozen = credentials.get_frozen_credentials()
            return RecipeReadHandoff(self.scope, self.authorization, self.__read_role, self.__assumed_role,
                self.expires_at, frozen.access_key, frozen.secret_key, frozen.token, history=self.__history)
        except (ValueError, TypeError, AttributeError, PriorHistoryUnavailable, BotoCoreError, ClientError):
            raise RecipeScopeDenied('recipe read handoff unavailable') from None


class RecipeReadBroker:
    """Internal issuance primitive; the authenticated wrapper is the entry point."""
    def __init__(self, *, account, stage, read_role_arn, sts, session_factory=boto3.Session, source_authority=None,
                 cooking_resource=None):
        if not isinstance(account, str) or not re.fullmatch(r'[0-9]{12}', account) or \
                not isinstance(stage, str) or not re.fullmatch(r'[a-z0-9-]{1,32}', stage) or \
                read_role_arn != f'arn:aws:iam::{account}:role/foodtech-recipe-source-read-{stage}':
            raise ValueError('explicit reviewed recipe read role required')
        self.account, self.stage, self.read_role_arn = account, stage, read_role_arn
        self.sts, self.session_factory = sts, session_factory
        self.source_authority = source_authority
        self.cooking_resource = cooking_resource
        self.config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True,
            proxies={}, account_id_endpoint_mode='disabled',
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
        return RecipeReadLease(scope, expires, session, self.config,
                               read_role_arn=self.read_role_arn, assumed_role_arn=identity['Arn'])


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
        proxies={}, account_id_endpoint_mode='disabled',
        retries={'mode': 'standard', 'total_max_attempts': 2})
    sts = session.client('sts', config=config, endpoint_url=f'https://sts.{REGION}.amazonaws.com')
    identity = sts.get_caller_identity()
    role_name = caller.rsplit('/', 1)[1]
    if identity.get('Account') != match[1] or not re.fullmatch(
            r'arn:aws:sts::' + match[1] + ':assumed-role/' + re.escape(role_name) +
            r'/[A-Za-z0-9+=,.@_-]{2,64}', identity.get('Arn', '')):
        raise RecipeScopeDenied('unexpected recipe broker task identity')
    authority = DynamoSourceReadAuthority(auth_table=env['FOODTECH_AUTH_TABLE'],
        catalog_table=env['FOODTECH_SOURCE_ACCESS_TABLE'], client=session.client('dynamodb', config=config,
            endpoint_url=f'https://dynamodb.{REGION}.amazonaws.com'))
    return RecipeReadBroker(account=match[1], stage=stage, read_role_arn=read_role, sts=sts, source_authority=authority,
                            cooking_resource=session.resource('dynamodb', config=config,
                                endpoint_url=f'https://dynamodb.{REGION}.amazonaws.com'))


async def issue_authenticated_reads(request, execution_id, *, execution_reader, job_reader, broker,
                                    source_authority=None):
    """Reauthenticate around STS issuance; do not expose the resulting lease."""
    if type(broker) is not RecipeReadBroker:
        raise RecipeScopeDenied('recipe read broker unavailable')
    # A customer can name an unbound batch in a job request. That reverse
    # binding is not independent authority to disclose the source payload.
    authority = broker.source_authority if source_authority is None else source_authority
    authorize = getattr(authority, 'authorize_read', None)
    snapshotter = getattr(authority, 'authorization_snapshot', None)
    if not callable(authorize):
        raise RecipeScopeDenied('independent source authority unavailable')

    async def require_source_authority():
        try:
            allowed = await asyncio.to_thread(snapshotter if callable(snapshotter) else authorize, scope)
        except (BotoCoreError, ClientError, ValueError, TypeError, AttributeError):
            raise RecipeScopeDenied('source authority unavailable') from None
        if callable(snapshotter):
            if type(allowed) is not SourceReadAuthorization:
                raise RecipeScopeDenied('source authority unavailable')
        elif allowed is not True:
            raise RecipeScopeDenied('source authority unavailable')
        return allowed

    scope = await authenticated_recipe_scope(request, execution_id, execution_reader, job_reader)
    before_authorization = await require_source_authority()
    try:
        lease = await asyncio.to_thread(broker.issue, scope)
    except (BotoCoreError, ClientError, ValueError):
        raise RecipeScopeDenied('recipe read lease unavailable') from None
    # A revoke/change during issuance discards the capability before any read.
    await revalidate_recipe_scope(request, scope, execution_reader, job_reader)
    after_authorization = await require_source_authority()
    if after_authorization != before_authorization:
        raise RecipeScopeDenied('source authorization changed')
    if type(after_authorization) is SourceReadAuthorization:
        lease._bind_authorization(after_authorization)
    return lease


async def bind_authenticated_recipe_history(request, lease, *, authority, execution_reader, job_reader):
    """Attach server-observed history after fresh customer/CSRF/scope checks.

    No public route or attestation issuer is created; the authority must read
    independently protected, actually verified writer/coverage records.
    """
    from .auth import _session, _csrf
    if type(lease) is not RecipeReadLease or type(authority) is not DynamoPriorHistoryAuthority:
        raise RecipeScopeDenied('trusted recipe history boundary required')
    session = await _session(request, 'customer')
    _csrf(request, session)
    await revalidate_recipe_scope(request, lease.scope, execution_reader, job_reader)
    try:
        witness = await asyncio.to_thread(authority.observe, owner=lease.scope.owner, auth_epoch=lease.scope.auth_epoch,
            current_job_id=lease.scope.job_partitions[0].rsplit('#', 1)[-1],
            prior_job_id=lease.scope.job_partitions[1].rsplit('#', 1)[-1], job_versions=lease.scope.job_versions)
        await revalidate_recipe_scope(request, lease.scope, execution_reader, job_reader)
        await asyncio.to_thread(lease._bind_history, witness, authority)
        await revalidate_recipe_scope(request, lease.scope, execution_reader, job_reader)
        return lease
    except (ValueError, PriorHistoryUnavailable, BotoCoreError, ClientError):
        raise RecipeScopeDenied('recipe history observation unavailable') from None
