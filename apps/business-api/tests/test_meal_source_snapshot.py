import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.meal_source_snapshot import MealSourceSnapshot


class MealSourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.store = SimpleNamespace(load_checkpoint=Mock())
        self.inventory = SimpleNamespace(progress_store=self.store, cipher=object(), _json=json.loads,
            targets=lambda **kw: iter([SimpleNamespace(meal_id='meal')]))
        self.inspector = MealSourceSnapshot(inventory=self.inventory)
        self.scope = {'subject': self.subject, 'request_id': self.request, 'index': 0}
        self.meta = {'pk': f'SCOPE#USER#{self.subject}#COMBI_MEAL#meal', 'sk': 'META',
                     'meal_batch_id': 'meal', 'version': 1, 'source': {'device_id': 'private'}}

    def save(self, meta):
        encoder = TypeSerializer()
        snap = {'schema': 1, 'meal_id': 'meal', 'version': 1,
                'metadata': {key: encoder.serialize(value) for key, value in meta.items()}}
        self.store.load_checkpoint.return_value = {'state': 'IN_PROGRESS', 'version': 1,
                                                   'checkpoint': json.dumps(snap).encode()}

    def test_source_requires_review_without_exposing_identifiers(self):
        self.save(self.meta)
        result = self.inspector.inspect(**self.scope)
        self.assertEqual(result, {'state': 'SOURCE_OWNERSHIP_REQUIRED'})
        self.assertNotIn('private', repr(result))
        self.save({**self.meta, 'source': None})
        self.assertEqual(self.inspector.inspect(**self.scope), {'state': 'NO_BOUND_SOURCE'})

    def test_missing_or_changed_scope_fails(self):
        self.store.load_checkpoint.return_value = None
        with self.assertRaises(ValueError): self.inspector.inspect(**self.scope)
        self.save({**self.meta, 'pk': f'SCOPE#USER#{uuid4()}#COMBI_MEAL#meal'})
        with self.assertRaises(ValueError): self.inspector.inspect(**self.scope)
        self.save({**self.meta, 'source': 'invalid'})
        with self.assertRaises(ValueError): self.inspector.inspect(**self.scope)


if __name__ == '__main__':
    unittest.main()
