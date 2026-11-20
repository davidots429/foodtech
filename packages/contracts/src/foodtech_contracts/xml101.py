"""XSD-validated synthetic REST XML; external entities and DTDs are forbidden."""
from decimal import Decimal
from importlib.resources import files
from lxml import etree

_SCHEMAS = {(profile, v): etree.XMLSchema(etree.fromstring(files("foodtech_contracts").joinpath(
    f"mockprofile-{profile}-{v}-v1.xsd").read_bytes())) for profile in ("101", "301") for v in ("a", "b", "c")}


def _extension(element):
    result = {}
    if element.attrib or (element.text and element.text.strip()):
        raise ValueError("mixed or attributed XML extension is unsupported")
    for child in element:
        if (not isinstance(child.tag, str) or child.tag in result or child.attrib or
                (child.tail and child.tail.strip())):
            raise ValueError("invalid or duplicate XML extension")
        result[child.tag] = _extension(child) if len(child) else (child.text or "")
    return result


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
    tags = [item.tag for item in root if isinstance(item.tag, str)]
    if len(tags) != len(set(tags)):
        raise ValueError("duplicate XML root field")
    vendor = "b" if root.find("device_id") is not None else "c" if root.find("DeviceId") is not None else "a"
    text = root.findtext
    instance = text({"a": "deviceId", "b": "device_id", "c": "DeviceId"}[vendor])
    profiles = {f"mockprofile-{profile}-{vendor}-001": profile for profile in ("101", "301")}
    if instance not in profiles:
        raise ValueError("unsupported synthetic XML instance")
    _SCHEMAS[(profiles[instance], vendor)].assertValid(root)
    if vendor == "b":
        result = {"schema_version": text("schema_version"), "source_id": text("source_id"),
                  "device_id": text("device_id"), "batch_id": text("batch_id"), "record_id": text("record_id"),
                  "sequence_number": int(text("sequence_number")), "event_time_ms": int(text("event_time_ms")),
                  "device_status": int(text("device_status")), "temperature_f": Decimal(text("temperature_f")),
                  "output_percent": Decimal(text("output_percent")), "elapsed_ms": int(text("elapsed_ms"))}
        for child in root:
            if isinstance(child.tag, str) and child.tag.startswith("ext_"):
                if len(child) or child.attrib:
                    raise ValueError("vendor-b XML extension must be a scalar")
                result[child.tag] = child.text or ""
        return result
    if vendor == "c":
        return {"SchVer": text("SchVer"), "SrcId": text("SrcId"), "DeviceId": text("DeviceId"),
                "BatchId": text("BatchId"), "RecId": text("RecId"), "SeqNo": int(text("SeqNo")),
                "Timestamp": text("Timestamp"), "Status": text("Status"),
                "Readings": {"TempC": Decimal(text("Readings/TempC")), "SpeedLvl": int(text("Readings/SpeedLvl")),
                             "ElapsedMin": Decimal(text("Readings/ElapsedMin"))},
                "Alarm": text("Alarm"), "Err": text("Err") or "", "Extension": _extension(root.find("Extension"))}
    if len(root.findall("extensions/firmwareVersion")) != 1:
        raise ValueError("duplicate XML firmware field")
    return {"schemaVersion": text("schemaVersion"), "sourceId": text("sourceId"),
            "deviceId": text("deviceId"), "batchId": text("batchId"), "recordId": text("recordId"),
            "sequenceNumber": int(text("sequenceNumber")), "eventTime": text("eventTime"),
            "deviceStatus": text("deviceStatus"),
            "measurements": {"temperatureCelsius": Decimal(text("measurements/temperatureCelsius")),
                             "speedRpm": int(text("measurements/speedRpm")),
                             "elapsedSeconds": int(text("measurements/elapsedSeconds"))},
            "errorReason": text("errorReason") or None,
            "extensions": {"firmwareVersion": text("extensions/firmwareVersion")}}
