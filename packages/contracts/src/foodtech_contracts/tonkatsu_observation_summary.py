"""Summarize observed synthetic fryer samples without interpolating temperatures."""
from datetime import datetime, timedelta
from decimal import Decimal

VERSION = "tonkatsu-observed-temperature-v1"


def summarize(ordered: list[dict]) -> dict:
    """Summarize point samples already validated and ordered by the replay.

    Durations describe spans between adjacent qualifying samples, not proof of
    temperature throughout those spans. Gaps never contribute to a span.
    """
    if not ordered:
        raise ValueError("at least one observed sample is required")
    previous = None
    low_span = high_span = longest_high = current_high = 0
    temperatures = []
    warning_count = 0
    recovery_start = None
    recovery_onset_bracketed = False
    last_sampled_recovery = None
    for item in ordered:
        normalized = item["normalized"]
        at = datetime.fromisoformat(normalized["event_time"].replace("Z", "+00:00"))
        temperature = Decimal(normalized["CHAMBER_TEMPERATURE"]["value"])
        if at.utcoffset() is None or not temperature.is_finite():
            raise ValueError("sample must have an aware timestamp and finite temperature")
        sequence = int(normalized["sequence_number"])
        warning_count += item.get("quality_status") == "WARNING"
        temperatures.append(temperature)
        adjacent = False
        if previous:
            last_at, last_temperature, last_sequence = previous
            delta = at - last_at
            adjacent = sequence == last_sequence + 1 and timedelta(0) < delta <= timedelta(seconds=5)
            micros = ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) if adjacent else 0
            if adjacent and last_temperature < 160 and temperature < 160:
                low_span += micros
            if adjacent and last_temperature > 180 and temperature > 180:
                current_high += micros
                high_span += micros
                longest_high = max(longest_high, current_high)
            else:
                current_high = 0
        if temperature < 165:
            if recovery_start is None or not adjacent:
                recovery_start = at
                recovery_onset_bracketed = bool(previous and adjacent and previous[1] >= 165)
        elif recovery_start is not None:
            if adjacent and temperature <= 175:
                duration = at - recovery_start
                last_sampled_recovery = {"first_below_at": recovery_start.isoformat(), "first_in_range_at": at.isoformat(),
                    "span_us": (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds,
                    "onset_bracketed_by_samples": recovery_onset_bracketed}
            recovery_start = None
        previous = (at, temperature, sequence)
    return {"version": VERSION, "basis": "POINT_SAMPLES_ONLY", "scope": "COOKING_INTERVAL_ONLY",
            "sample_count": len(temperatures), "warning_sample_count": warning_count,
            "first_sample_celsius": str(temperatures[0]), "minimum_sample_celsius": str(min(temperatures)),
            "maximum_sample_celsius": str(max(temperatures)), "last_sample_celsius": str(temperatures[-1]),
            "below_160_between_qualifying_samples_us": low_span,
            "above_180_between_qualifying_samples_us": high_span,
            "longest_above_180_sample_span_us": longest_high,
            "last_sampled_recovery": last_sampled_recovery}
