"""Approved synthetic tonkatsu thickness and elapsed cooking-time rules."""

from decimal import Decimal

RULE_VERSION = "tonkatsu-thickness-time-v1"

TIME_BANDS = {
    "THIN": (150, 165, 195, 210, 240),
    "STANDARD": (270, 285, 315, 330, 360),
    "THICK": (330, 345, 375, 390, 420),
}


def thickness_profile(thickness_cm: Decimal) -> tuple[str, str | None]:
    if thickness_cm < Decimal("0.5"):
        return "BELOW_RANGE", None
    if thickness_cm < Decimal("1.0"):
        return "IN_RANGE", "THIN"
    if thickness_cm < Decimal("1.5"):
        return "IN_RANGE", "STANDARD"
    if thickness_cm <= Decimal("2.0"):
        return "IN_RANGE", "THICK"
    return "ABOVE_RANGE", None


def cooking_time_status(profile: str | None, seconds: int) -> str:
    if profile is None:
        return "NOT_APPLICABLE"
    early, short, normal, delay, maximum = TIME_BANDS[profile]
    if seconds < early:
        return "EARLY_REMOVAL"
    if seconds < short:
        return "SHORT_WARNING"
    if seconds <= normal:
        return "NORMAL"
    if seconds <= delay:
        return "DELAY_WARNING"
    if seconds <= maximum:
        return "EXTENDED_COOKING"
    return "MAX_TIME_EXCEEDED"


def process_status(temperature_status: str, thickness_status: str, time_status: str,
                   *, data_gap: bool = False) -> str:
    if thickness_status == "ABOVE_RANGE":
        return "NOT_APPLICABLE"
    if temperature_status in {"LOW_PROCESS_FAILURE", "HIGH_PROCESS_FAILURE"} or \
            time_status in {"EARLY_REMOVAL", "MAX_TIME_EXCEEDED"}:
        return "FAILED"
    if thickness_status == "BELOW_RANGE" or data_gap or \
            temperature_status in {"LOW_WARNING", "HIGH_WARNING"} or \
            time_status in {"SHORT_WARNING", "DELAY_WARNING", "EXTENDED_COOKING"}:
        return "WARNING"
    if temperature_status == "NOT_MEASURED" or thickness_status == "NOT_MEASURED" or \
            time_status == "NOT_MEASURED":
        return "INSUFFICIENT_DATA"
    return "NORMAL"
