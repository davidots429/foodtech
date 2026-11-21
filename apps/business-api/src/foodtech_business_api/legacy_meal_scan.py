"""Restricted legacy meal scan; neither a snapshot nor deletion-completion proof.

Raw checkpoints can contain other customers' keys. Only an authenticated,
separately authorized maintenance worker with protected checkpoints may use it.
"""
from dataclasses import dataclass, field

from boto3.dynamodb.conditions import Attr

from .cooking_store import _meal_listing, _meal_page_args


@dataclass(frozen=True)
class LegacyMealPage:
    references: tuple[str, ...] = field(repr=False)
    checkpoint: dict | None = field(repr=False)
    scanned_count: int


def _checkpoint(value):
    if value is not None and (not isinstance(value, dict) or set(value) != {'pk', 'sk'} or
            any(not isinstance(part, str) or not part or len(part) > 2048 for part in value.values())):
        raise ValueError('invalid legacy scan checkpoint')


def scan_legacy_meals(table, *, subject: str, limit: int, checkpoint: dict | None = None) -> LegacyMealPage:
    _meal_page_args(subject, limit, None)
    _checkpoint(checkpoint)
    prefix = f'SCOPE#USER#{subject}#COMBI_MEAL#'
    args = {'FilterExpression': Attr('pk').begins_with(prefix) & Attr('sk').eq('META'),
            'ProjectionExpression': 'pk, sk, meal_batch_id', 'ConsistentRead': True, 'Limit': limit}
    if checkpoint is not None:
        args['ExclusiveStartKey'] = checkpoint
    page = table.scan(**args)
    items, count = page.get('Items'), page.get('ScannedCount')
    if not isinstance(items, list) or type(count) is not int or not 0 <= count <= limit:
        raise ValueError('invalid legacy scan result')
    references = []
    for item in items:
        try:
            meal_id = item['meal_batch_id']
            _meal_listing(prefix + meal_id, [{'sk': 'META', 'meal_batch_id': meal_id}])
            if item['pk'] != prefix + meal_id or item['sk'] != 'META':
                raise ValueError
            references.append(meal_id)
        except (KeyError, TypeError, ValueError):
            raise ValueError('invalid legacy meal') from None
    next_key = page.get('LastEvaluatedKey')
    _checkpoint(next_key)
    return LegacyMealPage(tuple(references), next_key, count)


def reconcile_legacy_meal_page(table, store, *, subject: str, limit: int,
                               checkpoint: dict | None = None) -> LegacyMealPage:
    """Return progress only after the whole page is reconciled; retry failures."""
    page = scan_legacy_meals(table, subject=subject, limit=limit, checkpoint=checkpoint)
    for meal_id in page.references:
        store.ensure_meal_listed(subject, meal_id)
    return page
