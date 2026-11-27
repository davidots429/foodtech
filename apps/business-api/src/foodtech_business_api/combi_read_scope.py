"""Authenticate a stored Combi execution's four candidate source partitions.

Internal composition only. Readers are trusted strongly consistent readers;
there is no public payload decoder, default AWS identity or credential issuer.
Observations cannot fence later writes; execution/settlement still need the
protected history conditions and separately enforced AWS read lease.
"""
import asyncio
from copy import deepcopy
import math
import time

from botocore.exceptions import BotoCoreError, ClientError

from foodtech_contracts.combi_execution_binding import CombiExecutionBinding, validate_combi_execution
from foodtech_contracts.combi_history_witness import canonical_uuid, history_integer
from foodtech_contracts.combi_read_scope import CombiReadScope
from .auth import _csrf, _session
from .auth_store import Session
from .combi_prior_history import DynamoCombiPriorHistoryAuthority
from .prior_history_authority import PriorHistoryUnavailable
from .recipe_scope import RecipeScopeDenied, _require_principal_record
from .subject_identity import valid_subject


def resolve_combi_read_scope(session, execution_id, *, execution_reader, history_authority, clock=time.time):
    """Bind a stable execution snapshot to independently observed whole history."""
    try:
        if type(session) is not Session or session.surface != 'customer' or not valid_subject(session.subject) or \
                not canonical_uuid(execution_id) or not callable(execution_reader) or \
                type(history_authority) is not DynamoCombiPriorHistoryAuthority:
            raise ValueError
        owner, epoch, expiry = session.subject, history_integer(session.epoch, 0, 2**63-1), history_integer(session.expires, 1, 2**63-1)

        def account_time():
            now = clock()
            if type(now) not in (int, float) or not 0 < now < 2**63 or not math.isfinite(now) or now >= expiry or \
                    (session.surface, session.subject, session.epoch, session.expires) != ('customer', owner, epoch, expiry) or \
                    type(session.epoch) is bool or type(session.expires) is bool:
                raise ValueError
            return now

        started = account_time()
        first = deepcopy(execution_reader(execution_id))
        execution = getattr(first, 'execution', None)
        binding = getattr(execution, 'combi_binding', None)
        if type(binding) is not CombiExecutionBinding or binding.auth_epoch != epoch or \
                getattr(execution, 'tenant_id', None) != owner or getattr(execution, 'execution_id', None) != execution_id:
            raise ValueError
        validate_combi_execution(execution)
        if type(getattr(first, 'state', None)) is not str or first.state not in ('PENDING', 'RESERVED') or \
                type(getattr(first, 'version', None)) is not int or not 1 <= first.version < 2**63:
            raise ValueError
        history = history_authority.observe(owner=owner, binding=binding)
        scope = CombiReadScope(owner, execution_id, binding, history, getattr(first, 'state', None), getattr(first, 'version', None))
        if not scope.matches_snapshot(first) or not scope.matches_snapshot(deepcopy(execution_reader(execution_id))):
            raise ValueError
        history_authority.revalidate(history)
        if not scope.matches_snapshot(deepcopy(execution_reader(execution_id))) or account_time() < started:
            raise ValueError
        return scope
    except (ValueError, TypeError, AttributeError, KeyError, ArithmeticError, PriorHistoryUnavailable, BotoCoreError, ClientError):
        raise RecipeScopeDenied('verified combi read scope unavailable') from None


async def authenticated_combi_read_scope(request, execution_id, *, execution_reader, history_authority):
    before = await _session(request, 'customer')
    _csrf(request, before)
    await _require_principal_record(before)
    # Do not let a shared mutable Session change the comparison baseline. Do
    # not copy access/refresh tokens into an observation or credential boundary.
    expected = Session(before.surface, before.subject, None, before.csrf, before.expires, before.epoch)
    scope = await asyncio.to_thread(resolve_combi_read_scope, expected, execution_id,
        execution_reader=execution_reader, history_authority=history_authority)
    after = await _session(request, 'customer')
    _csrf(request, after)
    await _require_principal_record(after)
    try:
        same = (after.surface, after.subject, after.epoch, after.csrf) == \
            (expected.surface, expected.subject, expected.epoch, expected.csrf) and \
            history_integer(after.expires, 1, 2**63-1) > time.time()
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        same = False
    if not same:
        raise RecipeScopeDenied('customer session changed')
    return scope
