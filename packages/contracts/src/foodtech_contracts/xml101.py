"""XSD-validated synthetic REST XML; external entities and DTDs are forbidden."""
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from lxml import etree

_SCHEMAS = {(profile, v): etree.XMLSchema(etree.fromstring(files("foodtech_contracts").joinpath(
    f"mockprofile-{profile}-{v}-v1.xsd").read_bytes())) for profile in ("101", "301", "311", "321") for v in ("a", "b", "c")}


def _decimal_field(value):
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("non-finite XML number")
        return number
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid XML number") from exc


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
    profiles = {f"mockprofile-{profile}-{vendor}-001": profile for profile in ("101", "301", "311", "321")}
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
        if profiles[instance] == "321":
            if any(key not in result for key in ("ext_climate_setpoint_percent", "ext_humidity_percent", "ext_fan_level")):
                raise ValueError("missing synthetic combi XML field")
            result["ext_climate_setpoint_percent"] = _decimal_field(result["ext_climate_setpoint_percent"])
            result["ext_humidity_percent"] = (_decimal_field(result["ext_humidity_percent"]) if result["ext_humidity_percent"] else None)
            result["ext_fan_level"] = int(result["ext_fan_level"])
        return result
    if vendor == "c":
        extension = _extension(root.find("Extension"))
        if profiles[instance] == "321":
            if any(key not in extension for key in ("ClimateSetpointPct", "HumidityPct", "FanLevel")):
                raise ValueError("missing synthetic combi XML field")
            extension["ClimateSetpointPct"] = _decimal_field(extension["ClimateSetpointPct"])
            extension["HumidityPct"] = _decimal_field(extension["HumidityPct"]) if extension["HumidityPct"] else None
            extension["FanLevel"] = int(extension["FanLevel"])
        return {"SchVer": text("SchVer"), "SrcId": text("SrcId"), "DeviceId": text("DeviceId"),
                "BatchId": text("BatchId"), "RecId": text("RecId"), "SeqNo": int(text("SeqNo")),
                "Timestamp": text("Timestamp"), "Status": text("Status"),
                "Readings": {"TempC": Decimal(text("Readings/TempC")), "SpeedLvl": int(text("Readings/SpeedLvl")),
                             "ElapsedMin": Decimal(text("Readings/ElapsedMin"))},
                "Alarm": text("Alarm"), "Err": text("Err") or "", "Extension": extension}
    if len(root.findall("extensions/firmwareVersion")) != 1:
        raise ValueError("duplicate XML firmware field")
    extensions = {"firmwareVersion": text("extensions/firmwareVersion")}
    if profiles[instance] in {"311", "321"}:
        extensions["equipmentType"] = text("extensions/equipmentType")
    if profiles[instance] == "321":
        humidity = text("extensions/humidityPercent")
        extensions.update(climateSetpointPercent=Decimal(text("extensions/climateSetpointPercent")),
                          humiditySource=text("extensions/humiditySource"),
                          humidityPercent=Decimal(humidity) if humidity else None,
                          fanLevel=int(text("extensions/fanLevel")))
    return {"schemaVersion": text("schemaVersion"), "sourceId": text("sourceId"),
            "deviceId": text("deviceId"), "batchId": text("batchId"), "recordId": text("recordId"),
            "sequenceNumber": int(text("sequenceNumber")), "eventTime": text("eventTime"),
            "deviceStatus": text("deviceStatus"),
            "measurements": {"temperatureCelsius": Decimal(text("measurements/temperatureCelsius")),
                             "speedRpm": int(text("measurements/speedRpm")),
                             "elapsedSeconds": int(text("measurements/elapsedSeconds"))},
            "errorReason": text("errorReason") or None,
            "extensions": extensions}
