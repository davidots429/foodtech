"""Internal, short-lived source-read delegation for a verified fryer execution."""
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from foodtech_contracts.tonkatsu_read_handoff import TonkatsuReadHandoff, scope_sha256

from .recipe_scope import RecipeScopeDenied
from .tonkatsu_read_scope import DynamoTonkatsuReadScope, TonkatsuReadScope

REGION = "ap-southeast-2"


def _time(clock):
    now = clock()
    if type(now) is not datetime or now.utcoffset() is None:
        raise ValueError("invalid fryer lease clock")
    return now


def _current(resolver, scope, now):
    witness = scope.history
    if not witness.writer_verified_at <= now.timestamp() < witness.writer_valid_until:
        raise RecipeScopeDenied("fryer history attestation expired")
    if resolver.resolve(owner=witness.history.owner, epoch=scope.binding.auth_epoch,
                        execution_id=scope.execution_id) != scope:
        raise RecipeScopeDenied("fryer scope changed")


class TonkatsuReadLease:
    __slots__ = ("_scope", "_expires", "__session", "_config", "_clock", "_resolver", "_clients", "_last_time", "_role", "_assumed_role")

    def __init__(self, scope, expires, session, config, clock, resolver, issued_at, *, role, assumed_role):
        self._scope, self._expires, self.__session = scope, expires, session
        self._config, self._clock, self._resolver = config, clock, resolver
        self._clients = []
        self._last_time = issued_at
        self._role, self._assumed_role = role, assumed_role

    def __repr__(self):
        return "TonkatsuReadLease()"

    def __reduce_ex__(self, protocol):
        raise TypeError("fryer leases cannot be serialized")

    @property
    def expires_at(self):
        return self._expires

    def _live_session(self, expected_scope):
        try:
            now = _time(self._clock)
            if (
                self.__session is None or type(expected_scope) is not TonkatsuReadScope
                or expected_scope != self._scope or not self._last_time <= now < self._expires
            ):
                raise ValueError("fryer lease unavailable")
            _current(self._resolver, self._scope, now)
            observed_at = _time(self._clock)
            if not now <= observed_at < self._expires:
                raise ValueError("fryer lease expired during verification")
            self._last_time = observed_at
            return self.__session
        except (ValueError, TypeError, AttributeError, OverflowError, BotoCoreError, ClientError):
            raise RecipeScopeDenied("fryer lease unavailable") from None

    def resource(self, expected_scope):
        session = self._live_session(expected_scope)
        resource = session.resource("dynamodb", region_name=REGION, config=self._config,
            endpoint_url=f"https://dynamodb.{REGION}.amazonaws.com")
        self._clients.append(resource.meta.client)
        return resource

    def _internal_handoff(self, expected_scope):
        session = self._live_session(expected_scope)
        credentials = session.get_credentials()
        if credentials is None or credentials.method != "explicit":
            raise RecipeScopeDenied("explicit fryer lease credentials required")
        frozen = credentials.get_frozen_credentials()
        handoff = TonkatsuReadHandoff(scope_digest=scope_sha256(expected_scope),
            role=self._role, assumed_role=self._assumed_role, expires=self._expires,
            credentials=(frozen.access_key, frozen.secret_key, frozen.token), clock=self._clock)
        handoff.validate(expected_scope)
        return handoff

    def close(self):
        self.__session = None
        for client in self._clients:
            try:
                client.close()
            except (BotoCoreError, OSError):
                pass
        self._clients.clear()


