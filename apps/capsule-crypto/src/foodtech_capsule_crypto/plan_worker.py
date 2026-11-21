"""Persist an encrypted Capsule deletion plan within a locked recovery boundary.

An authenticated, separately authorized worker must call this component. It
neither deletes records nor proves that other stores or backups are clear.
"""
import hashlib
import json
import re

from .contract import Entry

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

    def use_prepared(self, *, authenticated_subject, request_id, action):
        """Visit validated targets inside locked recovery; action must be idempotent.

        All pages are verified before the first action. Every action must check
        the deletion lock atomically with its own mutation. This does not mark
        any target or customer deletion complete.
        """
        if not callable(action) or self.inventory.table_name != self.source_table.name or \
                self.source_table.table_arn != self.progress_cipher.source_table_arn or \
                self.recovery.store.auth_table != self.progress_store.auth_table or \
                self.recovery.store.registry_table != self.progress_store.registry_table or \
                self.recovery.store.capsule_table != self.source_table.name:
            raise ValueError('invalid capsule plan dispatch scope')
        scope = {'subject': authenticated_subject, 'request_id': request_id}
        summary = {}
        def load(domain, version, state):
            item = self.progress_store.load_checkpoint(cipher=self.progress_cipher, **scope, domain=domain)
            if not item or item.get('version') != version or item.get('state') != state or \
                    not isinstance(item.get('checkpoint'), bytes):
                raise ValueError('capsule plan record unavailable')
            return item['checkpoint']
        def parse(raw):
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError
                return value
            except (TypeError, ValueError, UnicodeError):
                raise ValueError('invalid capsule plan record') from None
        def visit(_internal_id, _locator):
            head = parse(load('CAPSULE_PLAN', 2, 'VERIFYING'))
            if set(head) != {'schema', 'count', 'pages', 'sha256'} or type(head['schema']) is not int or head['schema'] != 1 or \
                    type(head['count']) is not int or head['count'] < 0 or \
                    type(head['pages']) is not int or not 0 <= head['pages'] < 10**12 or \
                    head['pages'] != (head['count'] + 19) // 20 or \
                    not isinstance(head['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', head['sha256']):
                raise ValueError('invalid capsule plan head')
            digest, hashes, previous, count = hashlib.sha256(), [], None, 0
            for number in range(1, head['pages'] + 1):
                raw = load(f'CAPSULE_PLAN_PAGE_{number:012d}', 1, 'IN_PROGRESS')
                page = parse(raw)
                if set(page) != {'schema', 'page', 'entries'} or type(page['schema']) is not int or page['schema'] != 1 or \
                        type(page['page']) is not int or page['page'] != number or \
                        not isinstance(page['entries'], list) or not 1 <= len(page['entries']) <= 20 or \
                        (number < head['pages'] and len(page['entries']) != 20):
                    raise ValueError('invalid capsule plan page')
                for value in page['entries']:
                    if not isinstance(value, dict) or set(value) != {'purpose', 'epoch', 'pseudonym_digest'}:
                        raise ValueError('invalid capsule plan target')
                    entry = Entry(value['purpose'], value['epoch'], value['pseudonym_digest'])
                    entry.validate(self.approved_purposes)
                    current = (entry.purpose, entry.epoch)
                    if previous is not None and current <= previous:
                        raise ValueError('invalid capsule plan target order')
                    previous = current
                    count += 1
                digest.update(len(raw).to_bytes(4, 'big'))
                digest.update(raw)
                hashes.append(hashlib.sha256(raw).digest())
            if count != head['count'] or digest.hexdigest() != head['sha256']:
                raise ValueError('capsule plan integrity mismatch')
            for number in range(1, head['pages'] + 1):
                raw = load(f'CAPSULE_PLAN_PAGE_{number:012d}', 1, 'IN_PROGRESS')
                if hashlib.sha256(raw).digest() != hashes[number - 1]:
                    raise ValueError('capsule plan page changed')
                for value in parse(raw)['entries']:
                    action(Entry(value['purpose'], value['epoch'], value['pseudonym_digest']))
            summary.update({'state': 'VISITED', 'target_count': count})
        self.recovery.use_locked(authenticated_subject=authenticated_subject, request_id=request_id, action=visit)
        if not summary:
            raise ValueError('capsule plan was not visited')
        return summary

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
