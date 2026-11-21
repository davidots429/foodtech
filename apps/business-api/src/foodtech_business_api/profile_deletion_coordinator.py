"""Bounded orchestration of profile deletion; not an authorization boundary.

An authenticated maintenance job must stop legacy writers for the entire run.
VERIFYING means only this local scan finished, never account deletion completion.
"""
import json

from .profile_orphan_cleanup import clean_one_orphan


class ProfileDeletionCoordinator:
    def __init__(self, *, inventory, worker, residual):
        if worker.inventory is not inventory or residual.source_table is not inventory.source_table or \
                residual.progress_store is not inventory.progress_store or residual.cipher is not inventory.cipher:
            raise ValueError('profile coordinator dependencies do not match')
        self.inventory, self.worker, self.residual = inventory, worker, residual
        self.store, self.cipher = inventory.progress_store, inventory.cipher

    def step(self, *, subject, request_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid profile deletion limit')
        scope = {'subject': subject, 'request_id': request_id, 'domain': 'PROFILE_COORDINATOR'}
        record = self.inventory._load(subject, request_id, scope['domain'])
        if record is None:
            data, version = {'schema': 1, 'phase': 'CAPTURE', 'index': 0, 'total': 0}, 0
        else:
            data, version = self.inventory._json(record['checkpoint']), record['version']
            if set(data) != {'schema', 'phase', 'index', 'total'} or type(data['schema']) is not int or \
                    data['schema'] != 1 or data['phase'] not in {'CAPTURE', 'DELETE', 'ORPHANS', 'RESIDUAL', 'VERIFYING'} or \
                    type(data['index']) is not int or type(data['total']) is not int or \
                    not 0 <= data['index'] <= data['total'] < 10**12 or \
                    (data['phase'] == 'CAPTURE' and (data['index'] != 0 or data['total'] != 0)) or \
                    (data['phase'] in {'ORPHANS', 'RESIDUAL', 'VERIFYING'} and data['index'] != data['total']) or \
                    type(version) is not int or version < 1 or \
                    record['state'] != ('VERIFYING' if data['phase'] == 'VERIFYING' else 'IN_PROGRESS'):
                raise ValueError('invalid profile coordinator checkpoint')
        if data['phase'] == 'VERIFYING':
            return {'phase': 'VERIFYING', 'residual_found': False}
        if data['phase'] == 'CAPTURE':
            result = self.inventory.capture_page(subject=subject, request_id=request_id, limit=limit)
            if result['complete']:
                data.update(phase='DELETE', total=result['count'])
        elif data['phase'] == 'DELETE':
            if data['index'] < data['total']:
                self.worker.delete_one(subject=subject, request_id=request_id, index=data['index'])
                data['index'] += 1
            if data['index'] == data['total']:
                data['phase'] = 'ORPHANS'
        elif data['phase'] == 'ORPHANS':
            result = clean_one_orphan(source_table=self.inventory.source_table, progress_store=self.store,
                                      cipher=self.cipher, subject=subject, request_id=request_id)
            if not result['removed']:
                data['phase'] = 'RESIDUAL'
        else:
            result = self.residual.observe_page(subject=subject, request_id=request_id, limit=limit)
            if result['residual_found']:
                return {'phase': 'RESIDUAL', 'residual_found': True}
            if result['scan_complete']:
                data['phase'] = 'VERIFYING'
        raw = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        self.store.save_checkpoint(cipher=self.cipher, **scope, expected_version=version,
            state='VERIFYING' if data['phase'] == 'VERIFYING' else 'IN_PROGRESS', checkpoint=raw)
        return {'phase': data['phase'], 'residual_found': False}
