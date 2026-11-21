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


def process_pending_page(store, auth_table, shard: int, cursor: dict | None = None, *, limit: int = 25) -> dict:
    """Process a bounded index page; the scheduler must visit all shards repeatedly.

    GSI reads are eventually consistent: cursors are per sweep, not durable
    completion checkpoints. Failed records remain in the base table for a later
    sweep. A stale index hit is harmless because the worker rereads the marker.
    """
    from boto3.dynamodb.conditions import Key

    if type(shard) is not int or not 0 <= shard < 16 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid pending page bounds')
    args = {'IndexName': 'MealAggregatePending',
            'KeyConditionExpression': Key('aggregate_refresh_shard').eq(f'PENDING#{shard:02x}'),
            'Limit': limit}
    if cursor is not None:
        if not isinstance(cursor, dict):
            raise ValueError('invalid pending page cursor')
        args['ExclusiveStartKey'] = cursor
    page = store.table.query(**args)
    counts = {'processed': 0, 'failed': 0}
    for marker in page.get('Items', []):
        try:
            process_pending_marker(store, auth_table, marker)
            counts['processed'] += 1
        except Exception:
            # The marker remains pending; never log customer keys or exception values.
            counts['failed'] += 1
    return {**counts, 'next_cursor': page.get('LastEvaluatedKey')}
