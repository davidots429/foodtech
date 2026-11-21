"""Resumable, encrypted observation of residual condition-profile source rows.

The worker must first stop legacy writers and empty the profile listing. This
scan checks source rows only: it is not a snapshot or a deletion receipt. The
caller must authenticate and coordinate the complete cross-store verification.
"""
import json

from .condition_profile_deletion import delete_profile_pair
from .legacy_profile_scan import scan_legacy_profiles
from .profile_deletion_inventory import ProfileDeletionInventory


class ProfileResidualScan:
    def __init__(self, *, source_table, progress_store, cipher):
        self.inventory = ProfileDeletionInventory(source_table=source_table, progress_store=progress_store, cipher=cipher)
        self.source_table, self.progress_store, self.cipher = source_table, progress_store, cipher

    def _checkpoint(self, subject, request_id):
        saved = self.inventory._load(subject, request_id, 'PROFILE_RESIDUAL')
        if saved is None:
            old = {'schema': 1, 'pages': 0, 'cursor': None, 'complete': False, 'residual': False}
            version = 0
        else:
            old = self.inventory._json(saved['checkpoint'])
            if set(old) != {'schema', 'pages', 'cursor', 'complete', 'residual'} or \
                    type(old['schema']) is not int or old['schema'] != 1 or \
                    type(old['pages']) is not int or not 1 <= old['pages'] < 10**12 or \
                    type(old['complete']) is not bool or type(old['residual']) is not bool or \
                    (old['complete'] and old['residual']) or \
                    (old['complete'] and old['cursor'] is not None) or \
                    (not old['complete'] and not old['residual'] and old['cursor'] is None) or \
                    saved['version'] != old['pages'] or \
                    saved['state'] != ('VERIFYING' if old['complete'] else 'IN_PROGRESS'):
                raise ValueError('invalid profile residual checkpoint')
            self.inventory._cursor(old['cursor'])
            version = saved['version']
        return old, version

    def observe_page(self, *, subject, request_id, limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid residual scan limit')
        scope = {'subject': subject, 'request_id': request_id, 'domain': 'PROFILE_RESIDUAL'}
        old, version = self._checkpoint(subject, request_id)
        if old['complete']:
            return {'scan_complete': True, 'residual_found': False}
        result = scan_legacy_profiles(self.source_table, subject=subject, limit=limit, checkpoint=old['cursor'])
        if result.checkpoint is not None and result.checkpoint == old['cursor']:
            raise ValueError('nonadvancing profile residual scan')
        residual = bool(result.references)
        data = {'schema': 1, 'pages': old['pages'] + 1, 'cursor': old['cursor'] if residual else result.checkpoint,
                'complete': not residual and result.checkpoint is None, 'residual': residual}
        raw = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        self.progress_store.save_checkpoint(cipher=self.cipher, **scope, expected_version=version,
            state='VERIFYING' if data['complete'] else 'IN_PROGRESS', checkpoint=raw)
        return {'scan_complete': data['complete'], 'residual_found': residual}

    def repair_one(self, *, subject, request_id, limit):
        """Explicitly remove one recorded residual under both transaction guards.

        The caller must investigate why residuals appeared and establish writer
        quiescence before invoking this restricted operation. No completion is
        recorded; the same position must subsequently be observed again.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid residual scan limit')
        old, _ = self._checkpoint(subject, request_id)
        if not old['residual'] or old['complete']:
            raise ValueError('no recorded profile residual')
        page = scan_legacy_profiles(self.source_table, subject=subject, limit=limit, checkpoint=old['cursor'])
        if page.checkpoint is not None and page.checkpoint == old['cursor']:
            raise ValueError('nonadvancing profile residual scan')
        if not page.references:
            return {'removed': False}
        profile_id, version = page.references[0]
        delete_profile_pair(self.source_table, self.progress_store.client,
                            auth_table=self.progress_store.auth_table,
                            registry_table=self.progress_store.registry_table,
                            subject=subject, request_id=request_id, profile_id=profile_id, version=version)
        return {'removed': True}
