"""Deterministic limits for synthetic proposal values; this does not approve a recipe."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .jeyuk_process import PROFILES
from .tonkatsu_process import TIME_BANDS

RULE_VERSION = "synthetic-recommendation-guard-v1"


@dataclass(frozen=True)
class Evidence:
    completeness: str
    safety_status: str
    critical_data_valid: bool | None
    severe_data_gap: bool | None


@dataclass(frozen=True)
class TonkatsuProposal:
    profile: str
    temperature_c: Decimal | None
    duration_seconds: int | None
    previous_duration_seconds: int | None


@dataclass(frozen=True)
class JeyukProposal:
    profile: str
    temperature_c: Decimal | None
    rpm: Decimal | None
    duration_seconds: int | None
    load_kg: Decimal | None
    vegetable_interval_seconds: int | None
    sauce_interval_seconds: int | None
    previous_temperature_c: Decimal | None
    previous_rpm: Decimal | None
    previous_duration_seconds: int | None
    previous_load_kg: Decimal | None
    previous_vegetable_interval_seconds: int | None
    previous_sauce_interval_seconds: int | None
    previous_jam_or_overload: bool | None


@dataclass(frozen=True)
class GuardResult:
    status: Literal["INDETERMINATE", "REJECTED", "WITHIN_ENCODED_LIMITS"]
    reasons: tuple[str, ...]
    rule_version: str = RULE_VERSION


def _finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _seconds(value: object, *, allow_zero: bool = False) -> bool:
    return type(value) is int and value >= (0 if allow_zero else 1)


def _evidence(evidence: Evidence) -> list[str]:
    reasons = []
    if evidence.completeness not in {"COMPLETE", "PARTIAL"}:
        reasons.append("DATA_COMPLETENESS_INSUFFICIENT")
    if evidence.safety_status not in {"PASSED", "FAILED"}:
        reasons.append("SAFETY_STATUS_UNAVAILABLE")
    if evidence.critical_data_valid is not True:
        reasons.append("CRITICAL_DATA_UNAVAILABLE")
    if evidence.severe_data_gap is not False:
        reasons.append("SEVERE_DATA_GAP_OR_UNKNOWN")
    return reasons


def tonkatsu(evidence: Evidence, proposal: TonkatsuProposal) -> GuardResult:
    unknown = _evidence(evidence)
    if not isinstance(proposal.profile, str) or proposal.profile not in TIME_BANDS:
        unknown.append("THICKNESS_PROFILE_UNAVAILABLE")
    if not _finite(proposal.temperature_c) or not _seconds(proposal.duration_seconds):
        unknown.append("PROPOSAL_VALUE_INVALID")
    if evidence.safety_status == "FAILED" and not _seconds(proposal.previous_duration_seconds):
        unknown.append("PREVIOUS_DURATION_UNAVAILABLE")
    if unknown:
        return GuardResult("INDETERMINATE", tuple(unknown))
    reasons = []
    earliest, *_, maximum = TIME_BANDS[proposal.profile]
    if not Decimal("165") <= proposal.temperature_c <= Decimal("175"):
        reasons.append("TEMPERATURE_OUTSIDE_TARGET")
    if not earliest <= proposal.duration_seconds <= maximum:
        reasons.append("DURATION_OUTSIDE_PROCESS_LIMITS")
    if evidence.safety_status == "FAILED" and proposal.duration_seconds < proposal.previous_duration_seconds:
        reasons.append("SHORTENING_AFTER_SAFETY_FAILURE")
    return GuardResult("REJECTED" if reasons else "WITHIN_ENCODED_LIMITS", tuple(reasons))


def jeyuk(evidence: Evidence, proposal: JeyukProposal) -> GuardResult:
    unknown = _evidence(evidence)
    profile = PROFILES.get(proposal.profile) if isinstance(proposal.profile, str) else None
    if profile is None:
        unknown.append("EQUIPMENT_PROFILE_UNAVAILABLE")
    if not all(_finite(value) for value in (proposal.temperature_c, proposal.rpm, proposal.load_kg)) or \
            not _seconds(proposal.duration_seconds) or \
            not _seconds(proposal.vegetable_interval_seconds, allow_zero=True) or \
            not _seconds(proposal.sauce_interval_seconds, allow_zero=True):
        unknown.append("PROPOSAL_VALUE_INVALID")
    if type(proposal.previous_jam_or_overload) is not bool:
        unknown.append("PREVIOUS_JAM_STATUS_UNAVAILABLE")
    if evidence.safety_status == "FAILED" and (not _seconds(proposal.previous_duration_seconds) or
            not _finite(proposal.previous_temperature_c) or proposal.previous_temperature_c <= 0):
        unknown.append("PREVIOUS_SAFETY_BASELINE_UNAVAILABLE")
    if proposal.previous_jam_or_overload is True and (
            not _finite(proposal.previous_rpm) or proposal.previous_rpm < 0 or
            not _finite(proposal.previous_load_kg) or proposal.previous_load_kg <= 0 or
            not _seconds(proposal.previous_vegetable_interval_seconds, allow_zero=True) or
            not _seconds(proposal.previous_sauce_interval_seconds, allow_zero=True)):
        unknown.append("PREVIOUS_JAM_BASELINE_UNAVAILABLE")
    if unknown:
        return GuardResult("INDETERMINATE", tuple(unknown))
    reasons = []
    if not profile.surface_low_normal <= proposal.temperature_c <= profile.surface_high_normal:
        reasons.append("TEMPERATURE_OUTSIDE_TARGET")
    if not profile.rpm_low_normal <= proposal.rpm <= profile.rpm_high_normal:
        reasons.append("RPM_OUTSIDE_TARGET")
    if not 0 < proposal.load_kg <= profile.rated_load_kg:
        reasons.append("LOAD_OUTSIDE_RATING")
    if not profile.time_bands[0] <= proposal.duration_seconds <= profile.time_bands[-1]:
        reasons.append("DURATION_OUTSIDE_PROCESS_LIMITS")
    if evidence.safety_status == "FAILED":
        if proposal.duration_seconds < proposal.previous_duration_seconds:
            reasons.append("SHORTENING_AFTER_SAFETY_FAILURE")
        if proposal.temperature_c < proposal.previous_temperature_c:
            reasons.append("LOWERING_TEMPERATURE_AFTER_SAFETY_FAILURE")
    if proposal.previous_jam_or_overload:
        if proposal.load_kg > proposal.previous_load_kg: reasons.append("INCREASED_LOAD_AFTER_JAM")
        if proposal.rpm > proposal.previous_rpm: reasons.append("INCREASED_RPM_AFTER_JAM")
        if proposal.vegetable_interval_seconds < proposal.previous_vegetable_interval_seconds:
            reasons.append("SHORTENED_VEGETABLE_INTERVAL_AFTER_JAM")
        if proposal.sauce_interval_seconds < proposal.previous_sauce_interval_seconds:
            reasons.append("SHORTENED_SAUCE_INTERVAL_AFTER_JAM")
    return GuardResult("REJECTED" if reasons else "WITHIN_ENCODED_LIMITS", tuple(reasons))
