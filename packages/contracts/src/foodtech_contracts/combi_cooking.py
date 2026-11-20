"""Approved synthetic rice and mackerel combi-oven batch calculations."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

RULE_VERSION = "synthetic-combi-cooking-v1"
Menu = Literal["RICE", "MACKEREL"]
Size = Literal["MEDIUM_FIXED_RACK_10", "LARGE_ROLL_IN_RACK_20"]

TIME_BANDS = {"RICE": (1680, 1740, 1860, 1920),
              "MACKEREL": (660, 690, 750, 780)}


def _mass(value: Decimal, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'nonnegative'} Decimal")
    return value


def cooking_time_status(menu: Menu, seconds: int) -> str:
    if menu not in TIME_BANDS or type(seconds) is not int or seconds < 0:
        raise ValueError("unsupported menu or invalid program duration")
    early, normal_start, normal_end, maximum = TIME_BANDS[menu]
    if seconds < early:
        return "EARLY_TERMINATION"
    if seconds < normal_start:
        return "SHORT_WARNING"
    if seconds <= normal_end:
        return "NORMAL"
    if seconds <= maximum:
        return "DELAY_WARNING"
    return "MAX_TIME_EXCEEDED"


def cooking_durations(started_at: datetime, program_completed_at: datetime,
                      unloaded_at: datetime) -> tuple[int, int]:
    values = (started_at, program_completed_at, unloaded_at)
    if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
           for value in values):
        raise ValueError("all cooking timestamps must include time zones")
    start, complete, unload = (value.astimezone(timezone.utc) for value in values)
    if complete < start or unload < complete:
        raise ValueError("cooking timestamps must be ordered")
    return int((complete - start).total_seconds()), int((unload - start).total_seconds())


@dataclass(frozen=True)
class RiceWater:
    absorbed_and_residual_water_kg: Decimal
    target_effective_water_kg: Decimal
    target_added_water_kg: Decimal | None
    actual_effective_water_kg: Decimal
    effective_water_ratio: Decimal
    status: str


def rice_water(dry_kg: Decimal, drained_kg: Decimal, added_water_kg: Decimal) -> RiceWater:
    dry = _mass(dry_kg, "dry rice", positive=True)
    drained = _mass(drained_kg, "drained rice")
    added = _mass(added_water_kg, "added water")
    if drained < dry:
        raise ValueError("drained rice cannot weigh less than dry rice")
    absorbed = drained - dry
    target = dry * Decimal("1.20")
    remainder = target - absorbed
    effective = absorbed + added
    ratio = effective / dry
    status = "NORMAL" if dry * Decimal("1.15") <= effective <= dry * Decimal("1.25") else "WATER_RATIO_OUT_OF_RANGE"
    return RiceWater(absorbed, target, remainder if remainder >= 0 else None, effective, ratio, status)


@dataclass(frozen=True)
class OvenLoad:
    size: str
    rack_count: int
    pan_count: int
    ingredient_kg: Decimal
    pan_tare_kg: Decimal
    rack_tare_kg: Decimal
    payload_kg: Decimal
    gross_kg: Decimal
    rated_capacity_kg: Decimal
    load_basis: str
    load_ratio: Decimal
    violations: tuple[str, ...]


def oven_load(size: Size, rack_count: int, pan_count: int, ingredient_kg: Decimal,
              pan_tare_kg: Decimal, rack_tare_kg: Decimal) -> OvenLoad:
    if size not in ("MEDIUM_FIXED_RACK_10", "LARGE_ROLL_IN_RACK_20"):
        raise ValueError("unsupported oven profile")
    if type(rack_count) is not int or type(pan_count) is not int or rack_count < 0 or pan_count < 0:
        raise ValueError("rack and pan counts must be nonnegative integers")
    ingredient = _mass(ingredient_kg, "ingredient")
    pans = _mass(pan_tare_kg, "pan tare")
    racks = _mass(rack_tare_kg, "rack tare")
    medium = size == "MEDIUM_FIXED_RACK_10"
    if medium and racks != 0:
        raise ValueError("fixed rack tare is excluded from medium load")
    capacity = Decimal("60" if medium else "160")
    payload = ingredient + pans
    gross = payload + racks
    actual = payload if medium else gross
    violations = []
    if rack_count > (1 if medium else 2):
        violations.append("RACK_COUNT_EXCEEDED")
    if pan_count > (10 if medium else 20) or pan_count > rack_count * 10:
        violations.append("PAN_COUNT_EXCEEDED")
    if actual > capacity:
        violations.append("RATED_LOAD_EXCEEDED")
    return OvenLoad(size, rack_count, pan_count, ingredient, pans, racks, payload, gross, capacity,
                    "PAYLOAD_ONLY" if medium else "GROSS_WITH_REMOVABLE_RACK", actual / capacity,
                    tuple(violations))
