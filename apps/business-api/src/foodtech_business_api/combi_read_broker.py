"""Private Combi read lease issuer; no configured/default or public HTTP path.

Actual IAM must independently restrict AssumeRole to the reviewed broker and
deny the Gateway. This module does not create that role or prove its policies.
It only consumes authenticated stored scopes and protected full history, and
narrows the Autopilot read baseline to four exact cooking-table partitions.
"""
import asyncio
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from foodtech_contracts.combi_read_scope import CombiReadScope
from foodtech_contracts.combi_read_handoff import CombiReadHandoff
from .combi_read_scope import authenticated_combi_read_scope
from .combi_prior_history import DynamoCombiPriorHistoryAuthority
from .prior_history_authority import PriorHistoryUnavailable
from .recipe_scope import RecipeScopeDenied

REGION = 'ap-southeast-2'
# Narrowed from the 0.4.0 Autopilot Combi source-reader baseline. Existing
# AWS-managed regional tables need no baseline KMS or replication wildcard.
READ_ACTIONS = ('dynamodb:GetItem', 'dynamodb:Query')


class CombiReadLease:
    __slots__ = ('__scope', '__expires', '__session', '__config', '__clock', '__closed', '__clients', '__last_time', '__role', '__assumed')

    def __init__(self, scope, expires, session, config, clock, *, read_role_arn, assumed_role_arn):
        self.__scope, self.__expires, self.__session = scope, expires, session
        self.__config, self.__clock, self.__closed, self.__clients = config, clock, False, []
        self.__last_time = clock()
        self.__role, self.__assumed = read_role_arn, assumed_role_arn

    def __repr__(self): return 'CombiReadLease()'
    def __reduce_ex__(self, _protocol): raise TypeError('combi read leases cannot be serialized')

    @property
    def scope(self): return self.__scope
    @property
    def expires_at(self): return self.__expires

    def _scoped_session(self, expected_scope):
        now = self.__clock()
        if self.__closed or type(expected_scope) is not CombiReadScope or expected_scope != self.__scope or \
                type(now) is not datetime or now.utcoffset() is None or now >= self.__expires or \
                now.timestamp() < self.scope.history.writer_verified_at or now < self.__last_time:
            raise RecipeScopeDenied('combi read lease expired or scope changed')
        self.__last_time = now
        credentials = self.__session.get_credentials()
        if credentials is None or credentials.method != 'explicit':
            raise RecipeScopeDenied('explicit combi lease credentials required')
        return self.__session

    def client(self, expected_scope):
        session = self._scoped_session(expected_scope)
        client = session.client('dynamodb', region_name=REGION, config=self.__config,
            endpoint_url=f'https://dynamodb.{REGION}.amazonaws.com')
        self.__clients.append(client)
        return client

    def _internal_handoff(self, expected_scope, *, execution_reader, history_authority):
        """Explicit private TLS encoding only, never a customer return value."""
        try:
            match = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-combi-source-read-([a-z0-9-]{1,32})', self.__role)
            if not match or type(history_authority) is not DynamoCombiPriorHistoryAuthority or \
                    history_authority.auth != 'foodtech-auth-sessions-'+match[2] or \
                    history_authority.cooking_arn != f'arn:aws:dynamodb:{REGION}:{match[1]}:table/foodtech-cooking-jobs-{match[2]}' or \
                    not callable(execution_reader): raise ValueError
            def revalidate():
                if not self.scope.matches_snapshot(execution_reader(self.scope.execution_id)) or \
                        history_authority.revalidate(self.scope.history) != self.scope.history:
                    raise ValueError
                return self._scoped_session(expected_scope)
            session = revalidate()
            frozen = session.get_credentials().get_frozen_credentials()
            revalidate()
            return CombiReadHandoff(self.scope, self.__role, self.__assumed, self.expires_at,
                frozen.access_key, frozen.secret_key, frozen.token, clock=self.__clock)
        except (ValueError, TypeError, AttributeError, ArithmeticError, PriorHistoryUnavailable, BotoCoreError, ClientError):
            raise RecipeScopeDenied('combi read handoff unavailable') from None

    def close(self):
        self.__closed = True
        for client in self.__clients:
            try: client.close()
            except (BotoCoreError, OSError): pass
        self.__clients.clear()
        # Local disposal does not revoke an already issued STS session. Every
        # consumer must still enforce live history/account and expiry guards.
        self.__session = None


