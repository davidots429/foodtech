"""Approved synthetic fryer targets and a recorded batch's review scope."""
from decimal import Decimal, InvalidOperation

from .tonkatsu_batch import MAX_BATCH_G
from .tonkatsu_process import TIME_BANDS

RULE = "tonkatsu-approved-targets-v1"
TARGET_SECONDS = {"THIN": 180, "STANDARD": 300, "THICK": 360}
TARGET_TEMPERATURE_C = 170
THICKNESS_RANGES = {"THIN": ("0.5", "1.0", False), "STANDARD": ("1.0", "1.5", False),
                    "THICK": ("1.5", "2.0", True)}


def build_tonkatsu_review_basis(recipe_version, profile, mass_kg, prior_failed, prior_seconds):
    if (type(recipe_version) is not str or not 1 <= len(recipe_version) <= 80 or not recipe_version.strip()
            or type(profile) is not str or profile not in TARGET_SECONDS
            or type(mass_kg) is not Decimal or not mass_kg.is_finite()
            or not Decimal("0.001") <= mass_kg <= Decimal(MAX_BATCH_G) / 1000
            or type(prior_failed) is not bool or type(prior_seconds) is not int or not 1 <= prior_seconds <= 86400):
        raise ValueError("invalid fryer review basis")
    low, high, closed = THICKNESS_RANGES[profile]
    minimum, *_, maximum = TIME_BANDS[profile]
    return {
        "rule_version": RULE, "reference_recipe_version": recipe_version, "profile": profile,
        "target_temperature_c": TARGET_TEMPERATURE_C, "target_duration_seconds": TARGET_SECONDS[profile],
        "minimum_thickness_cm": low, "maximum_thickness_cm": high, "include_maximum_thickness": closed,
        "observed_batch_mass_kg": format(mass_kg.normalize(), "f"), "load_scope": "OBSERVED_BATCH_ONLY",
        "minimum_temperature_c": 165, "maximum_temperature_c": 175,
        "minimum_duration_seconds": max(minimum, prior_seconds) if prior_failed else minimum,
        "maximum_duration_seconds": maximum, "previous_actual_duration_seconds": prior_seconds,
        "reason_code": "PRIOR_INITIAL_SAFETY_FAILURE" if prior_failed else "WITHIN_APPROVED_LIMITS",
    }


def validate_tonkatsu_review_basis(value, data):
    try:
        if type(value) is not dict or type(value["observed_batch_mass_kg"]) is not str or len(value["observed_batch_mass_kg"]) > 12:
            raise ValueError
        expected = build_tonkatsu_review_basis(value["reference_recipe_version"], value["profile"],
            Decimal(value["observed_batch_mass_kg"]), value["reason_code"] == "PRIOR_INITIAL_SAFETY_FAILURE",
            value["previous_actual_duration_seconds"])
        if value != expected or any(type(value[name]) is not type(field) for name, field in expected.items()):
            raise ValueError
        if not expected["minimum_temperature_c"] <= Decimal(data["temperature_c"]) <= expected["maximum_temperature_c"] or (
            not expected["minimum_duration_seconds"] <= data["duration_seconds"] <= expected["maximum_duration_seconds"]
        ):
            raise ValueError
        return expected
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ValueError("invalid stored fryer review basis") from None
