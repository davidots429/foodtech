"""Explicit immutable programme candidates from current approved Combi reviews.

Preservation never applies a programme, copies food-safety results, changes a
meal or invents a baseline recipe label for an unlabeled source batch.
"""
import copy
from datetime import datetime, timezone
from .combi_review_store import CombiReviewStore, _scope, _keys, _record, validate_combi_review
from .cooking_store import Conflict, MemoryCookingStore, _is_transaction_contention
from .recipe_review_store import _integer


def candidate_key(menu, version):
    _keys(menu); _integer(version, minimum=1)
    return f'RECIPE_CANDIDATE#{menu}#REVIEW#{version:019d}'


def _instant(value):
    if type(value) is not str:
        raise ValueError('invalid candidate instant')
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.utcoffset() != timezone.utc.utcoffset(instant):
        raise ValueError('UTC candidate instant required')
    return instant


def build_combi_candidate(review, meta, recorded_at):
    review = validate_combi_review(review)
    menu = review['menu']
    job_field = 'rice_cooking_job_id' if menu == 'RICE' else 'mackerel_cooking_job_id'
    if review['decision'] != 'APPROVE' or type(meta) is not dict or type(meta.get('version')) is not int or \
            (meta.get('meal_batch_id'), meta['version'], meta.get(job_field), meta.get('provenance')) != \
            (review['meal_id'], review['job_version'], review['job_id'], 'SYNTHETIC') or \
            meta.get('equipment_size') not in ('MEDIUM_FIXED_RACK_10', 'LARGE_ROLL_IN_RACK_20'):
        raise ValueError('approved current synthetic Combi review required')
    base = meta.get(menu.lower() + '_recipe_version')
    if base is not None and (type(base) is not str or not 1 <= len(base) <= 80 or base != base.strip()):
        raise ValueError('invalid source recipe version')
    if _instant(recorded_at) < _instant(review['reviewed_at']):
        raise ValueError('candidate predates review')
    return {'schema_version': 'combi-recipe-candidate-v1',
        'candidate_id': f"combi-{review['meal_id']}-{menu.lower()}-r{review['version']}",
        'status': 'CANDIDATE_NOT_APPLIED', 'provenance': 'SYNTHETIC', 'base_recipe_version': base,
        'equipment_size': meta['equipment_size'], 'meal_id': review['meal_id'], 'menu': menu,
        'job_id': review['job_id'], 'job_version': review['job_version'], 'execution_id': review['execution_id'],
        'review_version': review['version'], 'review_request_id': review['request_id'],
        'proposal_digest': review['proposal_digest'], 'proposal': copy.deepcopy(review['proposal']),
        'reviewed_at': review['reviewed_at'], 'recorded_at': recorded_at}


def validate_combi_candidate(value, review):
    review = validate_combi_review(review)
    if type(value) is not dict:
        raise ValueError('invalid stored Combi candidate')
    menu = review['menu']
    expected = build_combi_candidate(review, {'meal_batch_id': review['meal_id'], 'version': review['job_version'],
        'provenance': 'SYNTHETIC', ('rice_cooking_job_id' if menu == 'RICE' else 'mackerel_cooking_job_id'): review['job_id'],
        'equipment_size': value.get('equipment_size'), menu.lower() + '_recipe_version': value.get('base_recipe_version')},
        value.get('recorded_at'))
    if expected != value:
        raise ValueError('Combi candidate changed or mismatched')
    return copy.deepcopy(expected)


