"""Coordinate guarded profile, job and meal cleanup without completing deletion.

Only a separately authenticated and authorized maintenance worker may call
this internal adapter. Writer quiescence, other stores, encrypted snapshots
and backups require independent handling before account deletion can complete.
"""
import json


class CookingDeletionRecovery:
    def __init__(self, *, profile, job, meal, progress_store, cipher):
        for recovery in (profile, job, meal):
            coordinator = recovery.coordinator
            if coordinator.store is not progress_store or coordinator.cipher is not cipher or \
                    recovery.recovery.store.auth_table != progress_store.auth_table or \
                    recovery.recovery.store.registry_table != progress_store.registry_table:
                raise ValueError('cooking deletion recovery dependencies do not match')
        self.profile, self.job, self.meal = profile, job, meal
        self.store, self.cipher = progress_store, cipher

    def step(self, *, authenticated_subject, request_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid cooking deletion limit')
        scope = {'subject': authenticated_subject, 'request_id': request_id, 'domain': 'COOKING_RECOVERY'}
        item = self.store.load_checkpoint(cipher=self.cipher, **scope)
        if item is None:
            phase, version = 'PROFILE', 0
        else:
            try:
                state = json.loads(item['checkpoint'])
                if not isinstance(state, dict) or set(state) != {'schema', 'phase'} or \
                        type(state['schema']) is not int or state['schema'] != 1 or \
                        state['phase'] not in {'PROFILE', 'JOB', 'MEAL', 'VERIFYING'} or \
                        type(item['version']) is not int or item['version'] < 1 or \
                        item['state'] != ('VERIFYING' if state['phase'] == 'VERIFYING' else 'IN_PROGRESS'):
                    raise ValueError
                phase, version = state['phase'], item['version']
            except (KeyError, TypeError, ValueError, UnicodeError):
                raise ValueError('invalid cooking deletion checkpoint') from None
        if phase == 'VERIFYING':
            return {'phase': 'VERIFYING'}
        recovery = {'PROFILE': self.profile, 'JOB': self.job, 'MEAL': self.meal}[phase]
        result = recovery.step(authenticated_subject=authenticated_subject, request_id=request_id, limit=limit)
        if not isinstance(result, dict) or not isinstance(result.get('phase'), str):
            raise ValueError('invalid cooking cleanup result')
        if result.get('residual_found') or result.get('source_ownership_required'):
            return {'phase': phase, 'domain_phase': result['phase'],
                    **({'residual_found': True} if result.get('residual_found') else {}),
                    **({'source_ownership_required': True} if result.get('source_ownership_required') else {})}
        if result['phase'] != 'VERIFYING':
            return {'phase': phase, 'domain_phase': result['phase']}
        next_phase = {'PROFILE': 'JOB', 'JOB': 'MEAL', 'MEAL': 'VERIFYING'}[phase]
        raw = json.dumps({'schema': 1, 'phase': next_phase}, sort_keys=True, separators=(',', ':')).encode('ascii')
        self.store.save_checkpoint(cipher=self.cipher, **scope, expected_version=version,
            state='VERIFYING' if next_phase == 'VERIFYING' else 'IN_PROGRESS', checkpoint=raw)
        return {'phase': next_phase}
