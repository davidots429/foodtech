"""Capture subject-keyed condition profiles before locked deletion.

Requires an authenticated maintenance worker and stopped legacy writers. A
completed scan is not a consistent snapshot or proof of deletion completion.
"""
import hashlib
import json
from dataclasses import dataclass, field

from .cooking_store import _profile_listing
from .legacy_profile_scan import scan_legacy_profiles


@dataclass(frozen=True)
class ProfileTarget:
    profile_id: str = field(repr=False)
    version: str = field(repr=False)


class ProfileDeletionInventory:
    HEAD_DOMAIN = 'PROFILE_INVENTORY'

    def __init__(self, *, source_table, progress_store, cipher):
        self.source_table, self.progress_store, self.cipher = source_table, progress_store, cipher

    def _load(self, subject, request_id, domain):
        if not isinstance(getattr(self.source_table, 'table_arn', None), str) or \
                self.source_table.table_arn != getattr(self.cipher, 'source_table_arn', None) or \
                self.source_table.name in {self.progress_store.auth_table, self.progress_store.registry_table, self.progress_store.progress_table}:
            raise ValueError('profile source does not match inventory scope')
        return self.progress_store.load_checkpoint(cipher=self.cipher, subject=subject,
                                                   request_id=request_id, domain=domain)

    @staticmethod
    def _pack(value):
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        if len(raw) > 16384:
            raise ValueError('profile inventory page too large')
        return raw

    @staticmethod
    def _json(raw):
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (TypeError, ValueError, UnicodeError):
            raise ValueError('invalid profile inventory') from None

    @staticmethod
    def _cursor(cursor):
        if cursor is not None and (not isinstance(cursor, dict) or set(cursor) != {'pk', 'sk'} or
                any(not isinstance(value, str) or not value or len(value) > 2048 for value in cursor.values())):
            raise ValueError('invalid profile inventory cursor')

    @staticmethod
    def _domain(number):
        if type(number) is not int or not 1 <= number < 10**12:
            raise ValueError('invalid profile inventory page')
        return f'PROFILE_INVENTORY_PAGE_{number:012d}'

    def _head(self, item):
        if item is None:
            return {'schema': 1, 'pages': 0, 'count': 0, 'cursor': None, 'complete': False}
        data = self._json(item['checkpoint'])
        if set(data) != {'schema', 'pages', 'count', 'cursor', 'complete'} or data['schema'] != 1 or \
                type(data['pages']) is not int or not 1 <= data['pages'] < 10**12 or \
                type(data['count']) is not int or not 0 <= data['count'] <= data['pages'] * 20 or \
                type(data['complete']) is not bool or data['complete'] != (data['cursor'] is None) or \
                item.get('version') != data['pages'] or \
                item.get('state') != ('VERIFYING' if data['complete'] else 'IN_PROGRESS'):
            raise ValueError('invalid profile inventory head')
        self._cursor(data['cursor'])
        return data

    def _page(self, item, subject):
        if not item or item.get('version') != 1 or item.get('state') != 'IN_PROGRESS':
            raise ValueError('profile inventory page unavailable')
        data = self._json(item['checkpoint'])
        if set(data) != {'schema', 'start', 'next', 'references'} or data['schema'] != 1 or \
                not isinstance(data['references'], list) or len(data['references']) > 20:
            raise ValueError('invalid profile inventory page')
        self._cursor(data['start'])
        self._cursor(data['next'])
        if data['next'] is not None and data['next'] == data['start']:
            raise ValueError('nonadvancing profile inventory page')
        seen = set()
        for ref in data['references']:
            if not isinstance(ref, list) or len(ref) != 2:
                raise ValueError('invalid profile inventory reference')
            self._validate_reference(subject, ref)
            if tuple(ref) in seen:
                raise ValueError('duplicate profile inventory reference')
            seen.add(tuple(ref))
        return data

    @staticmethod
    def _validate_reference(subject, ref):
        _profile_listing(subject, {'profile_id': ref[0], 'profile_version': ref[1]})

    def _scan(self, *, subject, limit, checkpoint):
        return scan_legacy_profiles(self.source_table, subject=subject, limit=limit, checkpoint=checkpoint)

    @staticmethod
    def _target(ref):
        return ProfileTarget(*ref)

    def capture_page(self, *, subject, request_id, limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid profile inventory limit')
        scope = {'subject': subject, 'request_id': request_id}
        head = self._head(self._load(subject, request_id, self.HEAD_DOMAIN))
        if head['complete']:
            return {'complete': True, 'pages': head['pages'], 'count': head['count']}
        number, domain = head['pages'] + 1, self._domain(head['pages'] + 1)
        saved = self._load(subject, request_id, domain)
        if saved is None:
            found = self._scan(subject=subject, limit=min(limit, 20), checkpoint=head['cursor'])
            data = {'schema': 1, 'start': head['cursor'], 'next': found.checkpoint,
                    'references': [list(ref) for ref in found.references]}
            raw = self._pack(data)
            self._page({'version': 1, 'state': 'IN_PROGRESS', 'checkpoint': raw}, subject)
            self.progress_store.save_checkpoint(cipher=self.cipher, **scope, domain=domain,
                expected_version=0, state='IN_PROGRESS', checkpoint=raw)
        else:
            data = self._page(saved, subject)
        if data['start'] != head['cursor']:
            raise ValueError('invalid profile inventory page sequence')
        new_head = {'schema': 1, 'pages': number, 'count': head['count'] + len(data['references']),
                    'cursor': data['next'], 'complete': data['next'] is None}
        self.progress_store.save_checkpoint(cipher=self.cipher, **scope, domain=self.HEAD_DOMAIN,
            expected_version=head['pages'], state='VERIFYING' if new_head['complete'] else 'IN_PROGRESS',
            checkpoint=self._pack(new_head))
        return {'complete': new_head['complete'], 'pages': number, 'count': new_head['count']}

    def targets(self, *, subject, request_id):
        """Verify every encrypted page before yielding protected references."""
        head = self._head(self._load(subject, request_id, self.HEAD_DOMAIN))
        if not head['complete']:
            raise ValueError('profile inventory scan is not complete')
        cursor, count, seen_cursors, hashes = None, 0, set(), []
        for number in range(1, head['pages'] + 1):
            entry = self._load(subject, request_id, self._domain(number))
            page = self._page(entry, subject)
            hashes.append(hashlib.sha256(entry['checkpoint']).digest())
            if page['start'] != cursor or (number < head['pages'] and page['next'] is None) or \
                    (number == head['pages'] and page['next'] is not None):
                raise ValueError('invalid profile inventory page sequence')
            cursor = page['next']
            if cursor is not None:
                token = self._pack(cursor)
                if token in seen_cursors:
                    raise ValueError('repeated profile inventory cursor')
                seen_cursors.add(token)
            count += len(page['references'])
        if count != head['count']:
            raise ValueError('invalid profile inventory count')
        for number in range(1, head['pages'] + 1):
            entry = self._load(subject, request_id, self._domain(number))
            if not entry or hashlib.sha256(entry['checkpoint']).digest() != hashes[number - 1]:
                raise ValueError('profile inventory page changed')
            for ref in self._page(entry, subject)['references']:
                yield self._target(ref)
