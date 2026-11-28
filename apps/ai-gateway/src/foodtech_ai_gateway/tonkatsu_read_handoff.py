"""Receive fryer source delegation over the pinned internal TLS boundary."""
import asyncio
import hmac
import re
from dataclasses import replace
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from foodtech_contracts.combi_history_witness import history_integer
from foodtech_contracts.tonkatsu_read_handoff import CONTENT_TYPE, MAX_BYTES, ROLE_PATTERN, TonkatsuReadHandoff

from foodtech_contracts.tonkatsu_execution_binding import parse_tonkatsu_binding
from foodtech_contracts.tonkatsu_read_scope import TonkatsuReadScope

from .tonkatsu_transaction_conditions import TonkatsuTransactionConditions
from .tonkatsu_quality_inputs import observe_tonkatsu_quality
from .tonkatsu_source_observation import observe_scoped_tonkatsu_source

REGION = "ap-southeast-2"
TLS_PEER_KEY = "foodtech_tonkatsu_peer_sha256"


class TonkatsuHandoffDenied(ValueError):
    pass


def _close(client):
    try:
        client.close()
    except (BotoCoreError, ClientError, OSError):
        pass


class ReceivedTonkatsuReads:
    """Own the scoped DynamoDB client for one pending execution."""
    __slots__ = ("_frame", "_scope", "_resolve_scope", "_resource", "_stage")

    def __init__(self, frame, scope, resolve_scope, resource, stage):
        self._frame, self._scope, self._resolve_scope = frame, scope, resolve_scope
        self._resource, self._stage = resource, stage

    def __repr__(self):
        return "ReceivedTonkatsuReads()"

    def __reduce_ex__(self, protocol):
        raise TypeError("received fryer reads cannot be serialized")

    def _check_scope(self):
        if self._resource is None or self._resolve_scope() != self._scope:
            raise TonkatsuHandoffDenied("fryer execution or source scope changed")
        self._frame.validate(self._scope)

    def observe_inputs(self):
        self._check_scope()
        cooking = self._resource.Table(f"foodtech-cooking-jobs-{self._stage}")
        data = self._resource.Table(f"foodtech-data-flow-{self._stage}")
        owner = self._scope.history.history.owner
        result = []
        for pk, stream, reference in zip(self._scope.job_partitions, self._scope.history.streams,
                                       (self._scope.binding.current, self._scope.binding.prior), strict=True):
            job = cooking.get_item(Key={"pk": pk, "sk": "META"}, ConsistentRead=True).get("Item", {})
            if (job.get("pk") != pk or job.get("sk") != "META" or job.get("owner_subject") != owner
                or job.get("job_id") != reference.job_id
                or history_integer(job.get("version")) != reference.job_version):
                raise TonkatsuHandoffDenied("stored fryer job changed")
            observed = observe_scoped_tonkatsu_source(table=data, job=job, scoped_stream=stream,
                                                       expected_revision=reference.source_revision)
            quality = observe_tonkatsu_quality(cooking, job, observed)
            result.append(replace(observed, quality_summary=quality))
        self._check_scope()
        return tuple(result)

    def matches_stored(self, item, *, completed_calls):
        if self._resource is None or type(completed_calls) is not int or not 0 <= completed_calls <= 3:
            return False
        scope = self._scope
        try:
            return (
                item["pk"] == "EXEC#" + scope.execution_id and item["sk"] == "META"
                and item["execution_id"] == scope.execution_id
                and item["tenant_id"] == scope.history.history.owner
                and history_integer(item["version"], 1, 2**63 - 1) == scope.execution_version + completed_calls
                and item["state"] == ("RESERVED" if completed_calls else "PENDING")
                and (not completed_calls or item.get("reserved_provider") == "ollama")
                and (history_integer(item.get("retry_count"), 1, 2) == completed_calls - 1
                    if completed_calls > 1 else "retry_count" not in item)
                and parse_tonkatsu_binding(item["tonkatsu_binding"]) == scope.binding
                and item["feature"] == "RECIPE_CORRECTION" and item["data_classification"] == "SYNTHETIC"
                and item["job_id"] == scope.binding.current.job_id
                and history_integer(item["job_version"]) == scope.binding.current.job_version
                and item["prior_job_id"] == scope.binding.prior.job_id
                and history_integer(item["prior_job_version"]) == scope.binding.prior.job_version
                and item["estimated_cost_usd"] == 0 and type(item["estimated_cost_usd"]) is not bool
                and item["external_transfer_approved"] is False
                and all(item.get(key) is None for key in ("combi_binding", "jeyuk_source_revision",
                    "prior_source_revision", "comparison_id", "credential_id", "external_transfer_consent_id"))
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return False

    def guard_transaction(self, execution_update, *, execution_id, completed_calls, failure=False):
        # A failure can close its own reservation after expiry or source change.
        # Its EXEC predicate still requires the exact reserved state and binding.
        if self._resource is None or execution_id != self._scope.execution_id or (
            failure and completed_calls not in (1, 2, 3)
        ):
            raise TonkatsuHandoffDenied("fryer transaction scope unavailable")
        if not failure:
            self._frame.validate(self._scope)
        role = re.fullmatch(ROLE_PATTERN, self._frame.role)
        conditions = TonkatsuTransactionConditions(scope=self._scope, account=role[1], stage=role[2],
            clock=self._frame.live_timestamp)
        return (conditions.bind_execution_update(execution_update, completed_calls=completed_calls),
                [] if failure else conditions.source_checks())

    def verification_checks(self, *, execution_id, completed_calls):
        if self._resource is None or execution_id != self._scope.execution_id:
            raise TonkatsuHandoffDenied("fryer transaction scope unavailable")
        self._frame.validate(self._scope)
        role = re.fullmatch(ROLE_PATTERN, self._frame.role)
        conditions = TonkatsuTransactionConditions(scope=self._scope, account=role[1], stage=role[2],
            clock=self._frame.live_timestamp)
        return [
            *conditions.source_checks(),
            {"ConditionCheck": {
                "TableName": "foodtech-ai-execution-registry-" + role[2],
                "Key": {"pk": {"S": "EXEC#" + execution_id}, "sk": {"S": "META"}},
                **conditions.execution_predicate(completed_calls=completed_calls),
                "ReturnValuesOnConditionCheckFailure": "NONE",
            }},
        ]

    def close(self):
        if self._resource is not None:
            _close(self._resource.meta.client)
            self._resource = None
            self._frame = None


def _accept(raw, *, resolve_scope, expected_role, expected_role_id, session_factory, clock):
    sts = None
    resource = None
    try:
        role = re.fullmatch(ROLE_PATTERN, expected_role)
        if not role or not re.fullmatch(r"[A-Z0-9]{16,128}", expected_role_id):
            raise ValueError
        frame = TonkatsuReadHandoff._decode_from_internal_tls(raw, clock=clock)
        scope = resolve_scope()
        if type(scope) is not TonkatsuReadScope:
            raise ValueError
        frame.validate(scope)
        if frame.role != expected_role:
            raise ValueError
        config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True,
            proxies={}, account_id_endpoint_mode="disabled", retries={"mode": "standard", "total_max_attempts": 2})
        session = session_factory(region_name=REGION, **frame._sdk_credentials())
        credentials = session.get_credentials()
        if credentials is None or credentials.method != "explicit":
            raise ValueError
        sts_url = f"https://sts.{REGION}.amazonaws.com"
        sts = session.client("sts", region_name=REGION, config=config, endpoint_url=sts_url)
        if sts.meta.endpoint_url != sts_url:
            raise ValueError
        identity = sts.get_caller_identity()
        session_name = frame.assumed_role.rsplit("/", 1)[1]
        if (identity.get("Account"), identity.get("Arn"), identity.get("UserId")) != (
            role[1], frame.assumed_role, expected_role_id + ":" + session_name,
        ) or resolve_scope() != scope:
            raise ValueError
        frame.validate(scope)
        data_url = f"https://dynamodb.{REGION}.amazonaws.com"
        resource = session.resource("dynamodb", region_name=REGION, config=config, endpoint_url=data_url)
        if resource.meta.client.meta.endpoint_url != data_url:
            raise ValueError
        result = ReceivedTonkatsuReads(frame, scope, resolve_scope, resource, role[2])
        resource = None
        return result
    except (ValueError, TypeError, AttributeError, LookupError, ArithmeticError, BotoCoreError, ClientError):
        raise TonkatsuHandoffDenied("fryer handoff unavailable") from None
    finally:
        if sts is not None:
            _close(sts)
        if resource is not None:
            _close(resource.meta.client)


