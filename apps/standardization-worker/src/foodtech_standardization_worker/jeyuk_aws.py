"""Immutable Jeyuk process decisions with an atomic per-fixture monitor pointer."""

from dataclasses import asdict, fields

from boto3.dynamodb.types import TypeSerializer

from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample
from foodtech_contracts.jeyuk_process import ProcessMonitor, RULE_VERSION
from foodtech_contracts.jeyuk_safety import JeyukSafetyMonitor, RULE_VERSION as SAFETY_RULE_VERSION


class DynamoJeyukProcessor:
    def __init__(self, table, ddb):
        self.table = table
        self.ddb = ddb
        self.serializer = TypeSerializer()

    def process(self, reference: dict) -> dict:
        if reference.get("profile_id") != "mockprofile-110":
            raise ValueError("unsupported Jeyuk process profile")
        pk = reference["pk"]
        second = reference["second"]
        if type(second) is not int or second < 1:
            raise ValueError("invalid Jeyuk second")
        assessment_key = {"pk": pk, "sk": f"ASSESSMENT#{second:06d}"}
        existing = self.table.get_item(Key=assessment_key, ConsistentRead=True).get("Item")
        if existing:
            return self._result(existing, replayed=True)
        raw_sk = f"RAW#{second:06d}"
        if reference["raw_sk"] != raw_sk:
            raise ValueError("Jeyuk reference does not match its second")
        raw_item = self.table.get_item(Key={"pk": pk, "sk": raw_sk}, ConsistentRead=True)["Item"]
        sample = decode_jeyuk_mock_sample(raw_item["raw"])
        if (pk != f"JEYUK#{sample.fixture_id}" or reference["fixture_id"] != sample.fixture_id
                or sample.second != second):
            raise ValueError("Jeyuk reference does not match its source")
        state_key = {"pk": pk, "sk": "STATE"}
        state = self.table.get_item(Key=state_key, ConsistentRead=True).get("Item")
        if state:
            if state.get("process_rule_version") != RULE_VERSION:
                raise ValueError("Jeyuk process rule changed; preserve this stream and use a new derived run")
            prior = int(state["last_second"])
            values = {field.name: state[field.name] for field in fields(ProcessMonitor)}
            values["failures"] = set(values["failures"])
            monitor = ProcessMonitor(**values)
            safety = (JeyukSafetyMonitor(**state["safety_state"])
                      if state.get("safety_state") is not None else None)
            if monitor.profile_name != sample.profile or monitor.actual_load_kg != sample.actual_load_kg:
                raise ValueError("Jeyuk process identity changed")
        else:
            prior = 0
            monitor = ProcessMonitor(sample.profile, sample.actual_load_kg)
            safety = JeyukSafetyMonitor() if sample.mixture_celsius is not None else None
        if second != prior + 1:
            raise ValueError("Jeyuk process sample is out of order")
        if (safety is None) != (sample.mixture_celsius is None):
            raise ValueError("Jeyuk safety sample contract changed within a fixture")
        reading = monitor.observe(
            second, surface_celsius=sample.surface_celsius, actual_rpm=sample.actual_rpm,
            target_rpm=sample.target_rpm, motor_load_percent=sample.motor_load_percent,
        )
        safety_reading = (safety.observe(
            second, mixture_celsius=sample.mixture_celsius,
            calibration_status=sample.calibration_status,
            safety_sensor_position=sample.safety_sensor_position,
            last_ingredient_addition_completed=sample.last_ingredient_addition_completed,
        ) if safety is not None else None)
        assessment = {
            **assessment_key, "fixture_id": sample.fixture_id, "profile_id": "mockprofile-110",
            "raw_sk": raw_sk, "second": second, "process_status": reading.status,
            "process_rule_version": RULE_VERSION,
            "reason_codes": list(reading.reason_codes), "surface_band": reading.surface_band,
            "rpm_band": reading.rpm_band, "load_status": reading.load_status,
            "profile": sample.profile, "actual_load_kg": str(sample.actual_load_kg),
            "auto_safety_status": safety_reading.status if safety_reading else "NOT_MEASURED",
            "mixture_hold_seconds": safety_reading.hold_seconds if safety_reading else 0,
            "last_addition_second": safety_reading.last_addition_second if safety_reading else None,
            "safety_passed_at_second": safety_reading.passed_at_second if safety_reading else None,
            "safety_rule_version": SAFETY_RULE_VERSION if safety_reading else None,
            "provenance": "SYNTHETIC",
        }
        monitor_state = asdict(monitor)
        monitor_state["failures"] = sorted(monitor.failures)
        next_state = {**state_key, **monitor_state, "process_rule_version": RULE_VERSION,
                      "safety_state": asdict(safety) if safety is not None else None}
        state_put = {"TableName": self.table.name, "Item": self._marshal(next_state),
                     "ConditionExpression": "attribute_not_exists(pk)" if prior == 0 else "last_second = :prior AND process_rule_version = :rule"}
        if prior:
            state_put["ExpressionAttributeValues"] = {":prior": self.serializer.serialize(prior), ":rule": self.serializer.serialize(RULE_VERSION)}
        try:
            self.ddb.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table.name, "Item": self._marshal(assessment),
                         "ConditionExpression": "attribute_not_exists(pk)"}},
                {"Put": state_put},
            ])
        except self.ddb.exceptions.TransactionCanceledException:
            existing = self.table.get_item(Key=assessment_key, ConsistentRead=True).get("Item")
            if not existing:
                raise
            return self._result(existing, replayed=True)
        return self._result(assessment, replayed=False)

    @staticmethod
    def _result(item: dict, *, replayed: bool) -> dict:
        return {"fixture_id": item["fixture_id"], "second": int(item["second"]),
                "process_status": item["process_status"], "reason_codes": item["reason_codes"],
                "process_rule_version": item.get("process_rule_version", "jeyuk-process-v1"),
                "surface_band": item["surface_band"], "rpm_band": item["rpm_band"],
                "load_status": item["load_status"],
                "profile": item["profile"], "actual_load_kg": item["actual_load_kg"],
                "auto_safety_status": item.get("auto_safety_status", "NOT_MEASURED"),
                "mixture_hold_seconds": int(item.get("mixture_hold_seconds", 0)),
                "last_addition_second": item.get("last_addition_second"),
                "safety_passed_at_second": item.get("safety_passed_at_second"),
                "replayed": replayed}

    def _marshal(self, item: dict) -> dict:
        return {key: self.serializer.serialize(value) for key, value in item.items()}
