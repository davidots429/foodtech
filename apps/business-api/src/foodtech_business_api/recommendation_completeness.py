"""Conservative preliminary checks for synthetic recipe recommendation input.

No quantitative gap threshold is assumed. A passing preliminary check is not
an authorization, a complete input assessment, or a safety decision.
"""
from decimal import Decimal, InvalidOperation
from foodtech_contracts.tonkatsu_process import TIME_BANDS

VERSION = "synthetic-recommendation-completeness-v2"


def _valid_core_series(job: dict) -> bool:
    probes = job.get("core_check")
    batch = job.get("tonkatsu_batch")
    batch = batch if isinstance(batch, dict) else {}
    expected = batch.get("product_count", job.get("planned_portions"))
    if type(expected) is not int or expected < 1 or not isinstance(probes, list) or len(probes) != expected:
        return False
    ids, below = set(), False
    for probe in probes:
        if not isinstance(probe, dict) or probe.get("calibration_status") != "OK":
            return False
        item_id, samples = probe.get("item_id"), probe.get("samples")
        if not isinstance(item_id, str) or not item_id or item_id in ids or not isinstance(samples, list) or len(samples) != 61:
            return False
        ids.add(item_id)
        for second, sample in enumerate(samples):
            if not isinstance(sample, dict) or type(sample.get("at_second")) is not int or sample["at_second"] != second:
                return False
            value = sample.get("celsius")
            if isinstance(value, bool) or value is None:
                return False
            try:
                temperature = Decimal(str(value))
                if not temperature.is_finite() or not Decimal("-273.15") <= temperature <= Decimal("300"):
                    return False
            except (InvalidOperation, ValueError, TypeError):
                return False
            below = below or temperature < 75
    return (job.get("safety_status") == "FAILED" and below) or (job.get("safety_status") == "PASSED" and not below)


def preliminary(job: dict, *, initial_context: dict | None, coverage: dict | None) -> dict:
    if job.get("menu") == "JEYUK" and job.get("provenance") == "SYNTHETIC":
        feeds = job.get("jeyuk_feeds") or {}
        insufficient = []
        partial_temperature = False
        if not isinstance(feeds, dict) or set(feeds) != {"MEAT", "VEGETABLE", "SAUCE"}:
            insufficient.append("JEYUK_FEEDS_MISSING")
        else:
            recorded = sum(isinstance(feed, dict) and feed.get("initial_temperature_c") is not None and
                           bool(feed.get("temperature_observed_at")) for feed in feeds.values())
            if recorded == 0:
                insufficient.append("JEYUK_INITIAL_TEMPERATURE_MISSING")
            partial_temperature = 0 < recorded < len(feeds)
        if job.get("jeyuk_feed_mass_status") != "MATCHED":
            insufficient.append("JEYUK_FEED_MASS_UNVERIFIED")
        if job.get("jeyuk_feed_safety_aligned") is not True or job.get("safety_status") != "PASSED":
            insufficient.append("JEYUK_FEED_SAFETY_UNVERIFIED")
        if type(job.get("actual_cooking_seconds")) is not int or job["actual_cooking_seconds"] <= 0:
            insufficient.append("COOKING_DURATION_MISSING")
        unknown = ["SOURCE_COMPLETENESS_NOT_ASSESSED", "JEYUK_INITIAL_TEMPERATURE_SUITABILITY_NOT_ASSESSED"]
        if partial_temperature:
            unknown.append("JEYUK_INITIAL_TEMPERATURE_PARTIAL")
        if coverage and coverage.get("internal_gap_detected") is True:
            unknown.append("GAP_SEVERITY_NOT_ASSESSED")
        return {"status": "INSUFFICIENT" if insufficient else "NOT_ASSESSED",
                "reasons": insufficient + unknown, "rule_version": VERSION}
    if job.get("menu") != "TONKATSU" or job.get("provenance") != "SYNTHETIC":
        return {"status": "NOT_ASSESSED", "reasons": ["MENU_NOT_ASSESSED"], "rule_version": VERSION}
    insufficient = []
    if job.get("safety_status") not in {"PASSED", "FAILED"}:
        insufficient.append("SAFETY_NOT_MEASURED_OR_INCOMPLETE")
    elif not _valid_core_series(job):
        insufficient.append("CORE_SERIES_MISSING_INVALID_OR_INCONSISTENT")
    if not isinstance(job.get("thickness_profile"), str) or job["thickness_profile"] not in TIME_BANDS:
        insufficient.append("THICKNESS_PROFILE_MISSING")
    if type(job.get("actual_cooking_seconds")) is not int or job["actual_cooking_seconds"] <= 0:
        insufficient.append("COOKING_DURATION_MISSING")
    if job.get("equipment_profile") == "synthetic-fryer-approved-v2" and not job.get("tonkatsu_batch"):
        insufficient.append("BATCH_INPUT_MISSING")
    if not initial_context or initial_context.get("ingredient_temperature_c") is None or \
            not initial_context.get("temperature_observed_at"):
        insufficient.append("INITIAL_TEMPERATURE_MISSING")
    unknown = ["SOURCE_COMPLETENESS_NOT_ASSESSED"]
    if coverage and coverage.get("internal_gap_detected") is True:
        unknown.append("GAP_SEVERITY_NOT_ASSESSED")
    return {"status": "INSUFFICIENT" if insufficient else "NOT_ASSESSED",
            "reasons": insufficient + unknown, "rule_version": VERSION}
