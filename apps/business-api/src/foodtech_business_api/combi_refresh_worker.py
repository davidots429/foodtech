"""Process one discovered meal marker without a customer HTTP session.

Discovery, execution role and scheduling are separate deployment responsibilities.
"""

import asyncio
import re
from decimal import Decimal
from uuid import UUID

from .combi_quality_aggregate import refresh_pending_for_subject


class RefreshNotAuthorized(Exception):
    """Account is missing, suspended, locked, or has invalid authorization state."""


def _scope(marker: dict) -> tuple[str, UUID]:
    if not isinstance(marker, dict) or marker.get('sk') != 'MEAL_AGGREGATE#DIRTY':
        raise ValueError('invalid pending marker')
    pk = marker.get('pk')
    match = re.fullmatch(r'SCOPE#USER#([A-Za-z0-9_-]{1,128})#COMBI_MEAL#([0-9a-f-]{36})', pk) if isinstance(pk, str) else None
    if not match:
        raise ValueError('invalid pending marker scope')
    try:
        meal_id = UUID(match.group(2))
        if str(meal_id) != match.group(2):
            raise ValueError
    except ValueError:
        raise ValueError('invalid pending marker scope') from None
    return match.group(1), meal_id


def _authorized_epoch(principal: dict | None) -> int:
    if not isinstance(principal, dict) or principal.get('suspended', False) is not False or \
            'deletion_lock_request' in principal:
        raise RefreshNotAuthorized('account cannot be processed')
    epoch = principal.get('epoch', 0)
    if isinstance(epoch, bool) or not isinstance(epoch, (int, Decimal)) or \
            (isinstance(epoch, Decimal) and not epoch.is_finite()) or \
            int(epoch) != epoch or not 0 <= epoch < 2**63:
        raise RefreshNotAuthorized('invalid account epoch')
    return int(epoch)


def process_pending_marker(store, auth_table, marker: dict) -> dict:
    """Process one scoped marker; errors leave it available for retry.

    The caller must authenticate the worker and control discovery. A strongly
    consistent account read supplies the epoch; each write checks it again in
    the same transaction as the aggregate or marker change.
    """
    subject, meal_id = _scope(marker)
    response = auth_table.get_item(Key={'pk': f'USER#{subject}'}, ConsistentRead=True)
    epoch = _authorized_epoch(response.get('Item'))
    return asyncio.run(refresh_pending_for_subject(store, subject, epoch, meal_id))