class TonkatsuReadBroker:
    def __init__(self, *, account, stage, read_role_arn, sts, resolver, session_factory=boto3.Session,
                 clock=lambda: datetime.now(timezone.utc)):
        expected_role = f"arn:aws:iam::{account}:role/foodtech-tonkatsu-source-read-{stage}"
        if (
            type(account) is not str or not re.fullmatch(r"[0-9]{12}", account)
            or type(stage) is not str or not re.fullmatch(r"[a-z0-9-]{1,32}", stage)
            or read_role_arn != expected_role or sts is None or type(resolver) is not DynamoTonkatsuReadScope
            or resolver.history.cooking_arn != self._arn(account, "cooking-jobs", stage)
            or resolver.history.auth != f"foodtech-auth-sessions-{stage}"
            or not callable(clock) or not callable(session_factory)
        ):
            raise ValueError("explicit same-stage fryer role and scope resolver required")
        self.account, self.stage, self.role = account, stage, read_role_arn
        self.sts, self.resolver, self.session_factory, self.clock = sts, resolver, session_factory, clock
        self.config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True,
            proxies={}, account_id_endpoint_mode="disabled", retries={"mode": "standard", "total_max_attempts": 2})

    @staticmethod
    def _arn(account, table, stage):
        return f"arn:aws:dynamodb:{REGION}:{account}:table/foodtech-{table}-{stage}"

    def session_policy(self, scope):
        if type(scope) is not TonkatsuReadScope:
            raise RecipeScopeDenied("typed fryer scope required")
        # Use the existing recipe reader's action baseline with exact fryer keys.
        streams = scope.history.streams
        statements = []
        for table, keys in (("cooking-jobs", scope.job_partitions + streams), ("data-flow", streams)):
            statements.append({
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem", "dynamodb:Query"],
                "Resource": self._arn(self.account, table, self.stage),
                "Condition": {
                    "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": list(keys)},
                    "Null": {"dynamodb:LeadingKeys": "false"},
                    "StringEquals": {"aws:RequestedRegion": REGION},
                },
            })
        policy = json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))
        if len(policy) > 2048:
            raise RecipeScopeDenied("fryer session policy exceeds length limit")
        return policy

    def issue(self, *, owner, epoch, execution_id):
        """The caller supplies authenticated identity and the stored execution UUID."""
        scope = self.resolver.resolve(owner=owner, epoch=epoch, execution_id=execution_id)
        try:
            started = _time(self.clock)
            policy = self.session_policy(scope)
            if not scope.history.writer_verified_at <= started.timestamp() <= scope.history.writer_valid_until - 30:
                raise ValueError("fryer history attestation too close to expiration")
            name = "tonkatsu-" + uuid4().hex
            response = self.sts.assume_role(RoleArn=self.role, RoleSessionName=name, DurationSeconds=900, Policy=policy)
            _current(self.resolver, scope, _time(self.clock))
            now = _time(self.clock)
            arn = f"arn:aws:sts::{self.account}:assumed-role/foodtech-tonkatsu-source-read-{self.stage}/{name}"
            credentials = response["Credentials"]
            expires = credentials["Expiration"]
            if (
                now < started or response["AssumedRoleUser"]["Arn"] != arn
                or type(expires) is not datetime or expires.utcoffset() is None
                or not 30 <= (expires - now).total_seconds() <= 930
                or any(type(credentials.get(key)) is not str or not 1 <= len(credentials[key]) <= 16384
                       for key in ("AccessKeyId", "SecretAccessKey", "SessionToken"))
            ):
                raise ValueError("invalid fryer role response")
            expires = min(expires, datetime.fromtimestamp(scope.history.writer_valid_until, timezone.utc))
            if (expires - now).total_seconds() < 30:
                raise ValueError("fryer lease expired during verification")
            session = self.session_factory(region_name=REGION, aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"], aws_session_token=credentials["SessionToken"])
            explicit = session.get_credentials()
            if explicit is None or explicit.method != "explicit":
                raise ValueError("explicit fryer lease credentials required")
            return scope, TonkatsuReadLease(scope, expires, session, self.config, self.clock, self.resolver, now,
                                                  role=self.role, assumed_role=arn)
        except (ValueError, TypeError, AttributeError, KeyError, ArithmeticError, BotoCoreError, ClientError):
            raise RecipeScopeDenied("verified fryer read lease unavailable") from None
