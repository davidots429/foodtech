"""Private Combi receiver preparation; no route or provider activation here.

The dedicated listener must authenticate the reviewed business/broker client at
the TLS handshake. Transport and STS role/ID matching still do not prove an
inline session policy: reviewed exclusive issuer deployment and actual AWS
least-privilege validation remain independent prerequisites.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import re
import hmac

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from foodtech_contracts.combi_read_handoff import CombiReadHandoff, CONTENT_TYPE, MAX_BYTES, TLS_PEER_STATE_KEY, TLS_PIN_APP_STATE_KEY
from .combi_history_conditions import DynamoCombiHistoryConditions
from .combi_source_guard import DynamoCombiSourceGuard
from .policy import Execution, ExecutionSnapshot

REGION = 'ap-southeast-2'


class CombiHandoffDenied(Exception): pass


def _close(clients):
    seen = set()
    for client in clients:
        if id(client) in seen: continue
        seen.add(id(client))
        close = getattr(client, 'close', None)
        if callable(close):
            try: close()
            except (BotoCoreError, ClientError, OSError): pass


class ReceivedCombiReads:
    """Own scoped clients for one stored execution, never serialize credentials."""
    __slots__ = ('__handoff', '__execution', '__guard', '__clients', '__closed')

    def __init__(self, handoff, execution, guard, clients):
        self.__handoff, self.__execution, self.__guard = handoff, execution, guard
        self.__clients, self.__closed = clients, False

    def __repr__(self): return 'ReceivedCombiReads()'
    def __reduce_ex__(self, _protocol): raise TypeError('received combi reads cannot be serialized')

    def _live(self):
        if self.__closed: raise ValueError('combi reads closed')
        self.__handoff._validate()

    def verify(self, execution):
        try:
            self._live()
            if type(execution) is not Execution or execution != self.__execution or not self.__guard.verify(execution): return False
            self._live()
            return True
        except (ValueError, TypeError, AttributeError, ArithmeticError, BotoCoreError, ClientError): return False

    def matches_transition(self, execution, *, version, completed_calls):
        """Bind one PENDING lease to the expected reserve/retry progression."""
        self._live()
        scope = self.__handoff.scope
        return type(execution) is Execution and execution == self.__execution and \
            scope.execution_state == 'PENDING' and type(version) is int and \
            type(completed_calls) is int and 0 <= completed_calls <= 3 and \
            version == scope.execution_version + completed_calls and version < 2**63

    def observe_inputs(self):
        self._live()
        result = self.__guard.observe_inputs(self.__execution)
        self._live()
        return result

    def settlement_checks(self, existing_auth_check):
        self._live()
        return self.__guard.settlement_checks(self.__execution, existing_auth_check)

    def close(self):
        if not self.__closed:
            self.__closed = True
            _close(self.__clients)
            self.__clients = []
            self.__handoff, self.__guard = None, None


def _accept(raw, *, snapshot, expected_read_role_arn, expected_read_role_id, condition_client,
            session_factory=boto3.Session, clock=lambda: datetime.now(timezone.utc)):
    clients = []
    try:
        snapshot = deepcopy(snapshot)
        if type(snapshot) is not ExecutionSnapshot or type(snapshot.execution) is not Execution or condition_client is None or \
                type(expected_read_role_id) is not str or not re.fullmatch('[A-Z0-9]{16,128}', expected_read_role_id): raise ValueError
        handoff = CombiReadHandoff._decode_from_internal_tls(raw, clock=clock)
        role = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-combi-source-read-([a-z0-9-]{1,32})', expected_read_role_arn) \
            if type(expected_read_role_arn) is str else None
        if not role or handoff.read_role_arn != expected_read_role_arn or not handoff.scope.matches_snapshot(snapshot): raise ValueError
        history = DynamoCombiHistoryConditions(handoff.scope.history, auth_table='foodtech-auth-sessions-'+role[2],
            cooking_table='foodtech-cooking-jobs-'+role[2], account_id=role[1], client=condition_client,
            clock=lambda: clock().timestamp())
        if not history.verify(snapshot.execution): raise ValueError
        config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True, proxies={},
            account_id_endpoint_mode='disabled', retries={'mode': 'standard', 'total_max_attempts': 2})
        session = session_factory(region_name=REGION, **handoff._sdk_credentials())
        credentials = session.get_credentials()
        if credentials is None or credentials.method != 'explicit': raise ValueError
        sts = session.client('sts', region_name=REGION, config=config, endpoint_url=f'https://sts.{REGION}.amazonaws.com')
        clients.append(sts)
        if sts.meta.endpoint_url != f'https://sts.{REGION}.amazonaws.com': raise ValueError
        identity = sts.get_caller_identity()
        session_name = handoff.assumed_role_arn.rsplit('/', 1)[1]
        if identity.get('Account') != role[1] or identity.get('Arn') != handoff.assumed_role_arn or \
                identity.get('UserId') != expected_read_role_id+':'+session_name or not history.verify(snapshot.execution): raise ValueError
        handoff._validate()
        client = session.client('dynamodb', region_name=REGION, config=config, endpoint_url=f'https://dynamodb.{REGION}.amazonaws.com')
        clients.append(client)
        if client.meta.endpoint_url != f'https://dynamodb.{REGION}.amazonaws.com': raise ValueError
        guard = DynamoCombiSourceGuard(handoff.scope, cooking_table='foodtech-cooking-jobs-'+role[2],
            client=client, history_conditions=history, clock=clock)
        return ReceivedCombiReads(handoff, snapshot.execution, guard, clients)
    except (ValueError, TypeError, AttributeError, ArithmeticError, BotoCoreError, ClientError):
        _close(clients)
        raise CombiHandoffDenied('combi read handoff unavailable') from None


def require_combi_transport(request):
    """Call before registry reads as well as before credential frame parsing."""
    expected = getattr(request.app.state,TLS_PIN_APP_STATE_KEY,None)
    state = request.scope.get('state',{})
    actual = state.get(TLS_PEER_STATE_KEY) if type(state) is dict else None
    if request.scope.get('scheme') != 'https' or getattr(request.app.state, 'mtls_listener', False) is not True or \
            getattr(request.app.state, 'combi_broker_listener', False) is not True or \
            type(expected) is not str or not re.fullmatch('[a-f0-9]{64}',expected) or type(actual) is not str or not re.fullmatch('[a-f0-9]{64}',actual) or \
            not hmac.compare_digest(actual,expected):
        raise HTTPException(401, 'internal mTLS authentication required', headers={'Cache-Control': 'no-store'})


async def receive_internal_combi_handoff(request, *, snapshot, expected_read_role_arn, expected_read_role_id,
        condition_client, session_factory=boto3.Session, clock=lambda: datetime.now(timezone.utc)):
    """Authenticate the dedicated listener before parsing any credential bytes."""
    require_combi_transport(request)
    length = request.headers.get('Content-Length')
    if request.headers.get('Content-Type') != CONTENT_TYPE or request.headers.get('Content-Encoding', 'identity') != 'identity' or \
            (length is not None and (not 1 <= len(length) <= 5 or not length.isascii() or not length.isdigit() or not 1 <= int(length) <= MAX_BYTES)):
        raise HTTPException(400, 'COMBI_READ_HANDOFF_UNAVAILABLE', headers={'Cache-Control': 'no-store'})
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw)+len(chunk) > MAX_BYTES: raise CombiHandoffDenied
            raw.extend(chunk)
        if length is not None and len(raw) != int(length): raise CombiHandoffDenied
        pending = asyncio.create_task(asyncio.to_thread(_accept, bytes(raw), snapshot=snapshot,
            expected_read_role_arn=expected_read_role_arn, expected_read_role_id=expected_read_role_id,
            condition_client=condition_client, session_factory=session_factory, clock=clock))
        try: return await asyncio.shield(pending)
        except BaseException:
            def discard(task):
                if not task.cancelled():
                    try: task.result().close()
                    except Exception: pass
            pending.add_done_callback(discard)
            raise
    except (CombiHandoffDenied, ClientDisconnect):
        raise HTTPException(400, 'COMBI_READ_HANDOFF_UNAVAILABLE', headers={'Cache-Control': 'no-store'}) from None
    finally:
        raw[:] = b'\x00'*len(raw)
