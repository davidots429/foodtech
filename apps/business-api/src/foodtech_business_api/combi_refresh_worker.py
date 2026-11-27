"""Process one discovered meal marker without a customer HTTP session.

Discovery, execution role and scheduling are separate deployment responsibilities.
"""

import asyncio
import json
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


def _authorized_epoch(principal: dict | None, *, require_principal: bool = False) -> int:
    if not isinstance(principal, dict) or principal.get('suspended', False) is not False or \
            'deletion_lock_request' in principal:
        raise RefreshNotAuthorized('account cannot be processed')
    if require_principal and 'epoch' not in principal:
        raise RefreshNotAuthorized('account epoch is required')
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
    epoch = _authorized_epoch(response.get('Item'),
                              require_principal=getattr(store, 'require_principal', False) is True)
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
    counts = {'processed': 0, 'partial': 0, 'failed': 0}
    for marker in page.get('Items', []):
        try:
            result = process_pending_marker(store, auth_table, marker)
            if result.get('status') not in {'NO_PENDING', 'REFRESHED', 'PARTIAL'}:
                raise ValueError('invalid refresh result')
            counts['processed'] += 1
            if result['status'] == 'PARTIAL':
                counts['partial'] += 1
        except TimeoutError:
            raise
        except Exception:
            # The marker remains pending; never log customer keys or exception values.
            counts['failed'] += 1
    return {**counts, 'next_cursor': page.get('LastEvaluatedKey')}


def sweep_pending(store, auth_table, *, page_size: int = 25, max_pages_per_shard: int = 10000) -> dict:
    """Visit every shard and page once; a future sweep retries retained markers."""
    import json

    if type(max_pages_per_shard) is not int or not 1 <= max_pages_per_shard <= 10000:
        raise ValueError('invalid sweep page limit')
    totals = {'processed': 0, 'partial': 0, 'failed': 0, 'pages': 0}
    for shard in range(16):
        cursor, seen = None, set()
        for _ in range(max_pages_per_shard):
            page = process_pending_page(store, auth_table, shard, cursor, limit=page_size)
            totals['processed'] += page['processed']
            totals['partial'] += page['partial']
            totals['failed'] += page['failed']
            totals['pages'] += 1
            cursor = page['next_cursor']
            if cursor is None:
                break
            token = json.dumps(cursor, sort_keys=True, separators=(',', ':'))
            if token in seen:
                raise RuntimeError('pending index pagination did not advance')
            seen.add(token)
        else:
            raise RuntimeError('pending index sweep page limit exceeded')
    return totals


def parse_refresh_targets(raw: str) -> tuple[dict, ...]:
    """Validate the entire bounded manual selection before any SDK discovery."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate target member')
            result[key] = value
        return result

    if type(raw) is not str or not 1 <= len(raw.encode('utf-8')) <= 8192:
        raise ValueError('invalid meal refresh targets')
    values = json.loads(raw, object_pairs_hook=unique)
    if type(values) is not list or not 1 <= len(values) <= 16:
        raise ValueError('invalid meal refresh targets')
    targets, seen = [], set()
    for value in values:
        if type(value) is not dict or set(value) != {'subject', 'meal_id'} or \
                type(value['subject']) is not str or type(value['meal_id']) is not str:
            raise ValueError('invalid meal refresh target')
        marker = {'pk': f"SCOPE#USER#{value['subject']}#COMBI_MEAL#{value['meal_id']}",
                  'sk': 'MEAL_AGGREGATE#DIRTY'}
        _scope(marker)
        if marker['pk'] in seen:
            raise ValueError('duplicate meal refresh target')
        seen.add(marker['pk'])
        targets.append(marker)
    return tuple(targets)


def refresh_targets(store, auth_table, targets: tuple[dict, ...]) -> dict:
    """Discover exact selected meals; never fall back to the global sweep.

    This is a trusted worker selection, not IAM isolation or index-completeness
    proof. Dependent aggregate groups are rebuilt by the existing domain flow,
    including their other meal references. Each target uses shard AND partition key, then the usual
    current account/marker/epoch checks. An index miss with a pending base row
    fails for later reconciliation instead of reporting a successful empty run.
    """
    from boto3.dynamodb.conditions import Key
    from .cooking_store import meal_aggregate_shard

    # Revalidate callers as well as environment input before the first query.
    if type(targets) is not tuple or not 1 <= len(targets) <= 16:
        raise ValueError('invalid meal refresh targets')
    seen = set()
    for target in targets:
        _scope(target)
        if set(target) != {'pk', 'sk'} or target['pk'] in seen:
            raise ValueError('invalid meal refresh targets')
        seen.add(target['pk'])
    totals = {'processed': 0, 'partial': 0, 'failed': 0, 'pages': 0}
    for target in targets:
        try:
            pk = target['pk']
            page = store.table.query(IndexName='MealAggregatePending',
                KeyConditionExpression=Key('aggregate_refresh_shard').eq(meal_aggregate_shard(pk)) & Key('pk').eq(pk),
                Limit=2)
            totals['pages'] += 1
            rows = page.get('Items', [])
            if type(rows) is not list or len(rows) > 1 or page.get('LastEvaluatedKey') is not None or \
                    any(not isinstance(row, dict) or row.get('pk') != pk or row.get('sk') != target['sk'] or
                        row.get('aggregate_refresh_shard') != meal_aggregate_shard(pk) for row in rows):
                raise ValueError('unexpected target index response')
            current = store.read_meal_aggregate_dirty(pk)
            if current is not None and (not rows or not isinstance(current, dict) or
                    current.get('pk', pk) != pk or current.get('sk') != target['sk'] or
                    current.get('aggregate_refresh_shard') != meal_aggregate_shard(pk)):
                raise ValueError('pending target index unavailable')
            result = process_pending_marker(store, auth_table, target)
            if result.get('status') not in {'NO_PENDING', 'REFRESHED', 'PARTIAL'}:
                raise ValueError('invalid refresh result')
            totals['processed'] += 1
            totals['partial'] += result['status'] == 'PARTIAL'
        except TimeoutError:
            raise
        except Exception:
            totals['failed'] += 1
    return totals


def main() -> int:
    """The executable requires the dedicated finite ECS runtime contract."""
    from .meal_refresh_runtime import main as runtime_main
    return runtime_main()


if __name__ == '__main__':
    raise SystemExit(main())
