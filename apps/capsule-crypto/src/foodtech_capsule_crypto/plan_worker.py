"""Persist an encrypted Capsule deletion plan within a locked recovery boundary.

An authenticated, separately authorized worker must call this component. It
neither deletes records nor proves that other stores or backups are clear.
"""
import hashlib
import json

from .deletion_plan import prepare_from_resolved_inventory


class CapsulePlanWorker:
    def __init__(self, *, recovery, inventory, source_table, progress_store, progress_cipher,
                 tenant_resolver, key_map, approved_purposes, approved_tenant_purposes, kms_client=None):
        if inventory.table_name != source_table.name or source_table.table_arn != progress_cipher.source_table_arn or \
                recovery.store.auth_table != progress_store.auth_table or \
                recovery.store.registry_table != progress_store.registry_table or \
                recovery.store.capsule_table != source_table.name or \
                source_table.name in {progress_store.auth_table, progress_store.registry_table, progress_store.progress_table}:
            raise ValueError('capsule plan storage scope mismatch')
        self.recovery, self.inventory, self.source_table = recovery, inventory, source_table
        self.progress_store, self.progress_cipher, self.tenant_resolver = progress_store, progress_cipher, tenant_resolver
        self.key_map, self.approved_purposes = key_map, approved_purposes
        self.approved_tenant_purposes, self.kms = approved_tenant_purposes, kms_client

    @staticmethod
    def _encode(value):
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        if len(raw) > 16384:
            raise ValueError('capsule plan page too large')
        return raw

    def prepare(self, *, authenticated_subject, request_id):
        if self.inventory.table_name != self.source_table.name or \
                self.source_table.table_arn != self.progress_cipher.source_table_arn or \
                self.recovery.store.auth_table != self.progress_store.auth_table or \
                self.recovery.store.registry_table != self.progress_store.registry_table or \
                self.recovery.store.capsule_table != self.source_table.name:
            raise ValueError('capsule plan storage scope mismatch')
        summary = {}
        scope = {'subject': authenticated_subject, 'request_id': request_id}
        def save_checked(domain, raw, state, *, allow_create):
            existing = self.progress_store.load_checkpoint(cipher=self.progress_cipher, **scope, domain=domain)
            if existing is None:
                if not allow_create:
                    raise ValueError('verified capsule plan page is missing')
                self.progress_store.save_checkpoint(cipher=self.progress_cipher, **scope, domain=domain,
                    expected_version=0, state=state, checkpoint=raw)
            elif existing.get('version') != 1 or existing.get('state') != state or existing.get('checkpoint') != raw:
                raise ValueError('capsule plan changed during retry')

        def build(internal_id, locator):
            plan = prepare_from_resolved_inventory(inventory=self.inventory, locator=locator.decode('ascii'),
                request_id=request_id, internal_id=internal_id, tenant_resolver=self.tenant_resolver,
                key_map=self.key_map, approved_purposes=self.approved_purposes,
                approved_tenant_purposes=self.approved_tenant_purposes, kms_client=self.kms)
            count = len(plan.entries)
            pages = (count + 19) // 20
            if pages >= 10**12:
                raise ValueError('too many capsule plan pages')
            def page_payload(number):
                start = (number - 1) * 20
                return self._encode({'schema': 1, 'page': number, 'entries': [
                    {'purpose': entry.purpose, 'epoch': entry.epoch, 'pseudonym_digest': entry.pseudonym_digest}
                    for entry in plan.entries[start:start + 20]]})
            digest = hashlib.sha256()
            for number in range(1, pages + 1):
                payload = page_payload(number)
                digest.update(len(payload).to_bytes(4, 'big'))
                digest.update(payload)
            head = self._encode({'schema': 1, 'count': count, 'pages': pages, 'sha256': digest.hexdigest()})
            existing = self.progress_store.load_checkpoint(cipher=self.progress_cipher, **scope, domain='CAPSULE_PLAN')
            if existing is None:
                self.progress_store.save_checkpoint(cipher=self.progress_cipher, **scope, domain='CAPSULE_PLAN',
                    expected_version=0, state='IN_PROGRESS', checkpoint=head)
                phase, version = 'IN_PROGRESS', 1
            elif existing.get('checkpoint') == head and (existing.get('version'), existing.get('state')) in {
                    (1, 'IN_PROGRESS'), (2, 'VERIFYING')}:
                phase, version = existing['state'], existing['version']
            else:
                raise ValueError('capsule plan changed during retry')
            for number in range(1, pages + 1):
                save_checked(f'CAPSULE_PLAN_PAGE_{number:012d}', page_payload(number), 'IN_PROGRESS',
                             allow_create=phase == 'IN_PROGRESS')
            if phase == 'IN_PROGRESS':
                self.progress_store.save_checkpoint(cipher=self.progress_cipher, **scope, domain='CAPSULE_PLAN',
                    expected_version=version, state='VERIFYING', checkpoint=head)
            summary.update({'state': 'PREPARED', 'target_count': count, 'pages': pages})
        self.recovery.use_locked(authenticated_subject=authenticated_subject, request_id=request_id, action=build)
        if not summary:
            raise ValueError('capsule plan was not prepared')
        return summary
