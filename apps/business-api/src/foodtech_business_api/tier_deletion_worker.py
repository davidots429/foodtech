"""Run one bounded, restartable tier-deletion page inside a trusted worker.

The caller must authenticate and authorize the deletion job. Progress storage
and the tier store must enforce the same request lock. This worker does not
verify indexes, backups, other domains, or mark the deletion complete.
"""


import json


class TierIndexCapture:
    """Store bounded encrypted pages before any source tier row is deleted."""
    def __init__(self, *, tier_store, progress_store, cipher):
        self.tier_store, self.progress_store, self.cipher = tier_store, progress_store, cipher

    @staticmethod
    def _json(raw):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, TypeError, UnicodeError):
            raise ValueError('invalid tier index capture') from None

    @staticmethod
    def _pack(data):
        raw = json.dumps(data, ensure_ascii=True, separators=(',', ':'), sort_keys=True).encode('ascii')
        if len(raw) > 16384:
            raise ValueError('tier index page exceeds checkpoint capacity')
        return raw

    def _load(self, subject, request_id, domain):
        source = getattr(self.cipher, 'source_table_arn', None)
        actual = getattr(getattr(self.tier_store, 'table', None), 'table_arn', None)
        if not isinstance(source, str) or not isinstance(actual, str) or source != actual:
            raise ValueError('tier source table does not match checkpoint scope')
        return self.progress_store.load_checkpoint(cipher=self.cipher, subject=subject,
                                                   request_id=request_id, domain=domain)

    def _head(self, entry):
        if entry is None:
            return {'schema': 2, 'cursor': None, 'pages': 0, 'count': 0, 'complete': False}
        data = self._json(entry['checkpoint'])
        if set(data) != {'schema', 'cursor', 'pages', 'count', 'complete'} or data['schema'] != 2 or \
                type(data['pages']) is not int or not 1 <= data['pages'] < 10**12 or \
                type(data['count']) is not int or not 0 <= data['count'] <= data['pages'] * 20 or \
                type(data['complete']) is not bool or data['complete'] != (data['cursor'] is None) or \
                (data['cursor'] is not None and (not isinstance(data['cursor'], str) or
                 not data['cursor'].startswith('APPLICATION#') or len(data['cursor']) > 2048)) or \
                entry['state'] != ('VERIFYING' if data['complete'] else 'IN_PROGRESS') or \
                entry['version'] != data['pages']:
            raise ValueError('invalid tier index capture')
        return data

    @staticmethod
    def _page_domain(number):
        if type(number) is not int or not 1 <= number < 10**12:
            raise ValueError('invalid tier index page')
        return f'TIER_INDEX_PAGE_{number:012d}'

    def _page(self, entry):
        if not entry or entry.get('state') != 'IN_PROGRESS' or entry.get('version') != 1:
            raise ValueError('missing tier index page')
        data = self._json(entry['checkpoint'])
        if set(data) != {'schema', 'start', 'next', 'keys'} or data['schema'] != 2 or \
                not isinstance(data['keys'], list) or len(data['keys']) > 20 or \
                any(not isinstance(k, str) or not k or len(k) > 257 for k in data['keys']) or \
                len(set(data['keys'])) != len(data['keys']) or any(
                    cursor is not None and (not isinstance(cursor, str) or not cursor.startswith('APPLICATION#')
                                            or len(cursor) > 2048) for cursor in (data['start'], data['next'])):
            raise ValueError('invalid tier index page')
        return data

    def capture_page(self, *, subject, request_id, limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid tier capture limit')
        scope = {'subject': subject, 'request_id': request_id}
        previous = self._load(subject, request_id, 'TIER_INDEX')
        head = self._head(previous)
        if head['complete']:
            return {'complete': True, 'version': head['pages'], 'captured_count': head['count']}
        number = head['pages'] + 1
        domain = self._page_domain(number)
        existing = self._load(subject, request_id, domain)
        if existing is None:
            page = self.tier_store.deletion_index_candidates(subject, request_id, min(limit, 20), head['cursor'])
            data = {'schema': 2, 'start': head['cursor'], 'next': page['next_cursor'], 'keys': page['queue_sorts']}
            raw = self._pack(data)
            self._page({'state': 'IN_PROGRESS', 'version': 1, 'checkpoint': raw})
            self.progress_store.save_checkpoint(cipher=self.cipher, **scope, domain=domain, expected_version=0,
                                                state='IN_PROGRESS', checkpoint=raw)
        else:
            data = self._page(existing)
        if data['start'] != head['cursor'] or (data['next'] is not None and data['start'] is not None and data['next'] <= data['start']):
            raise ValueError('invalid tier index page sequence')
        new_head = {'schema': 2, 'cursor': data['next'], 'pages': number,
                    'count': head['count'] + len(data['keys']), 'complete': data['next'] is None}
        saved = self.progress_store.save_checkpoint(cipher=self.cipher, **scope, domain='TIER_INDEX',
            expected_version=head['pages'], state='VERIFYING' if new_head['complete'] else 'IN_PROGRESS',
            checkpoint=self._pack(new_head))
        return {'complete': new_head['complete'], 'version': saved, 'captured_count': new_head['count']}

    def _verified_pages(self, subject, request_id):
        entry = self._load(subject, request_id, 'TIER_INDEX')
        head = self._head(entry)
        if not head['complete']:
            raise ValueError('tier index capture is not complete')
        cursor, count, pages = None, 0, []
        for number in range(1, head['pages'] + 1):
            page = self._page(self._load(subject, request_id, self._page_domain(number)))
            if page['start'] != cursor or (number < head['pages'] and page['next'] is None) or \
                    (number == head['pages'] and page['next'] is not None) or (page['next'] is not None and page['start'] is not None and page['next'] <= page['start']):
                raise ValueError('invalid tier index page sequence')
            cursor = page['next']
            count += len(page['keys'])
            pages.append(page['keys'])
        if count != head['count']:
            raise ValueError('invalid tier index page count')
        return pages

    def observe_one(self, *, subject, request_id, index):
        if type(index) is not int or index < 0:
            raise ValueError('invalid tier index observation')
        pages = self._verified_pages(subject, request_id)
        for keys in pages:
            if index < len(keys):
                result = self.tier_store.observe_pending_index(subject, request_id, keys[index])
                if result not in {'PRESENT', 'NOT_OBSERVED', 'INCONCLUSIVE'}:
                    raise ValueError('invalid tier index observation result')
                return {'observation': result}
            index -= len(keys)
        raise ValueError('invalid tier index observation')

    def observe_and_record(self, *, subject, request_id, index):
        """Persist one bounded observation without declaring deletion complete."""
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid tier index observation')
        scope = {'subject': subject, 'request_id': request_id, 'domain': f'TIER_OBSERVE_{index:012d}'}
        previous = self._load(subject, request_id, scope['domain'])
        version = 0 if previous is None else previous['version']
        if previous is not None:
            old = self._json(previous['checkpoint'])
            if previous.get('state') != 'VERIFYING' or set(old) != {'schema', 'index', 'observation', 'observed_at'} or \
                    old.get('schema') != 1 or old.get('index') != index or \
                    old.get('observation') not in {'PRESENT', 'NOT_OBSERVED', 'INCONCLUSIVE'} or \
                    not isinstance(old.get('observed_at'), str):
                raise ValueError('invalid tier observation record')
        if type(version) is not int or not 0 <= version < 2**63 - 1:
            raise ValueError('invalid tier observation version')
        observed = self.observe_one(subject=subject, request_id=request_id, index=index)
        from datetime import datetime, timezone
        record = {'schema': 1, 'index': index, 'observation': observed['observation'],
                  'observed_at': datetime.now(timezone.utc).isoformat()}
        saved = self.progress_store.save_checkpoint(cipher=self.cipher, **scope, expected_version=version,
                                                    state='VERIFYING', checkpoint=self._pack(record))
        return {'index': index, 'observation': observed['observation'], 'version': saved}

    def ensure_ready(self, *, subject, request_id):
        self._verified_pages(subject, request_id)


class TierDeletionWorker:
    def __init__(self, *, tier_store, progress_store, cipher, index_capture):
        if index_capture.tier_store is not tier_store or index_capture.progress_store is not progress_store or \
                index_capture.cipher is not cipher:
            raise ValueError('tier deletion dependencies do not match capture')
        self.index_capture = index_capture
        self.tier_store = tier_store
        self.progress_store = progress_store
        self.cipher = cipher

    def run_page(self, *, subject: str, request_id: str, limit: int) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid tier deletion page limit')
        self.index_capture.ensure_ready(subject=subject, request_id=request_id)
        scope = {'subject': subject, 'request_id': request_id, 'domain': 'TIER'}
        previous = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        if previous is None:
            version = 0
        else:
            if not isinstance(previous, dict) or previous.get('checkpoint') != b'tier-first-page-v1' or \
                    type(previous.get('version')) is not int or not 1 <= previous['version'] < 2**63 - 1 or \
                    previous.get('state') not in {'IN_PROGRESS', 'VERIFYING', 'FAILED'}:
                raise ValueError('invalid tier deletion progress')
            version = previous['version']
        # Always restart at the first remaining key. Every deletion rechecks
        # the lock; if saving progress fails, the page can safely be retried.
        count = self.tier_store.delete_inventory_page(subject, request_id, limit)
        if type(count) is not int or not 0 <= count <= limit:
            raise ValueError('invalid tier deletion page result')
        state = 'VERIFYING' if count == 0 else 'IN_PROGRESS'
        version = self.progress_store.save_checkpoint(cipher=self.cipher, **scope,
            expected_version=version, state=state, checkpoint=b'tier-first-page-v1')
        return {'deleted_count': count, 'version': version, 'state': state}