class CombiCandidateStore:
    def __init__(self, store):
        self.store = store
        self.reviews = CombiReviewStore(store)

    def review(self, pk, menu, version):
        _scope(pk, menu)
        _integer(version, minimum=1)
        return _record(self.reviews._get(pk, _keys(menu)[1] + f'{version:019d}'), pk, menu, version)

    def get(self, pk, menu, version):
        _scope(pk, menu)
        key = candidate_key(menu, version)
        raw = self.reviews._get(pk, key)
        if raw is None:
            return None
        if type(raw) is not dict or raw.get('pk') != pk or raw.get('sk') != key:
            raise ValueError('invalid Combi candidate storage scope')
        return validate_combi_candidate({k:v for k,v in raw.items() if k not in {'pk','sk'}}, self.review(pk,menu,version))

    def _source(self, pk, menu, version, expected_meal_version, digest):
        subject, meal = _scope(pk, menu)
        review = self.review(pk, menu, version)
        meta = self.reviews._get(pk, 'META')
        pointer = self.reviews.current(pk, menu)
        if pointer['version'] != version or pointer['review'] != review or review['decision'] != 'APPROVE' or \
                review['job_version'] != expected_meal_version or review['proposal_digest'] != digest or \
                type(meta) is not dict or meta.get('pk') != pk or meta.get('sk') != 'META' or \
                type(meta.get('version')) is not int or meta['version'] != expected_meal_version or \
                meta.get('meal_batch_id') != meal or meta.get('provenance') != 'SYNTHETIC':
            raise Conflict
        return subject, review, meta

    def preserve(self, pk, menu, version, expected_meal_version, digest, recorded_at, expected_epoch):
        _integer(version, minimum=1); _integer(expected_meal_version, minimum=1); _integer(expected_epoch)
        if expected_meal_version > 10**18:
            raise ValueError('invalid Combi meal version')
        if isinstance(self.store, MemoryCookingStore):
            with self.store.lock:
                _, review, meta = self._source(pk, menu, version, expected_meal_version, digest)
                candidate = build_combi_candidate(review, meta, recorded_at)
                existing = self.get(pk, menu, version)
                if existing is not None:
                    if {k:v for k,v in candidate.items() if k != 'recorded_at'} != \
                            {k:v for k,v in existing.items() if k != 'recorded_at'}:
                        raise Conflict
                    return existing
                sk = candidate_key(menu, version)
                self.store.items[pk, sk] = copy.deepcopy({'pk':pk, 'sk':sk, **candidate})
                return copy.deepcopy(candidate)
        if not self.store.auth_table_name or self.store.auth_table_name == self.store.table.name or not self.store.require_principal:
            raise ValueError('shared active principal storage required')
        subject, review, meta = self._source(pk, menu, version, expected_meal_version, digest)
        candidate = build_combi_candidate(review, meta, recorded_at)
        encode = self.store._item
        def check(sk, values, absent=()):
            fields = sorted(values)
            names = {f'#f{i}':name for i,name in enumerate(fields)}
            params = {f':v{i}':values[name] for i,name in enumerate(fields)}
            parts = [f'#f{i} = :v{i}' for i in range(len(fields))]
            for i,name in enumerate(absent):
                names[f'#absent{i}'] = name; parts.append(f'attribute_not_exists(#absent{i})')
            return {'ConditionCheck': {'TableName':self.store.table.name, 'Key':encode({'pk':pk,'sk':sk}),
                'ConditionExpression':' AND '.join(parts), 'ExpressionAttributeNames':names,
                'ExpressionAttributeValues':encode(params), 'ReturnValuesOnConditionCheckFailure':'NONE'}}
        base_field = menu.lower() + '_recipe_version'
        meta_fields = ('meal_batch_id','version','provenance','equipment_size',
                       'rice_cooking_job_id' if menu == 'RICE' else 'mackerel_cooking_job_id')
        meta_values = {key:meta[key] for key in meta_fields}
        if base_field in meta:
            meta_values[base_field] = meta[base_field]
        auth = self.store._owner_guard(subject, expected_epoch)
        auth['ConditionCheck']['ReturnValuesOnConditionCheckFailure'] = 'NONE'
        actions = [auth, check('META',meta_values, () if base_field in meta else (base_field,)),
            check(_keys(menu)[0], {'version':version}), check(_keys(menu)[1]+f'{version:019d}',review),
            {'Put': {'TableName':self.store.table.name, 'Item':encode({'pk':pk,'sk':candidate_key(menu,version),**candidate}),
                'ConditionExpression':'attribute_not_exists(pk)', 'ReturnValuesOnConditionCheckFailure':'NONE'}}]
        try:
            self.store.client.transact_write_items(TransactItems=actions)
        except self.store.client.exceptions.TransactionCanceledException as error:
            if not _is_transaction_contention(error):
                raise
            reasons = error.response.get('CancellationReasons') or []
            if len(reasons) != 5 or any(r.get('Code') not in {None,'None'} for r in reasons[:4]):
                raise Conflict from None
            existing = self.get(pk,menu,version)
            if existing is not None and {k:v for k,v in existing.items() if k != 'recorded_at'} == \
                    {k:v for k,v in candidate.items() if k != 'recorded_at'}:
                return existing
            raise Conflict from None
        return copy.deepcopy(candidate)
