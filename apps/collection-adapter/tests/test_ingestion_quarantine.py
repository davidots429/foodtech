import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from foodtech_collection_adapter import __main__ as cli
from foodtech_collection_adapter.ingestion_quarantine import IngestionQuarantine, failure
from foodtech_collection_adapter.rest import PollResult


class QuarantineTests(unittest.TestCase):
    def test_failure_boundary(self):
        self.assertEqual(failure('{"a":'), ("SCHEMA_VIOLATION", []))
        self.assertEqual(failure('{"a":1,"a":2}'), ("SCHEMA_VIOLATION", []))
        self.assertEqual(failure('[]'), ("SCHEMA_VIOLATION", []))
        self.assertEqual(failure('{"sourceId":"s"}')[0], "MANDATORY_FIELD_MISSING")
        fallback = {"sourceId": "s", "deviceId": "d", "batchId": "b", "schemaVersion": "1", "sequenceNumber": 1,
                    "eventTime": "2026-01-01T00:00:00Z"}
        self.assertIsNone(failure(json.dumps(fallback)))

    def test_object_before_manifest_and_retry(self):
        class Collision(Exception): pass
        class S3:
            def __init__(self): self.objects = {}
            def put_object(self, **request):
                self.objects[request["Key"]] = request["Body"]
                self.asserted = request["ServerSideEncryption"]
        class Table:
            def __init__(self):
                self.item = None
                self.meta = SimpleNamespace(client=SimpleNamespace(exceptions=SimpleNamespace(ConditionalCheckFailedException=Collision)))
            def put_item(self, Item, ConditionExpression):
                self.asserted = ConditionExpression
                if self.item: raise Collision()
                self.item = Item
            def get_item(self, **kwargs): return {"Item": self.item}
        sink = IngestionQuarantine.__new__(IngestionQuarantine)
        sink.s3, sink.table, sink.bucket = S3(), Table(), "private-bucket"
        kwargs = {"cursor_key": "default", "position": 3, "reason": "SCHEMA_VIOLATION", "missing": []}
        first = sink.preserve('{"a":', **kwargs)
        self.assertEqual(first, sink.preserve('{"a":', **kwargs))
        self.assertEqual(list(sink.s3.objects.values()), [b'{"a":'])
        self.assertEqual(sink.s3.asserted, "AES256")
        self.assertEqual(sink.table.item["ingestion_event_id"], first)
        self.assertNotIn("event_id", sink.table.item)
        sink.table.item["sha256"] = "altered"
        with self.assertRaisesRegex(ValueError, "conflicting"):
            sink.preserve('{"a":', **kwargs)

    def test_cli_commits_cursor_only_after_quarantine(self):
        calls = []
        class Client:
            def __init__(self, *args): pass
            def poll(self, cursor): return PollResult(('{"broken":',), cursor + 1)
            def close(self): pass
        class Ingestor:
            def __init__(self, *args): pass
            def cursor_state(self, key): return 0, set()
            def ingest(self, raw): raise AssertionError("invalid data entered normal pipeline")
            def save_cursor(self, *args): calls.append("cursor")
        class Sink:
            def __init__(self, *args): pass
            def preserve(self, raw, **kwargs): calls.append("preserve")
        config = {name: "test" for name in ("FOODTECH_MOCK_BASE_URL", "FOODTECH_MOCK_CLIENT_ID",
            "FOODTECH_MOCK_CLIENT_SECRET", "FOODTECH_MOCK_CA_FILE", "FOODTECH_DATA_TABLE",
            "FOODTECH_ORDERED_QUEUE_URL", "FOODTECH_INGESTION_DLQ_BUCKET")}
        with patch.dict("os.environ", config), patch.object(cli, "RestMockClient", Client), \
             patch.object(cli, "DynamoOrderedIngestor", Ingestor), patch.object(cli, "IngestionQuarantine", Sink), \
             patch("sys.argv", ["collector"]):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(calls, ["preserve", "cursor"])
        calls.clear()
        with patch.dict("os.environ", config), patch.object(cli, "RestMockClient", Client), \
             patch.object(cli, "DynamoOrderedIngestor", Ingestor), patch.object(cli, "IngestionQuarantine", Sink), \
             patch.object(Sink, "preserve", side_effect=RuntimeError("storage unavailable")), \
             patch("sys.argv", ["collector"]):
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                cli.main()
        self.assertEqual(calls, [])
