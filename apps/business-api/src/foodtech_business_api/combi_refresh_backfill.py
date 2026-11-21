"""Explicit page-level migration for legacy pending markers.

Call only after old writers have stopped; table scan and repeated sweeps may
consume substantial read capacity. This module never starts a migration itself.
"""
from decimal import Decimal

from boto3.dynamodb.conditions import Attr

from .combi_refresh_worker import _scope
from .cooking_store import meal_aggregate_shard


def backfill_pending_page(table, *, cursor: dict | None = None, limit: int = 100) -> dict:
    if type(limit) is not int or not 1 <= limit <= 1000 or (cursor is not None and not isinstance(cursor, dict)):
        raise ValueError('invalid backfill page bounds')
    args = {'Limit': limit, 'ConsistentRead': True,
            'FilterExpression': Attr('sk').eq('MEAL_AGGREGATE#DIRTY') & Attr('state').eq('PENDING') &
                                Attr('aggregate_refresh_shard').not_exists()}
    if cursor is not None:
        args['ExclusiveStartKey'] = cursor
    page = table.scan(**args)
    updated, changed = 0, 0
    for row in page.get('Items', []):
        _scope(row)
        version = row.get('version')
        if isinstance(version, bool) or not isinstance(version, (int, Decimal)) or \
                (isinstance(version, Decimal) and not version.is_finite()) or int(version) != version or version < 1:
            raise ValueError('invalid pending marker version')
        try:
            table.update_item(
                Key={'pk': row['pk'], 'sk': row['sk']},
                UpdateExpression='SET aggregate_refresh_shard = :shard',
                ConditionExpression=Attr('version').eq(version) & Attr('state').eq('PENDING') &
                                    Attr('aggregate_refresh_shard').not_exists(),
                ExpressionAttributeValues={':shard': meal_aggregate_shard(row['pk'])},
            )
            updated += 1
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            changed += 1
    return {'updated': updated, 'changed': changed, 'next_cursor': page.get('LastEvaluatedKey')}


def backfill_pass(table, *, page_size: int = 100, max_pages: int = 10000) -> dict:
    """Run one bounded scan pass; a clean observation is not writer quiescence proof.

    Concurrent updates may move behind a scan cursor even with consistent
    page reads. The caller must independently prove old writers stopped, then
    run another full pass to establish that no legacy markers were observed.
    """
    import json

    if type(max_pages) is not int or not 1 <= max_pages <= 10000:
        raise ValueError('invalid backfill page limit')
    cursor, seen = None, set()
    total = {'updated': 0, 'changed': 0, 'pages': 0}
    for _ in range(max_pages):
        page = backfill_pending_page(table, cursor=cursor, limit=page_size)
        total['updated'] += page['updated']
        total['changed'] += page['changed']
        total['pages'] += 1
        cursor = page['next_cursor']
        if cursor is None:
            return {**total, 'status': 'OBSERVED_CLEAN' if not (total['updated'] or total['changed']) else 'RESCAN'}
        token = json.dumps(cursor, sort_keys=True, separators=(',', ':'))
        if token in seen:
            raise RuntimeError('backfill pagination did not advance')
        seen.add(token)
    raise RuntimeError('backfill page limit exceeded')
