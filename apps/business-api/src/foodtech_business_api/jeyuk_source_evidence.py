"""Bounded, read-only audit of synthetic Jeyuk source assessments and raw samples."""

from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample
from foodtech_contracts.jeyuk_process import ProcessMonitor, PROFILES, RULE_VERSION as PROCESS_RULE_VERSION
from foodtech_contracts.jeyuk_safety import JeyukSafetyMonitor, RULE_VERSION as SAFETY_RULE_VERSION


MAX_SECONDS = max(profile.time_bands[-1] for profile in PROFILES.values())


def read_source_records(table_name: str, batch_id: str, last_second: int) -> tuple[list[dict], list[dict]]:
    if not 1 <= last_second <= MAX_SECONDS:
        raise ValueError("Jeyuk source range exceeds the supported profile")
    table = boto3.Session(region_name="ap-southeast-2").resource("dynamodb").Table(table_name)
    pk = f"JEYUK#{batch_id}"

    def read_prefix(prefix: str) -> list[dict]:
        items = []
        start_key = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk) &
                    Key("sk").between(f"{prefix}#000001", f"{prefix}#{last_second:06d}"),
                    "ConsistentRead": True}
            if start_key:
                args["ExclusiveStartKey"] = start_key
            page = table.query(**args)
            items.extend(page.get("Items", []))
            if len(items) > MAX_SECONDS:
                raise ValueError("Jeyuk source range exceeds the supported profile")
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                return items

    return read_prefix("ASSESSMENT"), read_prefix("RAW")


def audit_source_records(job: dict, assessments: list[dict], raws: list[dict]) -> dict:
    binding = job["source_binding"]
    context = job["validation_context"]
    process = job["process_assessment"]
    last_second = int(process["second"])
    expected_pk = f"JEYUK#{binding['batch_id']}"
    contiguous = (1 <= last_second <= MAX_SECONDS and
                  len(assessments) == last_second and len(raws) == last_second and
                  all(item.get("pk") == expected_pk and item.get("sk") == f"ASSESSMENT#{index:06d}"
                      for index, item in enumerate(assessments, 1)) and
                  all(item.get("pk") == expected_pk and item.get("sk") == f"RAW#{index:06d}"
                      for index, item in enumerate(raws, 1)))
    result = {"source_records_contiguous": contiguous,
              "source_records_match_binding": False,
              "source_derived_results_match_raw": False,
              "source_process_conditions_normal": False,
              "source_safety_samples_valid": False,
              "source_validation_identity_matches": False,
              "source_relative_duration_covers_cooking": False,
              "source_process_span_covers_cooking": False}
    if not contiguous:
        return result
    size = job.get("jeyuk_size_profile")
    if size not in PROFILES or job.get("equipment_profile") != f"jeyuk-{size.lower()}":
        return result
    expected_load = Decimal(context["actual_load_kg"])
    samples = []
    process_monitor = ProcessMonitor(size, expected_load)
    safety_monitor = JeyukSafetyMonitor()
    process_readings = []
    for index, (assessment, raw) in enumerate(zip(assessments, raws), 1):
        if assessment.get("raw_sk") != raw["sk"]:
            return result
        try:
            sample = decode_jeyuk_mock_sample(raw["raw"])
        except (KeyError, TypeError, ValueError):
            return result
        try:
            assessed_load = Decimal(str(assessment.get("actual_load_kg", "-1")))
        except InvalidOperation:
            return result
        if (sample.fixture_id != binding["batch_id"] or sample.second != index or
                sample.profile != size or sample.actual_load_kg != expected_load or
                assessment.get("fixture_id") != binding["batch_id"] or
                assessment.get("second") != index or
                assessment.get("profile_id") != "mockprofile-110" or
                assessment.get("profile") != size or
                assessed_load != expected_load or assessment.get("provenance") != "SYNTHETIC"):
            return result
        if sample.mixture_celsius is None:
            return result
        try:
            reading = process_monitor.observe(
                index, surface_celsius=sample.surface_celsius, actual_rpm=sample.actual_rpm,
                target_rpm=sample.target_rpm, motor_load_percent=sample.motor_load_percent)
            safety = safety_monitor.observe(
                index, mixture_celsius=sample.mixture_celsius,
                calibration_status=sample.calibration_status,
                safety_sensor_position=sample.safety_sensor_position,
                last_ingredient_addition_completed=sample.last_ingredient_addition_completed)
        except (TypeError, ValueError):
            return result
        if (assessment.get("process_status") != reading.status or
                assessment.get("reason_codes") != list(reading.reason_codes) or
                assessment.get("surface_band") != reading.surface_band or
                assessment.get("rpm_band") != reading.rpm_band or
                assessment.get("load_status") != reading.load_status or
                assessment.get("auto_safety_status") != safety.status or
                assessment.get("mixture_hold_seconds") != safety.hold_seconds or
                assessment.get("last_addition_second") != safety.last_addition_second or
                assessment.get("safety_passed_at_second") != safety.passed_at_second or
                assessment.get("safety_rule_version") != SAFETY_RULE_VERSION):
            return result
        samples.append(sample)
        process_readings.append(reading)
    last = assessments[-1]
    if (last.get("sk") != process["source_assessment_sk"] or
            last.get("process_status") != process["status"] or
            last.get("auto_safety_status") != job["auto_safety_status"] or
            last.get("mixture_hold_seconds") != job["mixture_hold_seconds"] or
            last.get("safety_passed_at_second") != job["safety_passed_at_second"]):
        return result
    result["source_records_match_binding"] = True
    result["source_derived_results_match_raw"] = True
    result["source_process_conditions_normal"] = (
        job.get("process_time_status") == "NORMAL" and
        job.get("process_assessment_status") == "NORMAL" and
        all(reading.status == "NORMAL" for reading in process_readings))
    result["source_safety_samples_valid"] = (
        last.get("safety_rule_version") == SAFETY_RULE_VERSION and
        last.get("auto_safety_status") == "PASSED" and
        all(sample.calibration_status == "OK" and
            sample.safety_sensor_position == "LOWEST_MIXTURE_POINT"
            for sample in samples))
    expected_identity = {
        "ingredientSpecId": context.get("ingredient_spec_id"),
        "ingredientLotId": context.get("ingredient_lot_id"),
        "supplierId": context.get("supplier_id"),
        "sensorId": context.get("sensor_id"),
        "sensorCalibrationRecordId": context.get("sensor_calibration_record_id"),
        "processConditionProfileId": context.get("process_condition_profile_id"),
        "processConditionVersion": context.get("process_condition_version"),
    }
    result["source_validation_identity_matches"] = (
        all(isinstance(value, str) and value for value in expected_identity.values()) and
        all(sample.validation_identity == expected_identity for sample in samples))
    duration = job.get("actual_cooking_seconds")
    result["source_relative_duration_covers_cooking"] = (
        type(duration) is int and duration > 0 and last_second >= duration and
        process.get("rule_version") == PROCESS_RULE_VERSION)
    try:
        placed = datetime.fromisoformat(job["actual_placed_at"].replace("Z", "+00:00"))
        lifted = datetime.fromisoformat(job["actual_lifted_at"].replace("Z", "+00:00"))
        result["source_process_span_covers_cooking"] = bool(
            result["source_relative_duration_covers_cooking"] and placed.utcoffset() is not None and
            lifted.utcoffset() is not None and lifted > placed and
            int((lifted - placed).total_seconds()) == duration and
            all(sample.observed_at == placed + timedelta(seconds=index)
                for index, sample in enumerate(samples, 1)) and samples[-1].observed_at >= lifted)
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
        pass
    return result
