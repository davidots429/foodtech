"""Find recorded fryer temperature samples preceding synthetic food placement."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

VERSION = "tonkatsu-preplacement-sample-v1"


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.utcoffset() is not None else None
    except ValueError:
        return None


def observe(assessments: list[dict], job: dict) -> dict:
    """Return the latest eligible recorded point, not an inferred placement temperature."""
    placed = _instant(job.get("actual_placed_at"))
    binding = job.get("source_binding")
    empty = {"version": VERSION, "status": "NOT_CAPTURED"}
    if placed is None or not isinstance(binding, dict):
        return empty
    candidates = []
    for item in assessments:
        normalized = item.get("normalized")
        if item.get("dedup_status") != "UNIQUE" or item.get("quality_status") not in {"VALID", "WARNING"} or \
                not isinstance(normalized, dict) or normalized.get("device_status") != "RUNNING" or \
                any(normalized.get(key) != value for key, value in binding.items()):
            continue
        sample_at = _instant(normalized.get("event_time"))
        if sample_at is None or sample_at >= placed:
            continue
        try:
            reading = normalized.get("CHAMBER_TEMPERATURE")
            if not isinstance(reading, dict) or reading.get("unit") != "°C":
                continue
            value = Decimal(reading["value"])
            sequence = normalized["sequence_number"]
            if not value.is_finite() or isinstance(sequence, bool) or not isinstance(sequence, (int, Decimal)) or \
                    (isinstance(sequence, Decimal) and not sequence.is_finite()) or sequence < 1 or \
                    sequence > 10**18 or int(sequence) != sequence:
                continue
            sequence = int(sequence)
        except (KeyError, InvalidOperation, TypeError, ValueError):
            continue
        candidates.append((sample_at, value, item["quality_status"], sequence))
    if not candidates:
        return empty
    latest_time = max(candidate[0] for candidate in candidates)
    latest = [candidate for candidate in candidates if candidate[0] == latest_time]
    if len(latest) != 1:
        return {"version": VERSION, "status": "AMBIGUOUS", "sample_at": latest_time.isoformat()}
    sample_at, value, quality, sequence = latest[0]
    age = placed - sample_at
    return {"version": VERSION, "status": "OBSERVED", "sample_at": sample_at.isoformat(),
            "sample_celsius": str(value), "quality_status": quality, "sequence_number": sequence,
            "age_before_placement_us": (age.days * 86400 + age.seconds) * 1_000_000 + age.microseconds}
