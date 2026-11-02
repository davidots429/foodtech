import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import jsonschema

from foodtech_collection_adapter.resequence import Received, Resequencer
from foodtech_collection_adapter.rest import RestMockClient
from foodtech_collection_adapter.aws_flow import DynamoOrderedIngestor
from foodtech_collection_adapter import __main__ as collector_cli
from foodtech_collection_adapter.rest import PollResult
from foodtech_contracts import canonical_record, decode_record, payload_hash, stream_key
from foodtech_standardization_worker import assess

FIXTURE = json.loads((Path(__file__).resolve().parents[3] / "fixtures/mockprofile-100-a-001.json").read_text())


class MinimalFlowTests(unittest.TestCase):
    def test_cursor_is_scoped_to_fixture_and_remembers_streams(self):
        class Table:
            def __init__(self):
                self.items = {}

            def get_item(self, *, Key, ConsistentRead):
                assert ConsistentRead
                return {"Item": self.items[Key["pk"]]} if Key["pk"] in self.items else {}

            def update_item(self, *, Key, ExpressionAttributeValues, **_kwargs):
                self.items[Key["pk"]] = {"cursor": ExpressionAttributeValues[":cursor"],
                                         "streams": ExpressionAttributeValues[":streams"]}

        ingestor = DynamoOrderedIngestor.__new__(DynamoOrderedIngestor)
        ingestor.table = Table()
        ingestor.save_cursor(5, {"stream-b", "stream-a"}, "high-fixture")
        self.assertEqual(ingestor.cursor_state("high-fixture"), (5, {"stream-a", "stream-b"}))
        self.assertEqual(ingestor.cursor_state("default"), (0, set()))

    def test_cli_drains_stream_from_received_batch(self):
        record = {**FIXTURE["records"][0], "batchId": "batch-a-other-001"}
        raw = json.dumps(record)
        calls = {"drains": []}

        class Client:
            def __init__(self, *_args): pass
            def poll(self, cursor):
                self_cursor = cursor
                assert self_cursor == 0
                return PollResult((raw,), 1)
            def close(self): pass

        class Ingestor:
            def __init__(self, *_args): pass
            def cursor_state(self, key):
                assert key == "other-fixture"
                return 0, set()
            def ingest(self, _raw): return []
            def drain(self, stream):
                calls["drains"].append(stream)
                return []
            def save_cursor(self, cursor, streams, key):
                calls["saved"] = (cursor, streams, key)

        config = {name: "test" for name in (
            "FOODTECH_MOCK_BASE_URL", "FOODTECH_MOCK_CLIENT_ID", "FOODTECH_MOCK_CLIENT_SECRET",
            "FOODTECH_MOCK_CA_FILE", "FOODTECH_DATA_TABLE", "FOODTECH_ORDERED_QUEUE_URL")}
        with patch.dict("os.environ", config), \
             patch.object(collector_cli, "RestMockClient", Client), \
             patch.object(collector_cli, "DynamoOrderedIngestor", Ingestor), \
             patch("sys.argv", ["collector", "--cursor-key", "other-fixture"]):
            self.assertEqual(collector_cli.main(), 0)
        expected = stream_key(record)
        self.assertEqual(calls["drains"], [expected])
        self.assertEqual(calls["saved"], (1, {expected}, "other-fixture"))

    def records(self):
        return [json.dumps(item, separators=(",", ":")) for item in FIXTURE["records"]]

    def test_schema_hash_and_quality(self):
        records = self.records()
        self.assertEqual([assess(str(index), raw).quality_status for index, raw in enumerate(records)],
                         ["VALID", "QUARANTINED", "VALID"])
        valid = decode_record(records[0])
        self.assertEqual(canonical_record(valid)["temperature_celsius"], "180")
        reordered = dict(reversed(list(valid.items())))
        self.assertEqual(payload_hash(valid), payload_hash(reordered))
        with self.assertRaises(ValueError):
            decode_record('{"schemaVersion":"1.0","schemaVersion":"1.0"}')
        with self.assertRaises(jsonschema.ValidationError):
            decode_record(records[0].replace('"recordId":"rec-a-001",', ""))

    def test_resequence_out_of_order_and_gap(self):
        raws = self.records()
        stream = Resequencer()
        def event(index, now):
            item = FIXTURE["records"][index]
            return Received(str(index), raws[index], item["sourceId"], item["deviceId"],
                            item["batchId"], item["sequenceNumber"], now)
        self.assertEqual([item.sequence_number for item in stream.accept(event(0, 0), 0)], [1])
        self.assertEqual(stream.accept(event(1, 1), 1), [])
        self.assertEqual([item.sequence_number for item in stream.accept(event(2, 2), 2)], [2, 3])
        gap = Resequencer()
        gap.accept(event(0, 0), 0)
        gap.accept(event(1, 1), 1)
        self.assertEqual(gap.tick(5), [])
        late_release = gap.tick(6)
        self.assertEqual([(item.sequence_number, item.gap_before) for item in late_release], [(3, (2, 2))])
        self.assertEqual(gap.accept(event(2, 7), 7)[0].sequence_status, "LATE_AFTER_GAP")
        self.assertEqual(assess("3", raws[1], gap_before=(2, 2)).quality_status, "QUARANTINED")

    def test_https_poll_retries_real_transport_disconnect(self):
        records = self.records()
        calls = {"records": 0}
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(200, json={"access_token": "test-token", "token_type": "Bearer"})
            calls["records"] += 1
            if calls["records"] == 1:
                raise httpx.RemoteProtocolError("connection terminated")
            return httpx.Response(200, text="\n".join(records) + "\n", headers={
                "X-Contract-Version": "1.0", "X-Profile-ID": "mockprofile-100",
                "X-Provenance": "SYNTHETIC", "X-Next-Cursor": "3",
            })
        client = httpx.Client(base_url="https://localhost", transport=httpx.MockTransport(handler))
        poller = RestMockClient("https://localhost", "id", "secret", "test-ca", client)
        result = poller.poll(0)
        self.assertEqual(result.records, tuple(records))
        self.assertEqual(result.next_cursor, 3)
        self.assertEqual(calls["records"], 2)
        poller.close()


if __name__ == "__main__":
    unittest.main()
