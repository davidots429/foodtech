"""Fail-closed parsing and encoded-limit checking for synthetic recipe proposals.

The caller must supply authoritative evidence and baselines. This module neither
invokes a model nor approves, persists, or applies a recipe.
"""
import json
from dataclasses import dataclass
from decimal import Decimal

from .combi_recommendation import CombiBaseline, SCHEMA_VERSION as COMBI_SCHEMA_VERSION, proposal as combi_proposal
from .recommendation_guard import Evidence, JeyukProposal, TonkatsuProposal, jeyuk, tonkatsu

SCHEMA_VERSION = "synthetic-recommendation-output-v1"
MAX_OUTPUT_BYTES = 16_384


@dataclass(frozen=True)
class TonkatsuBaseline:
    profile: str
    previous_duration_seconds: int | None


@dataclass(frozen=True)
class JeyukBaseline:
    profile: str
    previous_temperature_c: Decimal | None
    previous_rpm: Decimal | None
    previous_duration_seconds: int | None
    previous_load_kg: Decimal | None
    previous_vegetable_interval_seconds: int | None
    previous_sauce_interval_seconds: int | None
    previous_jam_or_overload: bool | None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def _number(value):
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("numeric value required")
    return value


def _integer(value):
    if type(value) is not int:
        raise ValueError("integer value required")
    return value


def validate(raw: str, evidence: Evidence, baseline: TonkatsuBaseline | JeyukBaseline | CombiBaseline) -> dict:
    """Return an empty data object on invalid, indeterminate, or rejected output."""
    schema_version = COMBI_SCHEMA_VERSION if isinstance(baseline, CombiBaseline) else SCHEMA_VERSION
    try:
        if not isinstance(evidence, Evidence) or not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("invalid output size")
        value = json.loads(raw, parse_float=Decimal, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        if not isinstance(value, dict):
            raise ValueError("object required")
        if isinstance(baseline, TonkatsuBaseline):
            if set(value) != {"temperature_c", "duration_seconds"}:
                raise ValueError("unexpected fields")
            temperature, duration = _number(value["temperature_c"]), _integer(value["duration_seconds"])
            result = tonkatsu(evidence, TonkatsuProposal(baseline.profile, temperature, duration,
                                                         baseline.previous_duration_seconds))
            candidate = {"temperature_c": str(temperature), "duration_seconds": duration}
        elif isinstance(baseline, JeyukBaseline):
            if set(value) != {"temperature_c", "rpm", "duration_seconds", "load_kg",
                              "vegetable_interval_seconds", "sauce_interval_seconds"}:
                raise ValueError("unexpected fields")
            temperature, rpm, load = (_number(value[name]) for name in ("temperature_c", "rpm", "load_kg"))
            duration, vegetable, sauce = (_integer(value[name]) for name in
                ("duration_seconds", "vegetable_interval_seconds", "sauce_interval_seconds"))
            result = jeyuk(evidence, JeyukProposal(baseline.profile, temperature, rpm, duration, load,
                vegetable, sauce, baseline.previous_temperature_c, baseline.previous_rpm,
                baseline.previous_duration_seconds, baseline.previous_load_kg,
                baseline.previous_vegetable_interval_seconds, baseline.previous_sauce_interval_seconds,
                baseline.previous_jam_or_overload))
            candidate = {"temperature_c": str(temperature), "rpm": str(rpm), "duration_seconds": duration,
                         "load_kg": str(load), "vegetable_interval_seconds": vegetable,
                         "sauce_interval_seconds": sauce}
        elif isinstance(baseline, CombiBaseline):
            result, candidate = combi_proposal(value, evidence, baseline)
        else:
            raise ValueError("unknown authoritative baseline")
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        return {"status": "FAILED", "error_code": "STRUCTURED_OUTPUT_INVALID", "data": {},
                "schema_version": schema_version, "fallback_used": False}
    if result.status != "WITHIN_ENCODED_LIMITS":
        return {"status": result.status, "error_code": "LIMIT_CHECK_FAILED", "data": {},
                "reasons": list(result.reasons), "schema_version": schema_version, "fallback_used": False}
    return {"status": "NEEDS_REVIEW", "error_code": None, "data": candidate,
            "schema_version": schema_version, "guard_version": result.rule_version, "fallback_used": False}
