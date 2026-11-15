"""Synthetic Jeyuk batch safety hold after the last ingredient addition."""

from dataclasses import dataclass
from decimal import Decimal


RULE_VERSION = "jeyuk-mixture-hold-v1"
MINIMUM_CELSIUS = Decimal("75")
REQUIRED_SECONDS = 60


@dataclass(frozen=True)
class SafetyReading:
    status: str
    hold_seconds: int
    last_addition_second: int | None
    passed_at_second: int | None


@dataclass
class JeyukSafetyMonitor:
    last_second: int = 0
    last_addition_second: int | None = None
    hold_seconds: int = 0
    passed_at_second: int | None = None
    invalid_after_addition: bool = False

    def observe(self, second: int, *, mixture_celsius: Decimal,
                calibration_status: str, safety_sensor_position: str,
                last_ingredient_addition_completed: bool) -> SafetyReading:
        if type(second) is not int or second != self.last_second + 1:
            raise ValueError("one consecutive one-second safety sample is required")
        if not mixture_celsius.is_finite() or mixture_celsius < 0:
            raise ValueError("mixture temperature must be a finite nonnegative value")
        if calibration_status not in ("OK", "EXPIRED") or \
                safety_sensor_position != "LOWEST_MIXTURE_POINT" or \
                type(last_ingredient_addition_completed) is not bool:
            raise ValueError("unsupported safety sensor contract")
        if last_ingredient_addition_completed and self.last_addition_second is not None:
            raise ValueError("the final ingredient addition was already recorded")
        self.last_second = second
        if last_ingredient_addition_completed:
            self.last_addition_second = second
            self.hold_seconds = 0
        elif self.last_addition_second is not None:
            if calibration_status != "OK":
                self.invalid_after_addition = True
                self.hold_seconds = 0
            elif mixture_celsius >= MINIMUM_CELSIUS:
                self.hold_seconds += 1
                if self.hold_seconds >= REQUIRED_SECONDS and self.passed_at_second is None:
                    self.passed_at_second = second
            else:
                self.hold_seconds = 0
        status = ("ASSESSMENT_INCOMPLETE" if self.invalid_after_addition else
                  "PASSED" if self.passed_at_second is not None else "PENDING")
        return SafetyReading(status, self.hold_seconds, self.last_addition_second,
                             self.passed_at_second)
