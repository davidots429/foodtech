"""Exercise the AWS check's customer and concurrency scenario without AWS."""
import importlib.util
import unittest
from pathlib import Path
from uuid import uuid4

from foodtech_business_api.cooking_store import MemoryCookingStore

path = Path(__file__).resolve().parents[1] / 'live/combi_live_dynamodb_check.py'
spec = importlib.util.spec_from_file_location('combi_live_dynamodb_check', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class HarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_scenario(self):
        store = MemoryCookingStore()
        partitions = []
        result = await module.run_scenario(store, 'synthetic-' + uuid4().hex, partitions)
        self.assertEqual(result['competing_appends'], [200, 409])
        self.assertEqual(len(partitions), 3)
        self.assertTrue(all(any(pk == key[0] for key in store.items) for pk in partitions))
