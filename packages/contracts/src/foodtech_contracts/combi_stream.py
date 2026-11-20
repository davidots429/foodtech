"""Validate a read-only synthetic combi stream against an explicit meal binding."""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal


FIELDS = {"eventId", "mealBatchId", "cookingJobId", "deviceId", "mockInstanceId", "eventType",
          "occurredAt", "receivedAt", "sequence", "schemaVersion", "programVersion", "provenance",
          "dataQuality", "phase", "phaseSecond", "chamberCelsius", "climateSetpointPercent",
          "measuredHumidityPercent", "humidityValueSource", "fanLevel", "doorState"}
V2_FIELDS = FIELDS | {"criticalAlarm", "ventSupported", "ventEvent"}
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate combi field")
        result[key] = value
    return result


def _time(value):
    if not isinstance(value, str):
        raise ValueError("invalid event time")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid event time") from exc
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("combi event time must be UTC")
    return result.astimezone(timezone.utc)


class CombiStreamValidator:
    def __init__(self, meal_id: str, device_id: str, jobs: dict[str, str]):
        if (not all(isinstance(value, str) and ID.fullmatch(value) for value in (meal_id, device_id)) or
                set(jobs) != {"RICE", "TRANSITION", "MACKEREL"} or
                len(set(jobs.values())) != 3 or
                any(not isinstance(value, str) or ID.fullmatch(value) is None for value in jobs.values())):
            raise ValueError("invalid combi meal binding")
        self.meal_id, self.device_id, self.jobs = meal_id, device_id, dict(jobs)
        self.sequence, self.phase, self.phase_second, self.previous_time = 0, None, 0, None
        self.schema_version = None

    def accept(self, raw: str) -> dict:
        record = json.loads(raw, parse_float=Decimal, object_pairs_hook=_pairs)
        if not isinstance(record, dict) or set(record) not in (FIELDS, V2_FIELDS):
            raise ValueError("unexpected combi source fields")
        schema = record["schemaVersion"]
        if ((schema == "synthetic-combi-v1" and set(record) != FIELDS) or
                (schema == "synthetic-combi-v2" and set(record) != V2_FIELDS) or
                schema not in ("synthetic-combi-v1", "synthetic-combi-v2") or
                (self.schema_version is not None and self.schema_version != schema)):
            raise ValueError("invalid or changing combi schema")
        phase = record["phase"]
        if (not isinstance(phase, str) or phase not in self.jobs or record["mealBatchId"] != self.meal_id or
                record["deviceId"] != self.device_id or record["cookingJobId"] != self.jobs[phase] or
                record["mockInstanceId"] != "mockprofile-120-a-001" or
                record["provenance"] != "SYNTHETIC" or record["dataQuality"] != "VALID" or
                record["eventType"] != "OBSERVATION" or
                not isinstance(record["programVersion"], str) or ID.fullmatch(record["programVersion"]) is None):
            raise ValueError("combi source does not match the binding")
        sequence = record["sequence"]
        if (type(sequence) is not int or sequence != self.sequence + 1 or
                record["eventId"] != f"{self.meal_id}-{sequence:06d}"):
            raise ValueError("invalid combi event sequence")
        allowed_next = {None: "RICE", "RICE": "TRANSITION", "TRANSITION": "MACKEREL"}
        if phase != self.phase and allowed_next.get(self.phase) != phase:
            raise ValueError("invalid combi phase order")
        next_phase_second = self.phase_second + 1 if phase == self.phase else 1
        if type(record["phaseSecond"]) is not int or record["phaseSecond"] != next_phase_second:
            raise ValueError("invalid combi phase sequence")
        occurred, received = _time(record["occurredAt"]), _time(record["receivedAt"])
        if received < occurred or (self.previous_time and occurred != self.previous_time + timedelta(seconds=1)):
            raise ValueError("invalid combi observation time")
        for key, lower, upper in (("chamberCelsius", -50, 350), ("climateSetpointPercent", 0, 100)):
            value = record[key]
            if type(value) not in (int, Decimal) or not Decimal(value).is_finite() or not lower <= value <= upper:
                raise ValueError("invalid combi measurement")
        humidity, source = record["measuredHumidityPercent"], record["humidityValueSource"]
        if source == "NOT_SUPPORTED":
            if humidity is not None:
                raise ValueError("unsupported humidity cannot have a value")
        elif (source not in ("MEASURED", "CONTROLLER_ESTIMATE") or type(humidity) not in (int, Decimal) or
              not Decimal(humidity).is_finite() or not 0 <= humidity <= 100):
            raise ValueError("invalid combi humidity")
        if type(record["fanLevel"]) is not int or not 0 <= record["fanLevel"] <= 10 or \
                record["doorState"] not in ("OPEN", "CLOSED"):
            raise ValueError("invalid combi fan or door")
        if schema == "synthetic-combi-v2" and (
                type(record["criticalAlarm"]) is not bool or type(record["ventSupported"]) is not bool or
                record["ventEvent"] not in (None, "OPENED", "CLOSED") or
                (record["ventEvent"] is not None and not record["ventSupported"])):
            raise ValueError("invalid combi alarm or vent observation")
        self.sequence, self.phase, self.phase_second, self.previous_time = sequence, phase, next_phase_second, occurred
        self.schema_version = schema
        return record
