"""Immutable menu-specific human reviews under owned COMBI_MEAL partitions.

Review approval is not recipe adoption, a safety assertion or an equipment write.
The batch and history revisions remain unchanged. Existing meal deletion removes
these records with the rest of that owned partition.
"""
import copy
import re
from uuid import UUID

from .cooking_store import Conflict, DynamoCookingStore, MemoryCookingStore, _api_numbers, _is_transaction_contention
from .recipe_review_store import _integer, _same, validate_review


def _scope(pk, menu):
    match = re.fullmatch(r'SCOPE#USER#([A-Za-z0-9_-]{1,128})#COMBI_MEAL#([a-f0-9-]{36})', pk) if type(pk) is str else None
    if not match or str(UUID(match[2])) != match[2] or menu not in ('RICE', 'MACKEREL'):
        raise ValueError('invalid combi review scope')
    return match[1], match[2]


def _keys(menu):
    if menu not in ('RICE', 'MACKEREL'):
        raise ValueError('invalid combi review menu')
    return f'RECIPE_REVIEW#{menu}#CURRENT', f'RECIPE_REVIEW#{menu}#VERSION#'


def validate_combi_review(value):
    if type(value) is not dict or value.get('schema_version') != 'combi-recipe-review-v1' or \
            not {'meal_id', 'menu'} <= set(value):
        raise ValueError('invalid combi review')
    base = {k: v for k, v in value.items() if k not in {'meal_id', 'menu'}}
    checked = validate_review({**base, 'schema_version': 'recipe-review-v1'})
    if value['menu'] not in ('RICE', 'MACKEREL') or checked['proposal']['menu'] != value['menu'] or \
            type(value['meal_id']) is not str or str(UUID(value['meal_id'])) != value['meal_id']:
        raise ValueError('invalid combi review binding')
    return {**checked, 'schema_version': 'combi-recipe-review-v1', 'meal_id': value['meal_id'], 'menu': value['menu']}


def _record(row, pk, menu, version):
    if type(row) is not dict or row.get('pk') != pk or row.get('sk') != _keys(menu)[1] + f'{version:019d}':
        raise ValueError('invalid stored combi review')
    value = validate_combi_review({k: v for k, v in row.items() if k not in {'pk', 'sk'}})
    if value['version'] != version or value['meal_id'] != _scope(pk, menu)[1] or value['menu'] != menu:
        raise ValueError('stored combi review scope mismatch')
    return copy.deepcopy(value)