class CombiReadBroker:
    def __init__(self, *, account, stage, read_role_arn, sts, session_factory=boto3.Session,
                 clock=lambda: datetime.now(timezone.utc)):
        if type(account) is not str or not re.fullmatch('[0-9]{12}', account) or \
                type(stage) is not str or not re.fullmatch('[a-z0-9-]{1,32}', stage) or \
                read_role_arn != f'arn:aws:iam::{account}:role/foodtech-combi-source-read-{stage}' or \
                sts is None or not callable(session_factory) or not callable(clock):
            raise ValueError('explicit separate combi read broker required')
        self.account, self.stage, self.role = account, stage, read_role_arn
        self.sts, self.session_factory, self.clock = sts, session_factory, clock
        self.config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True,
            proxies={}, account_id_endpoint_mode='disabled', retries={'mode': 'standard', 'total_max_attempts': 2})

    def __repr__(self): return 'CombiReadBroker()'

    def session_policy(self, scope):
        if type(scope) is not CombiReadScope:
            raise RecipeScopeDenied('typed combi read scope required')
        policy = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': list(READ_ACTIONS),
            'Resource': f'arn:aws:dynamodb:{REGION}:{self.account}:table/foodtech-cooking-jobs-{self.stage}',
            'Condition': {'ForAllValues:StringEquals': {'dynamodb:LeadingKeys': list(scope.read_partitions)},
                'Null': {'dynamodb:LeadingKeys': 'false'}, 'StringEquals': {'aws:RequestedRegion': REGION}}}]}
        text = json.dumps(policy, separators=(',', ':'), ensure_ascii=True)
        if len(text) > 2048: raise RecipeScopeDenied('combi session policy too large')
        return text

    def issue(self, scope, *, execution_reader, history_authority):
        policy = self.session_policy(scope)
        if type(history_authority) is not DynamoCombiPriorHistoryAuthority or \
                history_authority.auth != f'foodtech-auth-sessions-{self.stage}' or \
                history_authority.cooking_arn != f'arn:aws:dynamodb:{REGION}:{self.account}:table/foodtech-cooking-jobs-{self.stage}' or \
                not callable(execution_reader):
            raise RecipeScopeDenied('matching protected combi history authority required')
        try:
            def revalidate():
                now = self.clock()
                if type(now) is not datetime or now.utcoffset() is None or \
                        not scope.history.writer_verified_at <= now.timestamp() < scope.history.writer_valid_until or \
                        not scope.matches_snapshot(execution_reader(scope.execution_id)) or \
                        history_authority.revalidate(scope.history) != scope.history:
                    raise ValueError
                return now
            started = revalidate()
            session_name = 'combi-' + uuid4().hex
            response = self.sts.assume_role(RoleArn=self.role, RoleSessionName=session_name, DurationSeconds=900, Policy=policy)
            identity, credentials = response.get('AssumedRoleUser', {}), response.get('Credentials', {})
            expiration = credentials.get('Expiration')
            now = revalidate()
            arn = f'arn:aws:sts::{self.account}:assumed-role/foodtech-combi-source-read-{self.stage}/{session_name}'
            if now < started or identity.get('Arn') != arn or type(expiration) is not datetime or expiration.utcoffset() is None or \
                    not 30 <= (expiration-now).total_seconds() <= 930 or any(type(credentials.get(k)) is not str or
                        not 1 <= len(credentials[k]) <= 16384 for k in ('AccessKeyId', 'SecretAccessKey', 'SessionToken')):
                raise ValueError
            expires = min(expiration, datetime.fromtimestamp(scope.history.writer_valid_until, timezone.utc))
            if (expires-now).total_seconds() < 30: raise ValueError
            session = self.session_factory(region_name=REGION, aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'], aws_session_token=credentials['SessionToken'])
            if session.get_credentials() is None or session.get_credentials().method != 'explicit': raise ValueError
            return CombiReadLease(scope, expires, session, self.config, self.clock,
                read_role_arn=self.role, assumed_role_arn=identity['Arn'])
        except (ValueError, TypeError, AttributeError, KeyError, ArithmeticError, PriorHistoryUnavailable, BotoCoreError, ClientError):
            raise RecipeScopeDenied('verified combi read lease unavailable') from None


async def issue_authenticated_combi_reads(request, execution_id, *, broker, execution_reader, history_authority):
    if type(broker) is not CombiReadBroker: raise RecipeScopeDenied('explicit combi broker required')
    scope = await authenticated_combi_read_scope(request, execution_id,
        execution_reader=execution_reader, history_authority=history_authority)
    pending = asyncio.create_task(asyncio.to_thread(broker.issue, scope,
        execution_reader=execution_reader, history_authority=history_authority))
    try:
        lease = await asyncio.shield(pending)
    except BaseException:
        # Cancelling the await does not stop an SDK call in a worker thread.
        # Dispose its eventual unpublished result instead of losing ownership.
        def discard(task):
            if not task.cancelled():
                try: task.result().close()
                except Exception: pass
        pending.add_done_callback(discard)
        raise
    try:
        after = await authenticated_combi_read_scope(request, execution_id,
            execution_reader=execution_reader, history_authority=history_authority)
        if after != scope: raise RecipeScopeDenied('combi read scope changed')
        return lease
    except BaseException:
        lease.close()
        raise
