"""Immutable operator reviews; no recipe adoption, safety approval or equipment I/O.

Reviews live under the existing subject-owned job partition. Their independent
pointer never overwrites cooking META or changes the cooking version. Existing
partition deletion/residual checks include all of these rows.
"""
import copy
from datetime import datetime, timezone
import re
from uuid import UUID

from foodtech_contracts.recipe_result_view import recipe_proposal_digest, validated_recipe_proposal
from foodtech_contracts.tonkatsu_transaction_conditions import TonkatsuTransactionConditions
from .cooking_store import Conflict, DynamoCookingStore, MemoryCookingStore, _api_numbers, _is_transaction_contention

PREFIX = 'RECIPE_REVIEW#VERSION#'
CURRENT = 'RECIPE_REVIEW#CURRENT'


def _integer(value, *, minimum=0):
    if type(value) is not int or not minimum <= value < 2**63 - 1:
        raise ValueError('invalid recipe review version')
    return value


def _scope(pk):
    match = re.fullmatch(r'SCOPE#USER#([A-Za-z0-9_-]{1,128})#JOB#([0-9a-f-]{36})', pk) if type(pk) is str else None
    if not match or str(UUID(match[2])) != match[2]:
        raise ValueError('invalid recipe review scope')
    return match[1], match[2]


def validate_review(record):
    fields = {'schema_version', 'version', 'request_id', 'execution_id', 'job_id', 'job_version',
              'proposal_digest', 'proposal', 'decision', 'reason', 'reviewed_at'}
    if type(record) is not dict or set(record) != fields or record['schema_version'] != 'recipe-review-v1':
        raise ValueError('invalid recipe review record')
    _integer(record['version'], minimum=1)
    _integer(record['job_version'], minimum=1)
    for name in ('request_id', 'execution_id', 'job_id'):
        if type(record[name]) is not str or str(UUID(record[name])) != record[name]:
            raise ValueError('invalid recipe review identity')
    if type(record['decision']) is not str or record['decision'] not in {'APPROVE', 'REJECT'} or type(record['reason']) is not str or \
            not 1 <= len(record['reason'].strip()) <= 1000 or record['reason'] != record['reason'].strip():
        raise ValueError('invalid recipe review decision')
    if type(record['reviewed_at']) is not str:
        raise ValueError('invalid recipe review instant')
    instant = datetime.fromisoformat(record['reviewed_at'].replace('Z', '+00:00'))
    if instant.tzinfo is None or instant.utcoffset() != timezone.utc.utcoffset(instant):
        raise ValueError('invalid recipe review instant')
    proposal = validated_recipe_proposal(record['proposal'])
    if record['proposal_digest'] != recipe_proposal_digest(proposal):
        raise ValueError('recipe review proposal changed')
    return {**record, 'proposal': proposal}


def _record(row, *, pk, version):
    if type(row) is not dict or row.get('pk') != pk or row.get('sk') != PREFIX + f'{version:019d}':
        raise ValueError('invalid stored recipe review')
    value = validate_review({k: v for k, v in row.items() if k not in {'pk', 'sk'}})
    if value['version'] != version or value['job_id'] != _scope(pk)[1]:
        raise ValueError('invalid stored recipe review binding')
    return copy.deepcopy(value)


def _same(existing, requested):
    # A response retry has a fresh server timestamp, but cannot change the decision.
    return {k: v for k, v in existing.items() if k != 'reviewed_at'} == \
           {k: v for k, v in requested.items() if k != 'reviewed_at'}


