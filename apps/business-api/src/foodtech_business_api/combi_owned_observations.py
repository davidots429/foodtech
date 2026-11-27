"""Read recorded combi facts through a trusted, customer-owned store boundary.

Internal only: the session and readers must come from the authenticated BFF,
and expected version/hash from a trusted execution snapshot. This helper does
not authenticate a request, issue AWS credentials, attest complete history or
select the immediately prior meal. Recheck these observations and authorization
at execution/settlement; double reads cannot fence a later concurrent write.
The registered combi Gateway execution remains disabled.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import re
from uuid import UUID

from foodtech_contracts.combi_observed_inputs import observe_combi_batch
from .subject_identity import valid_subject


class CombiObservationUnavailable(Exception):
    pass


def _integer(value, minimum=0):
    if type(value) not in (int, Decimal) or (type(value) is Decimal and not value.is_finite()) or \
            not minimum <= value < 2**63 or value != int(value):
        raise CombiObservationUnavailable('recorded combi scope unavailable')
    return int(value)


def _identical(left, right):
    # In particular True and 1 must not conceal an intervening record change.
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_identical(v, right[k]) for k, v in left.items())
    if type(left) is list:
        return len(left) == len(right) and all(_identical(a, b) for a, b in zip(left, right))
    return left == right


def read_owned_combi_observation(session, *, meal_id, expected_version, source_digest, menu,
                                meal_reader, source_reader, principal_reader,
                                clock=lambda: datetime.now(timezone.utc)):
    """Check owner/account before and after reading the same persisted material.

    Readers must provide fresh strongly consistent, unprojected records. No
    browser-provided record map or grant/history Boolean belongs at this boundary.
    The existing store owns pagination and request/resource limits.
    """
    try:
        owner = session.subject
        epoch = _integer(session.epoch)
        expiry = _integer(session.expires, 1)
        if session.surface != 'customer' or not valid_subject(owner) or \
                type(meal_id) is not str or str(UUID(meal_id)) != meal_id or \
                type(expected_version) is not int or not 1 <= expected_version < 2**63 or \
                type(source_digest) is not str or not re.fullmatch('[a-f0-9]{64}', source_digest) or \
                menu not in ('RICE', 'MACKEREL'):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise CombiObservationUnavailable('recorded combi scope unavailable') from None
    pk = f'SCOPE#USER#{owner}#COMBI_MEAL#{meal_id}'

    def account():
        now = clock()
        if type(now) is not datetime or now.utcoffset() is None or now.timestamp() >= expiry:
            raise CombiObservationUnavailable('customer session expired')
        if (session.subject, _integer(session.epoch), _integer(session.expires, 1), session.surface) != \
                (owner, epoch, expiry, 'customer'):
            raise CombiObservationUnavailable('customer session changed')
        row = deepcopy(principal_reader(owner))
        if type(row) is not dict or row.get('pk') != 'USER#' + owner or \
                _integer(row.get('epoch')) != epoch or row.get('suspended', False) is not False or \
                'deletion_lock_request' in row:
            raise CombiObservationUnavailable('customer account unavailable')
        return now

    def snapshot():
        stored = deepcopy(meal_reader(pk))
        if type(stored) is not list or not stored:
            raise CombiObservationUnavailable('recorded combi meal unavailable')
        rows = {}
        for row in stored:
            if type(row) is not dict or row.get('pk') != pk or type(row.get('sk')) is not str or row['sk'] in rows:
                raise CombiObservationUnavailable('recorded combi row scope unavailable')
            rows[row['sk']] = row
        meta = rows.get('META')
        if type(meta) is not dict or meta.get('meal_batch_id') != meal_id or \
                _integer(meta.get('version'), 1) != expected_version or meta.get('provenance') != 'SYNTHETIC':
            raise CombiObservationUnavailable('recorded combi version unavailable')
        source = meta.get('source')
        if type(source) is not dict or source.get('raw_sha256') != source_digest:
            raise CombiObservationUnavailable('recorded combi source unavailable')
        total = _integer(source.get('record_count'), 3)
        if total > 5000:
            raise CombiObservationUnavailable('recorded combi source exceeds import bound')
        count = (total + 99) // 100
        raw, chunks = [], []
        for i in range(count):
            chunk = deepcopy(source_reader(pk, i))
            if type(chunk) is not dict or chunk.get('pk') != pk + '#SOURCE' or chunk.get('sk') != f'RAW#{i:04d}':
                raise CombiObservationUnavailable('recorded combi chunk scope unavailable')
            part = chunk.get('records')
            if _integer(chunk.get('first_sequence'), 1) != i * 100 + 1 or type(part) is not list or \
                    len(part) != min(100, total - i * 100) or any(type(line) is not str for line in part):
                raise CombiObservationUnavailable('recorded combi chunk incomplete')
            raw.extend(part)
            chunks.append(chunk)
        return rows, raw, chunks

    started = account()
    first, raw, chunks = snapshot()
    second, repeated_raw, repeated_chunks = snapshot()
    now = account()
    if now < started or not _identical(first, second) or not _identical(chunks, repeated_chunks) or raw != repeated_raw:
        raise CombiObservationUnavailable('recorded combi meal changed')
    try:
        return observe_combi_batch(first, raw, menu, now=now)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        raise CombiObservationUnavailable('recorded combi observations invalid') from None
