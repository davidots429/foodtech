"""Atomically create a fresh owned meal from an approved programme candidate.

Existing-target recovery is a read: it validates the exact original request and
immutable selection without resetting a meal which has already advanced. The
BFF must authenticate/recheck active principal and session around these calls.
Fresh creation additionally fences authorization and all source rows in the
same transaction as records, listing and history revision.
"""
import copy

from .combi_candidate_meal import CandidateMealRequest, candidate_meal_records, request_hash
from .combi_recipe_candidate import CombiCandidateStore, candidate_key, validate_combi_candidate
from .combi_review_store import _keys
from .cooking_store import Conflict, DynamoCookingStore, MemoryCookingStore, _meal_listing, _is_transaction_contention
from .recipe_review_store import _integer
from .subject_identity import valid_subject


class CombiCandidateMealStore:
    def __init__(self, store):
        if not isinstance(store, (MemoryCookingStore, DynamoCookingStore)):
            raise ValueError('explicit cooking storage dependency required')
        self.store = store
        self.candidates = CombiCandidateStore(store)

    def _get(self, pk, sk):
        return self.candidates.reviews._get(pk, sk)

    def _reconcile(self, subject, version, body):
        target = f'SCOPE#USER#{subject}#COMBI_MEAL#{body.request_id}'
        meta = self._get(target, 'META')
        if meta is None:
            return None
        try:
            if type(meta) is not dict or meta.get('pk') != target or meta.get('sk') != 'META' or \
                    meta.get('meal_batch_id') != str(body.request_id) or meta.get('provenance') != 'SYNTHETIC':
                raise ValueError
            _integer(meta.get('version'), minimum=1)
            selection = meta.get('recipe_candidate_selection')
            if type(selection) is not dict or set(selection) != {'schema_version','request_id','request_hash',
                    'source_meal_id','source_meal_version','source_job_id','menu','review_version','candidate','review'} or \
                    selection.get('schema_version') != 'combi-candidate-selection-v1' or \
                    (selection.get('request_id'),selection.get('source_meal_id'),selection.get('source_meal_version'),
                     selection.get('menu'),selection.get('review_version')) != \
                    (str(body.request_id),str(body.meal_id),body.expected_meal_version,body.menu,version):
                raise ValueError
            candidate = validate_combi_candidate(selection['candidate'], selection['review'])
            expected = candidate_meal_records(body, candidate, selection['review'], meta['created_at'])
            if selection != expected[0]['recipe_candidate_selection'] or \
                    selection['source_job_id'] != candidate['job_id'] or \
                    selection['request_hash'] != request_hash(body,candidate):
                raise ValueError
            # These references and programme intents are immutable, while status,
            # observations, quality, source binding and counters may advance.
            for key in ('meal_batch_id','rice_cooking_job_id','mode_transition_id','mackerel_cooking_job_id',
                        'created_at','equipment_size','rice_recipe_version','mackerel_recipe_version'):
                if meta.get(key) != expected[0].get(key) or (key in meta) != (key in expected[0]):
                    raise ValueError
            for row in expected[1:]:
                actual = self._get(target, row['sk'])
                if type(actual) is not dict or actual.get('pk') != target or actual.get('sk') != row['sk'] or \
                        actual.get('meal_batch_id') != str(body.request_id):
                    raise ValueError
                for name in ('job_id','transition_id','menu'):
                    if name in row and actual.get(name) != row[name]:
                        raise ValueError
                if row.get('menu') == body.menu:
                    planned = ['program_celsius','climate_setpoint_percent','fan_level','target_program_seconds','planned_pan_positions']
                    if body.menu == 'RICE':
                        planned.extend(('target_effective_water_ratio','target_soaking_seconds','target_draining_seconds'))
                    for name in planned:
                        if actual.get(name) != row.get(name):
                            raise ValueError
                if row['sk'].startswith('TRANSITION#') and 'target_duration_seconds' in row and \
                        actual.get('target_duration_seconds') != row['target_duration_seconds']:
                    raise ValueError
            listing = _meal_listing(target,expected)
            if self._get(listing['pk'],listing['sk']) != listing:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Conflict from None
        return False

    def _fresh_source(self, subject, version, body):
        source = f'SCOPE#USER#{subject}#COMBI_MEAL#{body.meal_id}'
        owner, review, meta = self.candidates._source(source,body.menu,version,
            body.expected_meal_version,body.proposal_digest)
        candidate = self.candidates.get(source,body.menu,version)
        if candidate is None or owner != subject:
            raise Conflict
        job_field = 'rice_cooking_job_id' if body.menu == 'RICE' else 'mackerel_cooking_job_id'
        if (meta.get(job_field),meta.get('equipment_size'),meta.get(body.menu.lower()+'_recipe_version')) != \
                (candidate['job_id'],candidate['equipment_size'],candidate['base_recipe_version']):
            raise Conflict
        return source,review,meta,candidate

    def create(self, subject, version, body, now, expected_epoch):
        if not valid_subject(subject) or type(body) is not CandidateMealRequest:
            raise ValueError('validated scoped candidate meal request required')
        _integer(version,minimum=1); _integer(expected_epoch)
        if isinstance(self.store, MemoryCookingStore):
            with self.store.lock:
                recovered = self._reconcile(subject,version,body)
                if recovered is not None:
                    return recovered
                _,review,_,candidate = self._fresh_source(subject,version,body)
                records = candidate_meal_records(body,candidate,review,now)
                target = f'SCOPE#USER#{subject}#COMBI_MEAL#{body.request_id}'
                self.store.create_meal(target,copy.deepcopy(records),expected_epoch)
                return True
        if not self.store.auth_table_name or self.store.auth_table_name == self.store.table.name or not self.store.require_principal:
            raise ValueError('shared active principal storage required')
        recovered = self._reconcile(subject,version,body)
        if recovered is not None:
            return recovered
        source,review,meta,candidate = self._fresh_source(subject,version,body)
        records = candidate_meal_records(body,candidate,review,now)
        target = f'SCOPE#USER#{subject}#COMBI_MEAL#{body.request_id}'
        encode = self.store._item
        def check(sk,values,absent=()):
            fields = sorted(values)
            names = {f'#f{i}':name for i,name in enumerate(fields)}
            args = {f':v{i}':values[name] for i,name in enumerate(fields)}
            parts = [f'#f{i} = :v{i}' for i in range(len(fields))]
            for i,name in enumerate(absent):
                names[f'#absent{i}']=name;parts.append(f'attribute_not_exists(#absent{i})')
            return {'ConditionCheck': {'TableName':self.store.table.name, 'Key':encode({'pk':source,'sk':sk}),
                'ConditionExpression':' AND '.join(parts), 'ExpressionAttributeNames':names,
                'ExpressionAttributeValues':encode(args), 'ReturnValuesOnConditionCheckFailure':'NONE'}}
        base = body.menu.lower()+'_recipe_version'
        meta_fields = ('meal_batch_id','version','provenance','equipment_size',
            'rice_cooking_job_id' if body.menu == 'RICE' else 'mackerel_cooking_job_id')
        meta_values = {key:meta[key] for key in meta_fields}
        if base in meta:
            meta_values[base]=meta[base]
        auth = self.store._owner_guard(subject,expected_epoch)
        actions = [auth,check('META',meta_values,() if base in meta else (base,)),
            check(_keys(body.menu)[0],{'version':version}),
            check(_keys(body.menu)[1]+f'{version:019d}',review),
            check(candidate_key(body.menu,version),candidate)]
        actions.extend({'Put': {'TableName':self.store.table.name,'Item':encode({'pk':target,**row}),
            'ConditionExpression':'attribute_not_exists(pk)'}} for row in records)
        actions.append({'Put': {'TableName':self.store.table.name,'Item':encode(_meal_listing(target,records)),
            'ConditionExpression':'attribute_not_exists(pk)'}})
        actions.append(self.store._meal_history_update(target))
        for action in actions:
            next(iter(action.values()))['ReturnValuesOnConditionCheckFailure']='NONE'
        try:
            self.store._history_transact(actions)
        except self.store.client.exceptions.TransactionCanceledException as error:
            if not _is_transaction_contention(error):
                raise
            reasons = error.response.get('CancellationReasons') or []
            if len(reasons) != 11 or any(r.get('Code') not in {None,'None'} for r in reasons[:5]):
                raise Conflict from None
            recovered = self._reconcile(subject,version,body)
            if recovered is not None:
                return recovered
            raise Conflict from None
        return True