class RecipeReviewStore:
    def __init__(self, store):
        if not isinstance(store, (MemoryCookingStore, DynamoCookingStore)):
            raise ValueError('explicit cooking storage dependency required')
        self.store = store

    def _get(self, pk, sk):
        if isinstance(self.store, MemoryCookingStore):
            with self.store.lock:
                return copy.deepcopy(self.store.items.get((pk, sk)))
        return _api_numbers(self.store.table.get_item(Key={'pk': pk, 'sk': sk}, ConsistentRead=True).get('Item'))

    def current(self, pk):
        _scope(pk)
        row = self._get(pk, CURRENT)
        if row is None:
            return {'version': 0, 'review': None}
        if type(row) is not dict or set(row) != {'pk', 'sk', 'version'} or row['pk'] != pk or row['sk'] != CURRENT:
            raise ValueError('invalid recipe review pointer')
        version = _integer(row['version'], minimum=1)
        return {'version': version, 'review': _record(self._get(pk, PREFIX + f'{version:019d}'), pk=pk, version=version)}

    def history(self, pk, limit=20, cursor=None):
        _scope(pk)
        if type(limit) is not int or not 1 <= limit <= 50 or \
                (cursor is not None and (type(cursor) is not int or not 1 <= cursor < 2**63 - 1)):
            raise ValueError('invalid recipe review page')
        if isinstance(self.store, MemoryCookingStore):
            with self.store.lock:
                rows = [copy.deepcopy(row) for (item_pk, sk), row in sorted(self.store.items.items())
                        if item_pk == pk and sk.startswith(PREFIX) and (cursor is None or sk > PREFIX + f'{cursor:019d}')]
            page, more = rows[:limit], len(rows) > limit
        else:
            from boto3.dynamodb.conditions import Key
            args = {'KeyConditionExpression': Key('pk').eq(pk) & Key('sk').begins_with(PREFIX),
                    'ConsistentRead': True, 'Limit': limit}
            if cursor is not None:
                args['ExclusiveStartKey'] = {'pk': pk, 'sk': PREFIX + f'{cursor:019d}'}
            response = self.store.table.query(**args)
            page, more = _api_numbers(response.get('Items', [])), bool(response.get('LastEvaluatedKey'))
        values = []
        for row in page:
            version = _integer(row.get('version'), minimum=1)
            values.append(_record(row, pk=pk, version=version))
        if more and not values:
            raise ValueError('recipe review page did not advance')
        return {'items': values, 'next_cursor': values[-1]['version'] if more else None}

    def candidate(self, pk, review_version):
        from .recipe_candidate import CANDIDATE_PREFIX, validate_candidate
        _scope(pk)
        _integer(review_version, minimum=1)
        raw = self._get(pk, CANDIDATE_PREFIX + f'{review_version:019d}')
        if raw is None:
            return None
        review = _record(self._get(pk, PREFIX + f'{review_version:019d}'), pk=pk, version=review_version)
        if type(raw) is not dict or raw.get('pk') != pk or raw.get('sk') != CANDIDATE_PREFIX + f'{review_version:019d}':
            raise ValueError('invalid stored recipe candidate')
        return validate_candidate({k: v for k, v in raw.items() if k not in {'pk', 'sk'}}, review)

    def _approved_candidate(self, pk, record):
        from .recipe_candidate import build_candidate
        if record['decision'] != 'APPROVE':
            return None
        job = self._get(pk, 'META')
        if not job or job.get('owner_subject') != _scope(pk)[0] or job.get('version') != record['job_version']:
            raise Conflict
        return build_candidate(record, job)

    def save(self, pk, expected_job_version, expected_review_version, record, expected_epoch, *, fryer_conditions=None):
        subject, job_id = _scope(pk)
        _integer(expected_job_version, minimum=1)
        _integer(expected_review_version)
        _integer(expected_epoch)
        record = validate_review(record)
        if record['job_id'] != job_id or record['job_version'] != expected_job_version or \
                record['version'] != expected_review_version + 1:
            raise ValueError('invalid recipe review binding')
        if fryer_conditions is not None:
            if type(fryer_conditions) is not TonkatsuTransactionConditions or not isinstance(self.store, DynamoCookingStore):
                raise ValueError("atomic fryer review storage required")
            scope = fryer_conditions.scope
            if (
                fryer_conditions.auth != self.store.auth_table_name or fryer_conditions.cooking != self.store.table.name
                or scope.history.history.owner != subject or scope.binding.auth_epoch != expected_epoch
                or scope.execution_id != record["execution_id"] or scope.binding.current.job_id != job_id
                or scope.binding.current.job_version != expected_job_version or record["proposal"]["menu"] != "TONKATSU"
            ):
                raise Conflict
        from .recipe_candidate import CANDIDATE_PREFIX
        sk = PREFIX + f"{record['version']:019d}"
        candidate_sk = CANDIDATE_PREFIX + f"{record['version']:019d}"
        if isinstance(self.store, MemoryCookingStore):
            with self.store.lock:
                meta = self.store.items.get((pk, 'META'))
                if not meta or meta.get('version') != expected_job_version or meta.get('owner_subject') != subject:
                    raise Conflict
                existing = self.store.items.get((pk, sk))
                if existing is not None:
                    value = _record(existing, pk=pk, version=record['version'])
                    if _same(value, record):
                        if value['decision'] == 'APPROVE' and self.candidate(pk, value['version']) is None:
                            raise Conflict
                        return value
                    raise Conflict
                pointer = self.current(pk)
                if pointer['version'] != expected_review_version:
                    raise Conflict
                candidate = self._approved_candidate(pk, record)
                if (pk, candidate_sk) in self.store.items:
                    raise Conflict
                self.store.items[pk, sk] = copy.deepcopy({'pk': pk, 'sk': sk, **record})
                self.store.items[pk, CURRENT] = {'pk': pk, 'sk': CURRENT, 'version': record['version']}
                if candidate is not None:
                    self.store.items[pk, candidate_sk] = copy.deepcopy({'pk': pk, 'sk': candidate_sk, **candidate})
                return copy.deepcopy(record)
        candidate = self._approved_candidate(pk, record)
        encode = self.store._item
        auth = self.store._owner_guard(subject, expected_epoch)
        auth['ConditionCheck']['ReturnValuesOnConditionCheckFailure'] = 'NONE'
        guards = [auth, {'ConditionCheck': {'TableName': self.store.table.name,
            'Key': encode({'pk': pk, 'sk': 'META'}),
            'ConditionExpression': '#version = :expected AND owner_subject = :owner AND job_id = :job',
            'ExpressionAttributeNames': {'#version': 'version'},
            'ExpressionAttributeValues': encode({':expected': expected_job_version, ':owner': subject, ':job': job_id}),
            'ReturnValuesOnConditionCheckFailure': 'NONE'}}]
        if fryer_conditions is not None:
            guards = fryer_conditions.review_checks()
        pointer_values = {':next': record['version']}
        if expected_review_version:
            pointer_values[':expected'] = expected_review_version
        candidate_actions = [] if candidate is None else [{'Put': {'TableName': self.store.table.name,
            'Item': encode({'pk': pk, 'sk': candidate_sk, **candidate}),
            'ConditionExpression': 'attribute_not_exists(pk)', 'ReturnValuesOnConditionCheckFailure': 'NONE'}}]
        try:
            self.store.client.transact_write_items(TransactItems=[*guards,
                {'Update': {'TableName': self.store.table.name, 'Key': encode({'pk': pk, 'sk': CURRENT}),
                    'UpdateExpression': 'SET #version = :next',
                    'ConditionExpression': '#version = :expected' if expected_review_version else 'attribute_not_exists(pk)',
                    'ExpressionAttributeNames': {'#version': 'version'},
                    'ExpressionAttributeValues': encode(pointer_values), 'ReturnValuesOnConditionCheckFailure': 'NONE'}},
                {'Put': {'TableName': self.store.table.name, 'Item': encode({'pk': pk, 'sk': sk, **record}),
                    'ConditionExpression': 'attribute_not_exists(pk)', 'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            ] + candidate_actions)
        except self.store.client.exceptions.TransactionCanceledException as error:
            if not _is_transaction_contention(error):
                raise
            reasons = error.response.get('CancellationReasons') or []
            if len(reasons) != len(guards) + 2 + len(candidate_actions) or any(
                reason.get('Code') not in {None, 'None'} for reason in reasons[:len(guards)]
            ):
                raise Conflict from None
            existing = self._get(pk, sk)
            if existing is not None:
                value = _record(existing, pk=pk, version=record['version'])
                if _same(value, record):
                    if value['decision'] == 'APPROVE' and self.candidate(pk, value['version']) is None:
                        raise Conflict from None
                    return value
            raise Conflict from None
        return copy.deepcopy(record)
