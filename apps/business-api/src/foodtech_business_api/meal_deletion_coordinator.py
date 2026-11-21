"""Resumable meal cleanup; VERIFYING is not a deletion receipt.

An authenticated worker must keep all legacy writers stopped. Snapshots,
source ownership, other stores and backups need separate handling.
"""
import json

from .meal_listing_deletion import clean_one_meal_listing


class MealDeletionCoordinator:
    def __init__(self, *, meals, worker, residual, metadata, final_residual, sources):
        if worker.inventory is not meals or metadata.inventory is not meals or metadata.residual is not residual or \
                sources.inventory is not meals or getattr(residual, 'DOMAIN', None) != 'MEAL_RESIDUAL' or \
                getattr(final_residual, 'DOMAIN', None) != 'MEAL_FINAL_RESIDUAL' or \
                any(check.source_table is not meals.source_table or check.progress_store is not meals.progress_store or
                    check.cipher is not meals.cipher for check in (residual, final_residual)):
            raise ValueError('meal deletion dependencies do not match')
        self.meals, self.worker, self.residual = meals, worker, residual
        self.metadata, self.final_residual, self.sources = metadata, final_residual, sources
        self.store, self.cipher = meals.progress_store, meals.cipher

    def step(self, *, subject, request_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid meal deletion limit')
        scope = {'subject': subject, 'request_id': request_id, 'domain': 'MEAL_COORDINATOR'}
        item = self.meals._load(subject, request_id, scope['domain'])
        if item is None:
            state, version = {'schema': 1, 'phase': 'CAPTURE', 'index': 0, 'total': 0}, 0
        else:
            state, version = self.meals._json(item['checkpoint']), item['version']
            if set(state) != {'schema', 'phase', 'index', 'total'} or type(state['schema']) is not int or \
                    state['schema'] != 1 or state['phase'] not in {
                        'CAPTURE', 'RECORDS', 'RESIDUAL', 'META', 'LISTINGS', 'FINAL', 'SOURCES', 'VERIFYING'} or \
                    type(state['index']) is not int or type(state['total']) is not int or \
                    not 0 <= state['index'] <= state['total'] < 10**12 or \
                    (state['phase'] == 'CAPTURE' and (state['index'] != 0 or state['total'] != 0)) or \
                    (state['phase'] in {'RESIDUAL', 'LISTINGS', 'FINAL', 'VERIFYING'} and state['index'] != state['total']) or \
                    type(version) is not int or version < 1 or \
                    item['state'] != ('VERIFYING' if state['phase'] == 'VERIFYING' else 'IN_PROGRESS'):
                raise ValueError('invalid meal deletion checkpoint')
        phase = state['phase']
        if phase == 'VERIFYING':
            return {'phase': 'VERIFYING'}
        if phase == 'CAPTURE':
            result = self.meals.capture_page(subject=subject, request_id=request_id, limit=limit)
            if result['complete']:
                state.update(phase='RECORDS', total=result['count'])
        elif phase == 'RECORDS':
            if state['index'] < state['total']:
                result = self.worker.delete_records(subject=subject, request_id=request_id, index=state['index'], limit=min(limit, 20))
                if (result.get('state'), result.get('removed')) == ('META_ONLY', False):
                    state['index'] += 1
                elif (result.get('state'), result.get('removed')) != ('IN_PROGRESS', True):
                    raise ValueError('invalid meal cleanup result')
            if state['index'] == state['total']:
                state['phase'] = 'RESIDUAL'
        elif phase in {'RESIDUAL', 'FINAL'}:
            check = self.residual if phase == 'RESIDUAL' else self.final_residual
            result = check.observe_page(subject=subject, request_id=request_id, limit=limit)
            if result['residual_found']:
                return {'phase': phase, 'residual_found': True}
            if result['scan_complete']:
                if phase == 'RESIDUAL':
                    state.update(phase='META', index=0)
                else:
                    state.update(phase='SOURCES', index=0)
        elif phase == 'META':
            if state['index'] < state['total']:
                count = min(limit, 20, state['total'] - state['index'])
                result = self.metadata.remove_batch(subject=subject, request_id=request_id, start=state['index'], count=count)
                if result != {'state': 'VERIFYING', 'count': count}:
                    raise ValueError('invalid meal metadata cleanup result')
                state['index'] += count
            if state['index'] == state['total']:
                state['phase'] = 'LISTINGS'
        elif phase == 'LISTINGS':
            result = clean_one_meal_listing(source_table=self.meals.source_table, progress_store=self.store,
                cipher=self.cipher, subject=subject, request_id=request_id)
            if (result.get('state'), result.get('removed')) == ('VERIFYING', False):
                state['phase'] = 'FINAL'
            elif (result.get('state'), result.get('removed')) != ('IN_PROGRESS', True):
                raise ValueError('invalid meal listing cleanup result')
        elif phase == 'SOURCES':
            if state['index'] < state['total']:
                result = self.sources.inspect(subject=subject, request_id=request_id, index=state['index'])
                if result.get('state') == 'SOURCE_OWNERSHIP_REQUIRED':
                    return {'phase': 'SOURCES', 'source_ownership_required': True}
                if result.get('state') != 'NO_BOUND_SOURCE':
                    raise ValueError('invalid meal source inspection result')
                state['index'] += 1
            if state['index'] == state['total']:
                state['phase'] = 'VERIFYING'
        raw = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        self.store.save_checkpoint(cipher=self.cipher, **scope, expected_version=version,
            state='VERIFYING' if state['phase'] == 'VERIFYING' else 'IN_PROGRESS', checkpoint=raw)
        return {'phase': state['phase']}
