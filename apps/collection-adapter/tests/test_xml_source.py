import json
import unittest
from pathlib import Path
from unittest.mock import patch

from lxml import etree
import jsonschema
from foodtech_contracts import canonical_record, decode_record
from foodtech_collection_adapter import __main__ as cli
from foodtech_collection_adapter.rest import PollResult
from foodtech_standardization_worker.process import assess

ROOT = Path(__file__).resolve().parents[3]


class XmlSourceTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / "fixtures/mockprofile-101-a-001.json").read_text())["records"][0]

    def test_xsd_mapping_and_security(self):
        record = decode_record(self.raw)
        self.assertEqual(record["deviceId"], "mockprofile-101-a-001")
        self.assertEqual(assess("event", self.raw).mapping_version, "mockprofile-101-a-map-v1")
        with self.assertRaises(etree.DocumentInvalid):
            decode_record(self.raw.replace("<speedRpm>60</speedRpm>", "<speedRpm>-1</speedRpm>"))
        with self.assertRaisesRegex(ValueError, "DTDs"):
            decode_record('<!DOCTYPE record [<!ENTITY x SYSTEM "file:///etc/passwd">]>' +
                          self.raw.replace('synthetic-rest-xml', '&x;'))
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            decode_record('<?xml version="1.0" encoding="ISO-8859-1"?>' + self.raw)

    def test_vendor_schemas_preserve_values_and_validate_extensions(self):
        for vendor in ("b", "c"):
            raw = json.loads((ROOT / f"fixtures/mockprofile-101-{vendor}-001.json").read_text())["records"][0]
            record = decode_record(raw)
            self.assertEqual(record["deviceId"], f"mockprofile-101-{vendor}-001")
            result = assess("event", raw)
            self.assertEqual(result.mapping_version, f"mockprofile-101-{vendor}-map-v1")
            self.assertNotIn("ROTATIONAL_SPEED_RPM", result.normalized)
            if vendor == "b":
                augmented = raw.replace("</record>", "<ext_custom>original</ext_custom><unknown>value</unknown></record>")
                self.assertEqual(canonical_record(decode_record(augmented))["extensions"]["ext_custom"], "original")
                with self.assertRaises(jsonschema.ValidationError):
                    decode_record(raw.replace("<device_status>1</device_status>", "<device_status>9</device_status>"))
            else:
                augmented = raw.replace("</Extension>", "<Nested><Code>v</Code></Nested></Extension>")
                self.assertEqual(canonical_record(decode_record(augmented))["extensions"]["vendor_extension"]["Nested"], {"Code": "v"})
                with self.assertRaises(jsonschema.ValidationError):
                    decode_record(raw.replace("<Alarm>N</Alarm>", "<Alarm>invalid</Alarm>"))
            with self.assertRaisesRegex(ValueError, "requires XML"):
                decode_record(json.dumps({"deviceId": f"mockprofile-101-{vendor}-001"}))

    def test_invalid_xml_is_quarantined_before_cursor(self):
        calls = []
        class Client:
            def __init__(self, *args, **kwargs): pass
            def poll(self, cursor): return PollResult(("<record><broken/></record>",), cursor + 1)
            def close(self): pass
        class Ingestor:
            def __init__(self, *args): pass
            def cursor_state(self, key): return 0, set()
            def ingest(self, raw): raise AssertionError("invalid XML entered normal pipeline")
            def save_cursor(self, *args): calls.append("cursor")
        class Sink:
            def __init__(self, *args): pass
            def preserve(self, raw, **kwargs): calls.append(("preserve", raw, kwargs["reason"], kwargs["profile_id"]))
        config = {name: "test" for name in ("FOODTECH_MOCK_BASE_URL", "FOODTECH_MOCK_CLIENT_ID",
            "FOODTECH_MOCK_CLIENT_SECRET", "FOODTECH_MOCK_CA_FILE", "FOODTECH_DATA_TABLE",
            "FOODTECH_ORDERED_QUEUE_URL", "FOODTECH_INGESTION_DLQ_BUCKET")}
        with patch.dict("os.environ", config), patch.object(cli, "RestMockClient", Client), \
             patch.object(cli, "DynamoOrderedIngestor", Ingestor), patch.object(cli, "IngestionQuarantine", Sink), \
             patch("sys.argv", ["collector", "--profile", "mockprofile-101", "--instance", "mockprofile-101-a-001"]):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(calls, [("preserve", "<record><broken/></record>", "SCHEMA_VIOLATION", "mockprofile-101"), "cursor"])
        calls.clear()
        with patch.dict("os.environ", config), patch.object(cli, "RestMockClient", Client), \
             patch.object(cli, "DynamoOrderedIngestor", Ingestor), patch.object(cli, "IngestionQuarantine", Sink), \
             patch.object(Sink, "preserve", side_effect=RuntimeError("storage unavailable")), \
             patch("sys.argv", ["collector", "--profile", "mockprofile-101", "--instance", "mockprofile-101-a-001"]):
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                cli.main()
        self.assertEqual(calls, [])
