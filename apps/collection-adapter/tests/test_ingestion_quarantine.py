import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foodtech_collection_adapter import __main__ as cli
from foodtech_collection_adapter.ingestion_quarantine import IngestionQuarantine, failure
from foodtech_collection_adapter.rest import PollResult


class QuarantineTests(unittest.TestCase):
    def test_mqtt_json_schema_violations_are_preserved_before_ack(self):
        from foodtech_collection_adapter.mqtt import accept_message, topic_for
        root = Path(__file__).resolve().parents[3]
        calls = []
        class Client:
            def ack(self, mid, qos): calls.append("ack"); return 0
        class Sink:
            def preserve_mqtt(self, payload, **kwargs):
                calls.append((payload, kwargs["reason"], kwargs["profile_id"]))
        class Ingestor:
            def ingest(self, raw): raise AssertionError("invalid JSON entered normal pipeline")
        for vendor in "abc":
            instance = f"mockprofile-300-{vendor}-001"
            record = json.loads((root / f"fixtures/{instance}.json").read_text())["records"][0]
            if vendor == "a": record["measurements"]["temperatureCelsius"] = "invalid"
            elif vendor == "b": record["temperature_f"] = "invalid"
            else: record["Readings"]["TempC"] = "invalid"
            payload = json.dumps(record).encode()
            message = SimpleNamespace(topic=topic_for(instance), qos=1, retain=False, payload=payload, mid=7)
            with self.subTest(vendor=vendor):
                self.assertIsNone(accept_message(Client(), message, Ingestor(), Sink(), "collector", instance))
                self.assertEqual(calls, [(payload, "SCHEMA_VIOLATION", "mockprofile-300"), "ack"])
                calls.clear()
                with patch.object(Sink, "preserve_mqtt", side_effect=RuntimeError("storage failed")):
                    with self.assertRaisesRegex(RuntimeError, "storage failed"):
                        accept_message(Client(), message, Ingestor(), Sink(), "collector", instance)
                self.assertEqual(calls, [])

    def test_mqtt_valid_json_enters_pipeline_but_wrong_identity_is_not_acknowledged(self):
        from foodtech_collection_adapter.mqtt import accept_message, topic_for
        root = Path(__file__).resolve().parents[3]
        calls = []
        class Client:
            def ack(self, mid, qos): calls.append("ack"); return 0
        class Sink:
            def preserve_mqtt(self, payload, **kwargs): raise AssertionError("valid JSON quarantined")
        class Ingestor:
            def ingest(self, raw): calls.append("ingest")
        record = json.loads((root / "fixtures/mockprofile-300-a-001.json").read_text())["records"][0]
        msg = SimpleNamespace(topic=topic_for("mockprofile-300-a-001"), qos=1, retain=False,
                              payload=json.dumps(record).encode(), mid=7)
        self.assertIsNotNone(accept_message(Client(), msg, Ingestor(), Sink(), "collector"))
        self.assertEqual(calls, ["ingest", "ack"])
        calls.clear()
        record["sourceId"] = "unexpected-source"
        msg.payload = json.dumps(record).encode()
        with self.assertRaisesRegex(ValueError, "unexpected synthetic source"):
            accept_message(Client(), msg, Ingestor(), Sink(), "collector")
        self.assertEqual(calls, [])

    def test_opcua_history_preservation_is_idempotent_and_fails_on_conflict(self):
        class Collision(Exception): pass
        class S3:
            def __init__(self): self.objects = {}
            def put_object(self, **request): self.objects[request["Key"]] = request
        class Table:
            def __init__(self):
                self.items = {}
                self.meta = SimpleNamespace(client=SimpleNamespace(exceptions=SimpleNamespace(ConditionalCheckFailedException=Collision)))
            def put_item(self, Item, ConditionExpression):
                if Item["pk"] in self.items: raise Collision()
                self.items[Item["pk"]] = Item
            def get_item(self, Key, ConsistentRead): return {"Item": self.items.get(Key["pk"])}
        sink = IngestionQuarantine.__new__(IngestionQuarantine)
        sink.s3, sink.table, sink.bucket = S3(), Table(), "private"
        args = {"instance_id": "mockprofile-203-a-001", "profile_id": "mockprofile-203"}
        first = sink.preserve_opcua_history(b'["broken"]', **args)
        self.assertEqual(first, sink.preserve_opcua_history(b'["broken"]', **args))
        obj = next(iter(sink.s3.objects.values()))
        self.assertEqual((obj["Body"], obj["ServerSideEncryption"]), (b'["broken"]', "AES256"))
        item = sink.table.items[f"INGESTION_DLQ#{first}"]
        self.assertEqual((item["protocol"], item["node"]), ("OPCUA", "AvailableSnapshots"))
        item["sha256"] = "tampered"
        with self.assertRaisesRegex(ValueError, "conflicting"):
            sink.preserve_opcua_history(b'["broken"]', **args)
        stir_id = sink.preserve_opcua_history(b'["bad-stir"]', instance_id="mockprofile-210-b-001",
                                              profile_id="mockprofile-210")
        self.assertEqual(sink.table.items[f"INGESTION_DLQ#{stir_id}"]["profile_id"], "mockprofile-210")
        combi_id = sink.preserve_opcua_history(b'["bad-combi"]', instance_id="mockprofile-220-c-001",
                                               profile_id="mockprofile-220")
        self.assertEqual(sink.table.items[f"INGESTION_DLQ#{combi_id}"]["profile_id"], "mockprofile-220")
        with self.assertRaises(ValueError):
            sink.preserve_opcua_history(b"bad", instance_id="mockprofile-200-b-001", profile_id="mockprofile-210")

    def test_failure_boundary(self):
        self.assertEqual(failure(b"\xff\xfe"), ("UNSUPPORTED_ENCODING", []))
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
        binary_sink = IngestionQuarantine.__new__(IngestionQuarantine)
        binary_sink.s3, binary_sink.table, binary_sink.bucket = S3(), Table(), "private-bucket"
        binary_sink.preserve(b"\xff\xfe", **{**kwargs, "reason": "UNSUPPORTED_ENCODING"})
        self.assertEqual(list(binary_sink.s3.objects.values()), [b"\xff\xfe"])
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

    def test_rest_json_schema_failure_preserves_each_vendor_before_cursor(self):
        root = Path(__file__).resolve().parents[3]
        calls = []
        class Client:
            def __init__(self, *args, **kwargs): pass
            def poll(self, cursor): return PollResult((payload,), cursor + 1)
            def close(self): pass
        class Ingestor:
            def __init__(self, *args): pass
            def cursor_state(self, key): return 0, set()
            def ingest(self, raw): raise AssertionError("invalid input entered normal pipeline")
            def save_cursor(self, *args): calls.append("cursor")
        class Sink:
            def __init__(self, *args): pass
            def preserve(self, raw, **kwargs): calls.append((raw, kwargs["reason"], kwargs["instance_id"]))
        config = {name: "test" for name in ("FOODTECH_MOCK_BASE_URL", "FOODTECH_MOCK_CLIENT_ID",
            "FOODTECH_MOCK_CLIENT_SECRET", "FOODTECH_MOCK_CA_FILE", "FOODTECH_DATA_TABLE",
            "FOODTECH_ORDERED_QUEUE_URL", "FOODTECH_INGESTION_DLQ_BUCKET")}
        for vendor in "abc":
            instance = f"mockprofile-100-{vendor}-001"
            record = json.loads((root / f"fixtures/{instance}.json").read_text())["records"][0]
            if vendor == "a": record["measurements"]["temperatureCelsius"] = "invalid"
            elif vendor == "b": record["temperature_f"] = "invalid"
            else: record["Readings"]["TempC"] = "invalid"
            payload = json.dumps(record)
            with self.subTest(vendor=vendor), patch.dict("os.environ", config), \
                 patch.object(cli, "RestMockClient", Client), patch.object(cli, "DynamoOrderedIngestor", Ingestor), \
                 patch.object(cli, "IngestionQuarantine", Sink), patch("sys.argv", ["collector", "--instance", instance]):
                self.assertEqual(cli.main(), 0)
                self.assertEqual(calls, [(payload, "SCHEMA_VIOLATION", instance), "cursor"])
                calls.clear()
                with patch.object(Sink, "preserve", side_effect=RuntimeError("storage unavailable")):
                    with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                        cli.main()
                self.assertEqual(calls, [])