class CombiReviewStore:
    def __init__(self, store):
        if not isinstance(store, (MemoryCookingStore, DynamoCookingStore)):
            raise ValueError('explicit cooking storage dependency required')
        self.store = store

    def _get(self, pk, sk):
        if isinstance(self.store, MemoryCookingStore):
            with self.store.lock:
                return copy.deepcopy(self.store.items.get((pk, sk)))
        return _api_numbers(self.store.table.get_item(Key={'pk': pk, 'sk': sk}, ConsistentRead=True).get('Item'))

    def current(self, pk, menu):
        _scope(pk, menu)
        current, prefix = _keys(menu)
        row = self._get(pk, current)
        if row is None:
            return {'version': 0, 'review': None}
        if type(row) is not dict or set(row) != {'pk', 'sk', 'version'} or row['pk'] != pk or row['sk'] != current:
            raise ValueError('invalid combi review pointer')
        version = _integer(row['version'], minimum=1)
        return {'version': version, 'review': _record(self._get(pk, prefix + f'{version:019d}'), pk, menu, version)}

    def history(self, pk, menu, limit=20, cursor=None):
        _scope(pk, menu)
        _, prefix = _keys(menu)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('invalid combi review page')
        if cursor is not None:
            _integer(cursor, minimum=1)
        if isinstance(self.store, MemoryCookingStore):
            with self.store.lock:
                rows = [copy.deepcopy(v) for (p, sk), v in sorted(self.store.items.items())
                        if p == pk and sk.startswith(prefix) and (cursor is None or sk > prefix + f'{cursor:019d}')]
            page, more = rows[:limit], len(rows) > limit
        else:
            from boto3.dynamodb.conditions import Key
            args = {'KeyConditionExpression': Key('pk').eq(pk) & Key('sk').begins_with(prefix),
                    'ConsistentRead': True, 'Limit': limit, 'ScanIndexForward': True}
            if cursor is not None:
                args['ExclusiveStartKey'] = {'pk': pk, 'sk': prefix + f'{cursor:019d}'}
            response = self.store.table.query(**args)
            page, more = _api_numbers(response.get('Items', [])), bool(response.get('LastEvaluatedKey'))
        values, previous = [], cursor or 0
        for row in page:
            version = _integer(row.get('version'), minimum=1)
            if version <= previous:
                raise ValueError('combi review page did not advance')
            values.append(_record(row, pk, menu, version))
            previous = version
        if more and not values:
            raise ValueError('combi review page did not advance')
        return {'items': values, 'next_cursor': previous if more else None}

    def save(self, pk, menu, expected_meal_version, expected_review_version, record, expected_epoch):
        subject, meal_id = _scope(pk, menu)
        _integer(expected_meal_version, minimum=1)
        if expected_meal_version > 10**18:
            raise ValueError('invalid meal version')
        _integer(expected_review_version)
        _integer(expected_epoch)
        record = validate_combi_review(record)
        if (record['meal_id'], record['menu'], record['job_version'], record['version']) != \
                (meal_id, menu, expected_meal_version, expected_review_version + 1):
            raise ValueError('invalid combi review binding')
        current, prefix = _keys(menu)
        sk = prefix + f"{record['version']:019d}"
        job_field = 'rice_cooking_job_id' if menu == 'RICE' else 'mackerel_cooking_job_id'
        if isinstance(self.store, MemoryCookingStore):
            with self.store.lock:
                meta = self.store.items.get((pk, 'META'))
                if type(meta) is not dict or type(meta.get('version')) is not int or \
                        (meta.get('meal_batch_id'), meta['version'], meta.get(job_field), meta.get('provenance')) != \
                        (meal_id, expected_meal_version, record['job_id'], 'SYNTHETIC'):
                    raise Conflict
                existing = self.store.items.get((pk, sk))
                if existing is not None:
                    value = _record(existing, pk, menu, record['version'])
                    if _same(value, record):
                        return value
                    raise Conflict
                if self.current(pk, menu)['version'] != expected_review_version:
                    raise Conflict
                self.store.items[pk, sk] = copy.deepcopy({'pk': pk, 'sk': sk, **record})
                self.store.items[pk, current] = {'pk': pk, 'sk': current, 'version': record['version']}
                return copy.deepcopy(record)
        if not self.store.auth_table_name or self.store.auth_table_name == self.store.table.name or \
                not self.store.require_principal:
            raise ValueError('shared active principal storage required')
        encode = self.store._item
        auth = self.store._owner_guard(subject, expected_epoch)
        auth['ConditionCheck']['ReturnValuesOnConditionCheckFailure'] = 'NONE'
        pointer_values = {':next': record['version']}
        if expected_review_version:
            pointer_values[':expected'] = expected_review_version
        try:
            self.store.client.transact_write_items(TransactItems=[auth,
                {'ConditionCheck': {'TableName': self.store.table.name, 'Key': encode({'pk': pk, 'sk': 'META'}),
                    'ConditionExpression': '#v = :expected AND meal_batch_id = :meal AND #job = :job AND provenance = :source',
                    'ExpressionAttributeNames': {'#v': 'version', '#job': job_field},
                    'ExpressionAttributeValues': encode({':expected': expected_meal_version, ':meal': meal_id,
                        ':job': record['job_id'], ':source': 'SYNTHETIC'}), 'ReturnValuesOnConditionCheckFailure': 'NONE'}},
                {'Update': {'TableName': self.store.table.name, 'Key': encode({'pk': pk, 'sk': current}),
                    'UpdateExpression': 'SET #v = :next',
                    'ConditionExpression': '#v = :expected' if expected_review_version else 'attribute_not_exists(pk)',
                    'ExpressionAttributeNames': {'#v': 'version'}, 'ExpressionAttributeValues': encode(pointer_values),
                    'ReturnValuesOnConditionCheckFailure': 'NONE'}},
                {'Put': {'TableName': self.store.table.name, 'Item': encode({'pk': pk, 'sk': sk, **record}),
                    'ConditionExpression': 'attribute_not_exists(pk)', 'ReturnValuesOnConditionCheckFailure': 'NONE'}}])
        except self.store.client.exceptions.TransactionCanceledException as error:
            if not _is_transaction_contention(error):
                raise
            reasons = error.response.get('CancellationReasons') or []
            if len(reasons) != 4 or any(r.get('Code') not in {None, 'None'} for r in reasons[:2]):
                raise Conflict from None
            existing = self._get(pk, sk)
            if existing is not None:
                saved = _record(existing, pk, menu, record['version'])
                if _same(saved, record):
                    return saved
            raise Conflict from None
        return copy.deepcopy(record)
