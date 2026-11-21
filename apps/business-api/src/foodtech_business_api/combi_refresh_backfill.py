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
