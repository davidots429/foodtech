"""Read-only elapsed-time guidance for approved synthetic cooking profiles."""
from datetime import datetime, timezone

from .jeyuk_process import PROFILES as JEYUK_PROFILES, RULE_VERSION as JEYUK_RULE_VERSION
from .tonkatsu_process import TIME_BANDS, RULE_VERSION as TONKATSU_RULE_VERSION


def observe(job: dict, now: datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("observation time must include a timezone")
    result = {"status": "NOT_ACTIVE", "elapsed_seconds": None, "rule_version": None,
              "recommended_limit_seconds": None, "maximum_seconds": None,
              "observed_at": now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}
    if job.get("status") not in {"RUNNING", "PAUSED", "SAFETY_HOLD"} or job.get("actual_lifted_at"):
        return result
    raw = job.get("actual_placed_at")
    if not isinstance(raw, str):
        return {**result, "status": "NOT_MEASURED"}
    try:
        placed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if placed.tzinfo is None or placed.utcoffset() is None:
            raise ValueError("missing timezone")
    except ValueError:
        return {**result, "status": "NOT_MEASURED"}
    seconds = (now - placed).total_seconds()
    if seconds < 0:
        return {**result, "status": "CLOCK_MISMATCH"}
    if job.get("menu") == "TONKATSU":
        bands = TIME_BANDS.get(job.get("thickness_profile"))
        rule = TONKATSU_RULE_VERSION
    elif job.get("menu") == "JEYUK":
        profile = JEYUK_PROFILES.get(job.get("jeyuk_size_profile"))
        bands = profile.time_bands if profile else None
        rule = JEYUK_RULE_VERSION
    else:
        bands = None
        rule = None
    elapsed = int(seconds)
    if not bands:
        return {**result, "status": "NOT_APPLICABLE", "elapsed_seconds": elapsed, "rule_version": rule}
    _, _, normal_max, recommended, maximum = bands
    status = ('MAX_TIME_EXCEEDED' if elapsed > maximum else
              'EXTENDED_COOKING' if elapsed > recommended else
              'DELAY_WARNING' if elapsed > normal_max else 'MONITORING')
    return {**result, "status": status, "elapsed_seconds": elapsed, "rule_version": rule,
            "recommended_limit_seconds": recommended, "maximum_seconds": maximum}
