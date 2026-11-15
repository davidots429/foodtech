"""Local bridge from collected synthetic Jeyuk raw samples to process decisions."""

from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample
from foodtech_contracts.jeyuk_process import ProcessMonitor, ProcessReading


class JeyukProcessStream:
    def __init__(self) -> None:
        self.fixture_id: str | None = None
        self.monitor: ProcessMonitor | None = None

    def accept(self, raw: str | bytes) -> ProcessReading:
        sample = decode_jeyuk_mock_sample(raw)
        if self.monitor is None:
            if sample.second != 1:
                raise ValueError("a process stream must begin at second one")
            self.fixture_id = sample.fixture_id
            self.monitor = ProcessMonitor(sample.profile, sample.actual_load_kg)
        elif (sample.fixture_id != self.fixture_id or sample.profile != self.monitor.profile_name
              or sample.actual_load_kg != self.monitor.actual_load_kg):
            raise ValueError("Jeyuk Mock stream identity changed")
        return self.monitor.observe(
            sample.second, surface_celsius=sample.surface_celsius,
            actual_rpm=sample.actual_rpm, target_rpm=sample.target_rpm,
            motor_load_percent=sample.motor_load_percent,
        )
