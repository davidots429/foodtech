"""Synthetic stir-fry process rules for the approved small/medium/large profiles.

Each observed sample represents one complete second. Missing seconds are rejected
so five-second transport averages cannot silently satisfy duration thresholds.
"""

from dataclasses import dataclass, field
from decimal import Decimal

RULE_VERSION = "jeyuk-process-v1"


@dataclass(frozen=True)
class Profile:
    rated_load_kg: Decimal
    recommended_load_kg: Decimal
    surface_low_failure: Decimal
    surface_low_normal: Decimal
    surface_high_normal: Decimal
    surface_high_failure: Decimal
    rpm_low_failure: Decimal
    rpm_low_normal: Decimal
    rpm_high_normal: Decimal
    rpm_high_failure: Decimal
    time_bands: tuple[int, int, int, int, int]


PROFILES = {
    "SMALL": Profile(Decimal("3"), Decimal("1.8"), Decimal("185"), Decimal("195"),
                     Decimal("205"), Decimal("215"), Decimal("22.5"), Decimal("27"),
                     Decimal("33"), Decimal("37.5"), (450, 470, 490, 510, 540)),
    "MEDIUM": Profile(Decimal("20"), Decimal("12"), Decimal("165"), Decimal("175"),
                      Decimal("185"), Decimal("195"), Decimal("15"), Decimal("18"),
                      Decimal("22"), Decimal("25"), (840, 880, 920, 960, 1000)),
    "LARGE": Profile(Decimal("100"), Decimal("60"), Decimal("145"), Decimal("155"),
                     Decimal("165"), Decimal("175"), Decimal("7.5"), Decimal("9"),
                     Decimal("11"), Decimal("12.5"), (1330, 1430, 1530, 1630, 1680)),
}


def load_status(profile_name: str, actual_load_kg: Decimal) -> str:
    profile = PROFILES[profile_name]
    if not actual_load_kg.is_finite() or actual_load_kg <= 0:
        raise ValueError("actual load must be a positive finite mass")
    if actual_load_kg > profile.rated_load_kg:
        return "RATED_LOAD_EXCEEDED"
    if actual_load_kg > profile.recommended_load_kg:
        return "OVER_RECOMMENDED_LOAD"
    return "NORMAL"


def surface_band(profile_name: str, celsius: Decimal) -> str:
    profile = PROFILES[profile_name]
    if not celsius.is_finite():
        raise ValueError("surface temperature must be finite")
    if celsius < profile.surface_low_failure:
        return "LOW_DEVIATION"
    if celsius < profile.surface_low_normal:
        return "LOW_WARNING"
    if celsius <= profile.surface_high_normal:
        return "NORMAL"
    if celsius <= profile.surface_high_failure:
        return "HIGH_WARNING"
    return "HIGH_DEVIATION"


def rpm_band(profile_name: str, actual_rpm: Decimal, target_rpm: Decimal) -> str:
    profile = PROFILES[profile_name]
    if not actual_rpm.is_finite() or not target_rpm.is_finite() or actual_rpm < 0 or target_rpm < 0:
        raise ValueError("RPM values must be nonnegative and finite")
    if target_rpm == 0:
        return "AGITATION_NOT_REQUIRED" if actual_rpm == 0 else "UNCLASSIFIED"
    if actual_rpm < profile.rpm_low_failure:
        return "LOW_DEVIATION"
    if actual_rpm < profile.rpm_low_normal:
        return "LOW_WARNING"
    if actual_rpm <= profile.rpm_high_normal:
        return "NORMAL"
    if actual_rpm <= profile.rpm_high_failure:
        return "HIGH_WARNING"
    return "HIGH_DEVIATION"


def cooking_time_status(profile_name: str, seconds: int) -> str:
    if type(seconds) is not int or seconds < 0:
        raise ValueError("cooking duration must be a nonnegative integer")
    early, short, normal, delay, maximum = PROFILES[profile_name].time_bands
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


@dataclass(frozen=True)
class ProcessReading:
    status: str
    reason_codes: tuple[str, ...]
    surface_band: str
    rpm_band: str
    load_status: str


