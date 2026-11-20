"""XSD-validated synthetic REST XML; external entities and DTDs are forbidden."""
from decimal import Decimal
from importlib.resources import files
from lxml import etree

_SCHEMA = etree.XMLSchema(etree.fromstring(files("foodtech_contracts").joinpath("mockprofile-101-a-v1.xsd").read_bytes()))


def parse_record(raw: str | bytes) -> dict:
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(payload) > 2_000_000:
        raise ValueError("synthetic XML exceeds the size limit")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    root = etree.fromstring(payload, parser)
    info = root.getroottree().docinfo
    if info.doctype:
        raise ValueError("XML DTDs are not permitted")
    if info.encoding and info.encoding.upper().replace("_", "-") not in {"UTF-8", "UTF8"}:
        raise ValueError("only UTF-8 XML is permitted")
    _SCHEMA.assertValid(root)
    text = root.findtext
    return {"schemaVersion": text("schemaVersion"), "sourceId": text("sourceId"),
            "deviceId": text("deviceId"), "batchId": text("batchId"), "recordId": text("recordId"),
            "sequenceNumber": int(text("sequenceNumber")), "eventTime": text("eventTime"),
            "deviceStatus": text("deviceStatus"),
            "measurements": {"temperatureCelsius": Decimal(text("measurements/temperatureCelsius")),
                             "speedRpm": int(text("measurements/speedRpm")),
                             "elapsedSeconds": int(text("measurements/elapsedSeconds"))},
            "errorReason": text("errorReason") or None,
            "extensions": {"firmwareVersion": text("extensions/firmwareVersion")}}
