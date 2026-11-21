"""Identify recorded changes relevant to synthetic validation reuse.

These functions inspect only supplied records. They do not prove that the
records are complete or that a validation is active.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ChangeAssessment:
    reasons: tuple[str, ...]
    unknown_fields: tuple[str, ...]


_CONTEXT_FIELDS = {
    "RECIPE_CHANGED": ("recipe_version",),
    "EQUIPMENT_CHANGED": ("equipment_profile", "equipment_device_id", "source_id"),
    "INGREDIENT_CHANGED": ("ingredient_spec_id", "ingredient_lot_id", "supplier_id"),
    "SENSOR_CHANGED": ("sensor_id", "sensor_calibration_record_id"),
    "PROCESS_CONDITION_CHANGED": ("process_condition_profile_id", "process_condition_version"),
}


def compare_jeyuk_context(previous: dict, current: dict) -> ChangeAssessment:
    """Compare recorded identity fields; missing fields remain unknown."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("two recorded contexts are required")
    reasons, unknown = [], []
    if previous.get("schema_version") != "jeyuk-validation-context-v1" or \
            current.get("schema_version") != "jeyuk-validation-context-v1":
        unknown.append("schema_version")
    for reason, fields in _CONTEXT_FIELDS.items():
        changed = False
        for name in fields:
            old, new = previous.get(name), current.get(name)
            if not isinstance(old, str) or not old.strip() or not isinstance(new, str) or not new.strip():
                unknown.append(name)
            elif old != new:
                changed = True
        if changed:
            reasons.append(reason)
    return ChangeAssessment(tuple(reasons), tuple(unknown))


def recorded_safety_change(event: dict) -> str | None:
    """Return an explicit invalidation candidate from a recorded safety event."""
    if not isinstance(event, dict):
        raise ValueError("event must be a mapping")
    domain, trigger = event.get("domain"), event.get("trigger")
    if domain == "SAFETY_STATUS" and event.get("to_status") in {"FAILED", "SUSPECTED", "UNDER_REVIEW"}:
        return "SAFETY_ANOMALY"
    if domain == "SAFETY_STATUS" and event.get("from_status") == "PASSED" and event.get("to_status") == "INCOMPLETE":
        return "SAFETY_ANOMALY"
    if domain == "SAFETY_STATUS" and event.get("kind") == "REVALIDATION":
        return "MANUAL_SAFETY_REVISED"
    if domain == "JEYUK_FEED" and trigger == "FEED_REVISED":
        return "INGREDIENT_RECORD_REVISED"
    if domain == "JEYUK_DISCHARGE" and trigger == "DISCHARGE_REVISED":
        return "DISCHARGE_RECORD_REVISED"
    if domain == "PROCESS_ASSESSMENT":
        reasons = event.get("reason_codes")
        if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
            raise ValueError("process reason codes must be a list of strings")
        if event.get("to_status") == "FAILED" or event.get("auto_safety_status") == "ASSESSMENT_INCOMPLETE" or any(reason in {"AGITATOR_JAM_WARNING", "AGITATOR_FAILURE"}
                                                     for reason in reasons):
            return "PROCESS_ANOMALY"
    return None