class MqttQuarantineTests(unittest.TestCase):
    def test_raw_bytes_are_preserved_and_each_delivery_has_distinct_id(self):
        class S3:
            def __init__(self): self.objects = []
            def put_object(self, **request): self.objects.append(request)
        class Table:
            def __init__(self): self.items = []
            def put_item(self, **request): self.items.append(request["Item"])
        sink = IngestionQuarantine.__new__(IngestionQuarantine)
        sink.s3, sink.table, sink.bucket = S3(), Table(), "private"
        args = {"topic": "foodtech/synthetic/test", "client_id": "foodtech-collector-test",
                "reason": "UNSUPPORTED_ENCODING", "missing": []}
        first = sink.preserve_mqtt(b'\xff\xfe\x80', **args)
        second = sink.preserve_mqtt(b'\xff\xfe\x80', **args)
        self.assertNotEqual(first, second)
        self.assertEqual(sink.s3.objects[0]["Body"], b'\xff\xfe\x80')
        self.assertEqual(sink.table.items[0]["ingestion_event_id"], first)
        self.assertEqual(sink.table.items[0]["ingestion_failure_reason_code"], "UNSUPPORTED_ENCODING")
        self.assertNotIn("event_id", sink.table.items[0])

    def test_vendor_topic_and_invalid_source_quarantine(self):
        from foodtech_collection_adapter.mqtt import accept_message, topic_for
        calls = []
        class Client:
            def ack(self, mid, qos): calls.append("ack"); return 0
        class Sink:
            def preserve_mqtt(self, payload, **kwargs): calls.append((payload, kwargs["topic"]))
        class Ingestor:
            def ingest(self, raw): raise AssertionError("invalid data entered normal pipeline")
        for vendor in ("b", "c"):
            instance = f"mockprofile-300-{vendor}-001"
            message = SimpleNamespace(topic=topic_for(instance), qos=1, retain=False, payload=b"{bad", mid=7)
            self.assertIsNone(accept_message(Client(), message, Ingestor(), Sink(), "foodtech-collector-test", instance))
            self.assertEqual(calls, [(b"{bad", topic_for(instance)), "ack"])
            calls.clear()
            message.topic = topic_for("mockprofile-300-a-001")
            with self.assertRaisesRegex(ValueError, "message contract"):
                accept_message(Client(), message, Ingestor(), Sink(), "foodtech-collector-test", instance)
            self.assertEqual(calls, [])

    def test_mqtt_xml_malformed_and_dtd_are_quarantined_before_ack(self):
        from foodtech_collection_adapter.mqtt import accept_message, topic_for
        calls = []
        class Client:
            def ack(self, mid, qos): calls.append("ack"); return 0
        class Sink:
            def preserve_mqtt(self, payload, **kwargs): calls.append((payload, kwargs["reason"], kwargs["profile_id"]))
        class Ingestor:
            def ingest(self, raw): raise AssertionError("invalid XML entered normal pipeline")
        instance = "mockprofile-301-a-001"
        for payload in (b"<record><broken>", b'<!DOCTYPE record [<!ENTITY x SYSTEM "file:///etc/passwd">]><record>&x;</record>'):
            message = SimpleNamespace(topic=topic_for(instance), qos=1, retain=False, payload=payload, mid=5)
            self.assertIsNone(accept_message(Client(), message, Ingestor(), Sink(), "foodtech-collector-test", instance))
            self.assertEqual(calls, [(payload, "SCHEMA_VIOLATION", "mockprofile-301"), "ack"])
            calls.clear()

    def test_ack_follows_successful_preservation_only(self):
        from foodtech_collection_adapter.mqtt import TOPIC, accept_message
        calls = []
        class Client:
            def ack(self, mid, qos): calls.append("ack"); return 0
        class Sink:
            def preserve_mqtt(self, payload, **kwargs):
                calls.append(("preserve", payload, kwargs["reason"]))
        class Ingestor:
            def ingest(self, raw): raise AssertionError("quarantined data entered normal pipeline")
        message = SimpleNamespace(topic=TOPIC, qos=1, retain=False, payload=b'\xff', mid=9)
        self.assertIsNone(accept_message(Client(), message, Ingestor(), Sink(), "foodtech-collector-test"))
        self.assertEqual(calls, [("preserve", b'\xff', "UNSUPPORTED_ENCODING"), "ack"])
        calls.clear()
        with patch.object(Sink, "preserve_mqtt", side_effect=RuntimeError("storage unavailable")):
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                accept_message(Client(), message, Ingestor(), Sink(), "foodtech-collector-test")
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(ValueError, "bucket"):
            accept_message(Client(), message, Ingestor(), None, "foodtech-collector-test")
        self.assertEqual(calls, [])
