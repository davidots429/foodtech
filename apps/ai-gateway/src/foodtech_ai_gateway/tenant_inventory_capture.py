"""Persist restricted AI inventory pages under a customer's deletion lock.

The injected progress store and cipher must run in an authenticated maintenance
boundary. A finished scan is not a snapshot, quiescence proof or deletion receipt.
"""
import hashlib
import json
from dataclasses import dataclass, field

from .legacy_tenant_inventory import reconcile_reserved_page


@dataclass(frozen=True)
class PartitionTarget:
    partition: str = field(repr=False)


class TenantInventoryCapture:
    def __init__(self, *, source_table, progress_store, cipher):
        self.source_table, self.progress_store, self.cipher = source_table, progress_store, cipher

    def _load(self, subject, request_id, domain):
        if not isinstance(getattr(self.source_table, 'table_arn', None), str) or \
                self.source_table.table_arn != getattr(self.cipher, 'source_table_arn', None) or \
                self.source_table.name in {self.progress_store.auth_table, self.progress_store.registry_table, self.progress_store.progress_table}:
            raise ValueError('AI source does not match inventory scope')
        return self.progress_store.load_checkpoint(cipher=self.cipher, subject=subject,
                                                   request_id=request_id, domain=domain)

    @staticmethod
    def _pack(value):
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        if len(raw) > 16384:
            raise ValueError('AI inventory page exceeds checkpoint capacity')
        return raw

    @staticmethod
    def _json(raw):
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, TypeError, UnicodeError):
            raise ValueError('invalid AI inventory') from None

    @staticmethod
    def _cursor(value):
        if value is not None and (not isinstance(value, dict) or set(value) != {'pk', 'sk'} or
                any(not isinstance(x, str) or not x or len(x) > 2048 for x in value.values())):
            raise ValueError('invalid AI inventory cursor')

    def _head(self, entry):
        if entry is None:
            return {'schema': 1, 'pages': 0, 'count': 0, 'cursor': None, 'complete': False}
        data = self._json(entry['checkpoint'])
        if set(data) != {'schema', 'pages', 'count', 'cursor', 'complete'} or data['schema'] != 1 or \
                type(data['pages']) is not int or not 1 <= data['pages'] < 10**12 or \
                type(data['count']) is not int or not 0 <= data['count'] <= data['pages'] * 20 or \
                type(data['complete']) is not bool or data['complete'] != (data['cursor'] is None) or \
                entry.get('version') != data['pages'] or \
                entry.get('state') != ('VERIFYING' if data['complete'] else 'IN_PROGRESS'):
            raise ValueError('invalid AI inventory head')
        self._cursor(data['cursor'])
        return data

    @staticmethod
    def _domain(number):
        if type(number) is not int or not 1 <= number < 10**12:
            raise ValueError('invalid AI inventory page')
        return f'AI_INVENTORY_PAGE_{number:012d}'

    def _page(self, entry):
        if not entry or entry.get('version') != 1 or entry.get('state') != 'IN_PROGRESS':
            raise ValueError('missing AI inventory page')
        value = self._json(entry['checkpoint'])
        if set(value) != {'schema', 'start', 'next', 'partitions'} or value['schema'] != 1 or \
                not isinstance(value['partitions'], list) or len(value['partitions']) > 20 or \
                any(not isinstance(x, str) or not x or len(x) > 2048 for x in value['partitions']) or \
                len(set(value['partitions'])) != len(value['partitions']):
            raise ValueError('invalid AI inventory page')
        self._cursor(value['start'])
        self._cursor(value['next'])
        if value['next'] is not None and value['next'] == value['start']:
            raise ValueError('nonadvancing AI inventory page')
        return value

    def capture_page(self, *, subject, request_id, limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid AI inventory limit')
        scope = {'subject': subject, 'request_id': request_id}
        head = self._head(self._load(subject, request_id, 'AI_INVENTORY'))
        if head['complete']:
            return {'complete': True, 'pages': head['pages'], 'count': head['count']}
        number = head['pages'] + 1
        domain = self._domain(number)
        entry = self._load(subject, request_id, domain)
        if entry is None:
            found = reconcile_reserved_page(self.source_table, self.progress_store.client,
                auth_table=self.progress_store.auth_table, registry_table=self.source_table.name,
                deletion_registry_table=self.progress_store.registry_table,
                tenant=subject, request_id=request_id, limit=min(limit, 20), checkpoint=head['cursor'])
            value = {'schema': 1, 'start': head['cursor'], 'next': found.checkpoint,
                     'partitions': list(found.partitions)}
            raw = self._pack(value)
            self._page({'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': raw})
            self.progress_store.save_checkpoint(cipher=self.cipher, **scope, domain=domain,
                expected_version=0, state='IN_PROGRESS', checkpoint=raw)
        else:
            value = self._page(entry)
        if value['start'] != head['cursor']:
            raise ValueError('invalid AI inventory page sequence')
        new_head = {'schema': 1, 'pages': number, 'count': head['count'] + len(value['partitions']),
                    'cursor': value['next'], 'complete': value['next'] is None}
        self.progress_store.save_checkpoint(cipher=self.cipher, **scope, domain='AI_INVENTORY',
            expected_version=head['pages'], state='VERIFYING' if new_head['complete'] else 'IN_PROGRESS',
            checkpoint=self._pack(new_head))
        return {'complete': new_head['complete'], 'pages': number, 'count': new_head['count']}

    def partitions(self, *, subject, request_id):
        """Yield internal targets only after validating every encrypted page."""
        head = self._head(self._load(subject, request_id, 'AI_INVENTORY'))
        if not head['complete']:
            raise ValueError('AI inventory scan is not complete')
        # Verify the entire page chain before exposing even the first target.
        cursor, count, visited, partitions, hashes = None, 0, set(), set(), []
        for number in range(1, head['pages'] + 1):
            entry = self._load(subject, request_id, self._domain(number))
            value = self._page(entry)
            hashes.append(hashlib.sha256(entry['checkpoint']).digest())
            if value['start'] != cursor or (number < head['pages'] and value['next'] is None) or \
                    (number == head['pages'] and value['next'] is not None):
                raise ValueError('invalid AI inventory page sequence')
            if partitions.intersection(value['partitions']):
                raise ValueError('duplicate AI inventory partition')
            partitions.update(value['partitions'])
            cursor = value['next']
            if cursor is not None:
                serialized = self._pack(cursor)
                if serialized in visited:
                    raise ValueError('repeated AI inventory cursor')
                visited.add(serialized)
            count += len(value['partitions'])
        if count != head['count']:
            raise ValueError('invalid AI inventory count')
        # Re-read because exposing earlier pages before validating later ones is unsafe.
        for number in range(1, head['pages'] + 1):
            entry = self._load(subject, request_id, self._domain(number))
            if not entry or hashlib.sha256(entry['checkpoint']).digest() != hashes[number - 1]:
                raise ValueError('AI inventory page changed')
            value = self._page(entry)
            for partition in value['partitions']:
                yield PartitionTarget(partition)