def require_tonkatsu_transport(request):
    expected = getattr(request.app.state, "tonkatsu_broker_peer_sha256", None)
    state = request.scope.get("state")
    received = state.get(TLS_PEER_KEY) if type(state) is dict else None
    if not (
        request.scope.get("scheme") == "https"
        and getattr(request.app.state, "mtls_listener", False) is True
        and getattr(request.app.state, "tonkatsu_broker_listener", False) is True
        and type(expected) is str and re.fullmatch(r"[a-f0-9]{64}", expected)
        and type(received) is str and hmac.compare_digest(expected, received)
    ):
        raise HTTPException(401, "internal mTLS authentication required", headers={"Cache-Control": "no-store"})


async def receive_internal_tonkatsu_handoff(request, *, resolve_scope, expected_role, expected_role_id,
                                              session_factory=boto3.Session,
                                              clock=lambda: datetime.now(timezone.utc)):
    """Read the credential body only after authenticating the internal listener."""
    require_tonkatsu_transport(request)
    raw = bytearray()
    try:
        length = request.headers.get("Content-Length")
        if request.headers.get("Content-Type") != CONTENT_TYPE or (
            request.headers.get("Content-Encoding", "identity") != "identity"
        ) or (length is not None and not re.fullmatch(r"[0-9]{1,5}", length)):
            raise TonkatsuHandoffDenied
        if length is not None and not 1 <= int(length) <= MAX_BYTES:
            raise TonkatsuHandoffDenied
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_BYTES:
                raise TonkatsuHandoffDenied
            raw.extend(chunk)
        if length is not None and len(raw) != int(length):
            raise TonkatsuHandoffDenied
        pending = asyncio.create_task(asyncio.to_thread(_accept, bytes(raw), resolve_scope=resolve_scope,
            expected_role=expected_role, expected_role_id=expected_role_id, session_factory=session_factory, clock=clock))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            def discard(task):
                if not task.cancelled() and task.exception() is None:
                    task.result().close()
            pending.add_done_callback(discard)
            raise
    except (TonkatsuHandoffDenied, ClientDisconnect):
        raise HTTPException(400, "TONKATSU_READ_HANDOFF_UNAVAILABLE", headers={"Cache-Control": "no-store"}) from None
    finally:
        raw[:] = b"\x00" * len(raw)
