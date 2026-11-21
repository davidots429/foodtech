"""Inspect protected meal metadata without asserting source ownership."""
from itertools import islice
from boto3.dynamodb.types import TypeDeserializer

from .job_source_snapshot import JobSourceSnapshot


class MealSourceSnapshot:
    def __init__(self, *, inventory):
        self.inventory = inventory
        self.store, self.cipher = inventory.progress_store, inventory.cipher

    def inspect(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid meal snapshot index')
        target = next((item for n, item in enumerate(self.inventory.targets(
            subject=subject, request_id=request_id)) if n == index), None)
        if target is None:
            raise ValueError('meal snapshot target unavailable')
        return self._inspect_target(subject=subject, request_id=request_id, index=index, target=target)

    def inspect_batch(self, *, subject, request_id, start, count):
        if type(start) is not int or not 0 <= start < 10**12 or type(count) is not int or \
                not 1 <= count <= 20 or start + count >= 10**12:
            raise ValueError('invalid meal snapshot range')
        targets = list(islice(self.inventory.targets(subject=subject, request_id=request_id), start, start + count))
        if len(targets) != count:
            raise ValueError('meal snapshot target range unavailable')
        for index, target in enumerate(targets, start):
            result = self._inspect_target(subject=subject, request_id=request_id, index=index, target=target)
            if result == {'state': 'SOURCE_OWNERSHIP_REQUIRED'}:
                return {'state': 'SOURCE_OWNERSHIP_REQUIRED'}
            if result != {'state': 'NO_BOUND_SOURCE'}:
                raise ValueError('invalid meal source inspection result')
        return {'state': 'NO_BOUND_SOURCE', 'count': count}

    def _inspect_target(self, *, subject, request_id, index, target):
        saved = self.store.load_checkpoint(cipher=self.cipher, subject=subject, request_id=request_id,
                                           domain=f'MEAL_META_{index:012d}')
        try:
            if not saved or saved['version'] != 1 or saved['state'] != 'IN_PROGRESS':
                raise ValueError
            snap = self.inventory._json(saved['checkpoint'])
            if set(snap) != {'schema', 'meal_id', 'version', 'metadata'} or type(snap['schema']) is not int or \
                    snap['schema'] != 1 or snap['meal_id'] != target.meal_id or type(snap['version']) is not int or \
                    not 1 <= snap['version'] < 10**18 or not isinstance(snap['metadata'], dict):
                raise ValueError
            decoder = TypeDeserializer()
            meta = {key: decoder.deserialize(JobSourceSnapshot._restore(value)) for key, value in snap['metadata'].items()}
            if meta.get('pk') != f'SCOPE#USER#{subject}#COMBI_MEAL#{target.meal_id}' or meta.get('sk') != 'META' or \
                    meta.get('meal_batch_id') != target.meal_id or meta.get('version') != snap['version']:
                raise ValueError
            source = meta.get('source')
            if source is not None and not isinstance(source, dict):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('meal snapshot unavailable') from None
        return {'state': 'SOURCE_OWNERSHIP_REQUIRED' if source is not None else 'NO_BOUND_SOURCE'}
