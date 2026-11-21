import unittest
from types import SimpleNamespace
from unittest.mock import patch
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore, MemoryCookingStore, _history_number


def meta(job_id="one"):
    return {"owner_subject": "owner", "job_id": job_id, "created_at": "2026-01-01T00:00:00Z",
            "version": 1, "recipe_version": "synthetic", "planned_portions": 1}


class HistoryRevisionTests(unittest.TestCase):
    def test_memory_increments_only_for_relevant_successful_mutations(self):
        store = MemoryCookingStore(); pk = "SCOPE#USER#owner#JOB#one"
        self.assertIsNone(store.history_revision("owner"))
        store.create(pk, meta()); self.assertEqual(store.history_revision("owner"), 1)
        with self.assertRaises(Conflict): store.create(pk, meta())
        store.ensure_listed(pk, meta()); self.assertEqual(store.history_revision("owner"), 1)
        store.bind_stream(pk, 1, {**meta(), "version": 2}, "STREAM#one")
        self.assertEqual(store.history_revision("owner"), 2)
        store.commit(pk, 2, {**meta(), "version": 3}, event={"sk": "EVENT#start", "trigger": "COOKING_STARTED"})
        self.assertEqual(store.history_revision("owner"), 3)
        with self.assertRaises(Conflict):
            store.commit(pk, 2, {**meta(), "version": 3}, event={"sk": "EVENT#start", "trigger": "COOKING_STARTED"})
        store.commit(pk, 3, {**meta(), "version": 4}, event={"sk": "EVENT#other", "trigger": "SAFETY"})
        self.assertEqual(store.history_revision("owner"), 3)
        store.create_recook(pk, 4, "SCOPE#USER#owner#JOB#two", meta("two"))
        self.assertEqual(store.history_revision("owner"), 4)
        key = ("SCOPE#USER#owner#JOBS", "CREATED#2026-01-01T00:00:00Z#two")
        del store.items[key]
        store.ensure_listed("SCOPE#USER#owner#JOB#two", meta("two"))
        self.assertEqual(store.history_revision("owner"), 5)
        store.ensure_listed("SCOPE#USER#owner#JOB#two", meta("two"))
        self.assertEqual(store.history_revision("owner"), 5)

    def test_start_and_history_revision_share_dynamo_transaction(self):
        calls = []; store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test"); store.serializer = TypeSerializer()
        store.client = SimpleNamespace(transact_write_items=lambda **kwargs: calls.append(kwargs))
        store.commit("SCOPE#USER#owner#JOB#one", 1, {"version": 2},
                     event={"sk": "EVENT#start", "trigger": "COOKING_STARTED"})
        ops = calls[0]["TransactItems"]
        self.assertEqual(len(ops), 3)
        self.assertEqual(ops[-1]["Update"]["Key"], {"pk": {"S": "SCOPE#USER#owner#JOBS"},
                                                     "sk": {"S": "HISTORY#REVISION"}})
        self.assertIn("attribute_not_exists", ops[-1]["Update"]["ConditionExpression"])

    def test_dynamo_retries_only_explicit_transaction_conflicts(self):
        class Cancelled(Exception):
            def __init__(self, code): self.response = {"CancellationReasons": [{"Code": code}]}
        class Client:
            exceptions = SimpleNamespace(TransactionCanceledException=Cancelled)
            def __init__(self): self.calls = 0; self.reason = "TransactionConflict"
            def transact_write_items(self, **_kwargs):
                self.calls += 1
                if self.calls < 3: raise Cancelled(self.reason)
        store = DynamoCookingStore.__new__(DynamoCookingStore); store.client = Client()
        with patch("foodtech_business_api.cooking_store.time.sleep") as delay:
            store._history_transact([])
        self.assertEqual((store.client.calls, delay.call_count), (3, 2))
        store.client.calls = 0; store.client.reason = "ConditionalCheckFailed"
        with patch("foodtech_business_api.cooking_store.time.sleep") as delay, self.assertRaises(Cancelled):
            store._history_transact([])
        self.assertEqual((store.client.calls, delay.call_count), (1, 0))

    def test_invalid_history_values_fail_closed(self):
        for value in (True, 0, -1, "1", 10**18 + 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _history_number({"revision": value})
