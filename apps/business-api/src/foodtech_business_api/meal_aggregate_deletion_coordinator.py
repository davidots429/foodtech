"""Resumable meal aggregate cleanup; verification is not account deletion.

No writer-fencing proof is connected yet, so this coordinator cannot mutate.
"""
import json

from .meal_aggregate_group_deletion import clean_one_meal_aggregate_group


class MealAggregateDeletionCoordinator:
    @staticmethod
    def _writer_quiescence_proven():
        return False

    def __init__(self, *, inventory, worker, residual):
        if worker.inventory is not inventory or residual.source_table is not inventory.source_table or \
                residual.progress_store is not inventory.progress_store or residual.cipher is not inventory.cipher or \
                getattr(residual, 'DOMAIN', None) != 'MEAL_AGGREGATE_RESIDUAL':
            raise ValueError('meal aggregate deletion dependencies do not match')
        self.inventory, self.worker, self.residual = inventory, worker, residual
        self.store, self.cipher = inventory.progress_store, inventory.cipher

    def step(self, *, subject, request_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid meal aggregate deletion limit')
        scope = {'subject': subject, 'request_id': request_id, 'domain': 'MEAL_AGGREGATE_COORDINATOR'}
        item = self.inventory._load(subject, request_id, scope['domain'])
        if item is None:
            state, version = {'schema': 1, 'phase': 'CAPTURE', 'index': 0, 'total': 0}, 0
        else:
            state, version = self.inventory._json(item['checkpoint']), item['version']
            if set(state) != {'schema', 'phase', 'index', 'total'} or type(state['schema']) is not int or \
                    state['schema'] != 1 or state['phase'] not in {'CAPTURE', 'DELETE', 'GROUPS', 'RESIDUAL', 'FINAL_GROUPS', 'VERIFYING'} or \
                    type(state['index']) is not int or type(state['total']) is not int or \
                    not 0 <= state['index'] <= state['total'] < 10**12 or \
                    (state['phase'] == 'CAPTURE' and (state['index'] or state['total'])) or \
                    (state['phase'] in {'GROUPS', 'RESIDUAL', 'FINAL_GROUPS', 'VERIFYING'} and state['index'] != state['total']) or \
                    type(version) is not int or version < 1 or \
                    item['state'] != ('VERIFYING' if state['phase'] == 'VERIFYING' else 'IN_PROGRESS'):
                raise ValueError('invalid meal aggregate deletion checkpoint')
        if not self._writer_quiescence_proven():
            return {'phase': 'QUIESCENCE', 'writer_quiescence_required': True}
        phase = state['phase']
        if phase == 'VERIFYING':
            return {'phase': 'VERIFYING'}
        if phase == 'CAPTURE':
            found = self.inventory.capture_page(subject=subject, request_id=request_id, limit=limit)
            if found['complete']:
                state.update(phase='DELETE', total=found['count'])
        elif phase == 'DELETE':
            if state['index'] < state['total']:
                result = self.worker.delete_one(subject=subject, request_id=request_id, index=state['index'])
                if result.get('state') != 'VERIFYING':
                    raise ValueError('invalid meal aggregate cleanup result')
                state['index'] += 1
            if state['index'] == state['total']:
                state['phase'] = 'GROUPS'
        elif phase in {'GROUPS', 'FINAL_GROUPS'}:
            result = clean_one_meal_aggregate_group(source_table=self.inventory.source_table,
                progress_store=self.store, cipher=self.cipher, subject=subject, request_id=request_id)
            if (result.get('state'), result.get('removed')) == ('VERIFYING', False):
                state['phase'] = 'RESIDUAL' if phase == 'GROUPS' else 'VERIFYING'
            elif (result.get('state'), result.get('removed')) != ('IN_PROGRESS', True):
                raise ValueError('invalid meal aggregate group cleanup result')
        else:
            result = self.residual.observe_page(subject=subject, request_id=request_id, limit=limit)
            if result['residual_found']:
                return {'phase': 'RESIDUAL', 'residual_found': True}
            if result['scan_complete']:
                state['phase'] = 'FINAL_GROUPS'
        raw = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        self.store.save_checkpoint(cipher=self.cipher, **scope, expected_version=version,
            state='VERIFYING' if state['phase'] == 'VERIFYING' else 'IN_PROGRESS', checkpoint=raw)
        return {'phase': state['phase']}