@dataclass
class ProcessMonitor:
    profile_name: str
    actual_load_kg: Decimal
    last_second: int = 0
    low_surface_consecutive: int = 0
    low_surface_cumulative: int = 0
    high_surface_consecutive: int = 0
    low_rpm_consecutive: int = 0
    low_rpm_cumulative: int = 0
    high_rpm_consecutive: int = 0
    jam_consecutive: int = 0
    recovery_seconds: int = 0
    recovery_pending: bool = False
    active_last_second: bool = False
    failures: set[str] = field(default_factory=set)

    def observe(self, second: int, *, surface_celsius: Decimal, actual_rpm: Decimal,
                target_rpm: Decimal, motor_load_percent: Decimal,
                event: str | None = None) -> ProcessReading:
        if type(second) is not int or second != self.last_second + 1:
            raise ValueError("one consecutive one-second sample is required")
        if not motor_load_percent.is_finite() or motor_load_percent < 0:
            raise ValueError("motor load must be nonnegative and finite")
        if event not in (None, "MOTOR_OVERLOAD_TRIP", "EMERGENCY_STOPPED"):
            raise ValueError("unsupported process event")
        surface = surface_band(self.profile_name, surface_celsius)
        rpm = rpm_band(self.profile_name, actual_rpm, target_rpm)
        load = load_status(self.profile_name, self.actual_load_kg)
        profile = PROFILES[self.profile_name]
        self.last_second = second
        self.low_surface_consecutive = self.low_surface_consecutive + 1 if surface == "LOW_DEVIATION" else 0
        self.low_surface_cumulative += surface == "LOW_DEVIATION"
        self.high_surface_consecutive = self.high_surface_consecutive + 1 if surface == "HIGH_DEVIATION" else 0
        active = target_rpm > 0
        self.low_rpm_consecutive = self.low_rpm_consecutive + 1 if active and rpm == "LOW_DEVIATION" else 0
        self.low_rpm_cumulative += active and rpm == "LOW_DEVIATION"
        self.high_rpm_consecutive = self.high_rpm_consecutive + 1 if active and rpm == "HIGH_DEVIATION" else 0
        jam = active and rpm == "LOW_DEVIATION" and motor_load_percent > 90
        self.jam_consecutive = self.jam_consecutive + 1 if jam else 0
        if not active:
            self.recovery_pending = False
            self.recovery_seconds = 0
        else:
            if not self.active_last_second:
                self.recovery_pending = True
                self.recovery_seconds = 0
            if self.recovery_pending:
                if actual_rpm >= profile.rpm_low_normal:
                    self.recovery_pending = False
                else:
                    self.recovery_seconds += 1
        self.active_last_second = active

        if self.low_surface_consecutive >= 45 or self.low_surface_cumulative >= 180:
            self.failures.add("LOW_SURFACE_TEMPERATURE")
        if self.high_surface_consecutive >= 30:
            self.failures.add("HIGH_SURFACE_TEMPERATURE")
        if self.jam_consecutive >= 30 or event in ("MOTOR_OVERLOAD_TRIP", "EMERGENCY_STOPPED"):
            self.failures.add("AGITATOR_FAILURE")
        if self.low_rpm_consecutive >= 60 or self.low_rpm_cumulative >= 120:
            self.failures.add("LOW_RPM_DURATION")
        if self.high_rpm_consecutive >= 30:
            self.failures.add("HIGH_RPM_DURATION")
        if self.recovery_seconds > 45:
            self.failures.add("RPM_RECOVERY_TIMEOUT")
        if load == "RATED_LOAD_EXCEEDED":
            self.failures.add(load)

        warnings = set()
        if surface in ("LOW_DEVIATION", "LOW_WARNING"):
            warnings.add("LOW_SURFACE_TEMPERATURE_WARNING")
        if surface in ("HIGH_DEVIATION", "HIGH_WARNING"):
            warnings.add("HIGH_SURFACE_TEMPERATURE_WARNING")
        if rpm in ("LOW_DEVIATION", "LOW_WARNING"):
            warnings.add("LOW_RPM_WARNING")
        if rpm in ("HIGH_DEVIATION", "HIGH_WARNING"):
            warnings.add("HIGH_RPM_WARNING")
        if motor_load_percent > 90 and active:
            warnings.add("HIGH_MOTOR_LOAD_WARNING")
        if self.jam_consecutive >= 5:
            warnings.discard("LOW_RPM_WARNING")
            warnings.discard("HIGH_MOTOR_LOAD_WARNING")
            warnings.add("AGITATOR_JAM_WARNING")
        if load == "OVER_RECOMMENDED_LOAD":
            warnings.add(load)
        if rpm == "UNCLASSIFIED":
            warnings.add("UNCLASSIFIED_RPM_WHILE_STOPPED")
        status = "FAILED" if self.failures else "INSUFFICIENT_DATA" if rpm == "UNCLASSIFIED" else \
            "WARNING" if warnings else "NORMAL"
        return ProcessReading(status, tuple(sorted(self.failures | warnings)), surface, rpm, load)
