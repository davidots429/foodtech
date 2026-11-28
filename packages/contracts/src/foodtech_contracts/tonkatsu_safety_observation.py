"""Verify recorded core measurements and separately observed additional heat."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID


MAX_SAFETY_EVENTS = 64


def recorded_number(value, minimum, maximum):
    if type(value) not in (str, int, Decimal):
        raise ValueError('recorded number required')
    result = Decimal(value)
    if not result.is_finite() or not minimum <= result <= maximum:
        raise ValueError('recorded number out of bounds')
    return result


def recorded_integer(value, minimum, maximum):
    result = recorded_number(value, minimum, maximum)
    if type(value) is str or result != int(result):
        raise ValueError('recorded integer required')
    return int(result)


def recorded_instant(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError('recorded timestamp required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() != timedelta(0):
        raise ValueError('recorded UTC timestamp required')
    return result


@dataclass(frozen=True)
class TonkatsuCoreSummary:
    probe_count: int
    minimum_celsius: Decimal
    probes_below_limit: int

    def model_context(self):
        return {
            "probe_count": self.probe_count, "minimum_celsius": str(self.minimum_celsius),
            "probes_below_limit": self.probes_below_limit,
            "observed_span_seconds": 60, "samples_per_probe": 61,
        }


def summarize_tonkatsu_core(probes, count, lifted, safety_status):
    """Summarize each calibrated series before checking the stored decision."""
    try:
        if type(probes) is not list or len(probes) != count:
            return None
        ids, probe_ids, removals = set(), set(), set()
        minima = []
        for probe in probes:
            item = probe['item_id']
            if type(item) is not str or not 1 <= len(item) <= 80 or item in ids or probe.get('calibration_status') != 'OK':
                return None
            probe_id = probe.get('probe_id')
            removal = recorded_integer(probe.get('removal_sequence'), 1, count)
            if type(probe_id) is not str or not 1 <= len(probe_id) <= 80 or probe_id in probe_ids or removal in removals or \
                    recorded_instant(probe.get('measurement_start')) != lifted or \
                    recorded_instant(probe.get('measurement_end')) != lifted + timedelta(seconds=60):
                return None
            ids.add(item); probe_ids.add(probe_id); removals.add(removal)
            samples = probe['samples']
            if type(samples) is not list or len(samples) != 61:
                return None
            temperatures = []
            for second, sample in enumerate(samples):
                if recorded_integer(sample['at_second'], 0, 60) != second:
                    return None
                temperatures.append(recorded_number(sample['celsius'], Decimal('-273.15'), Decimal(300)))
            minima.append(min(temperatures))
        below = sum(temperature < 75 for temperature in minima)
        if safety_status != ('FAILED' if below else 'PASSED'):
            return None
        return TonkatsuCoreSummary(count, min(minima), below)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None



@dataclass(frozen=True)
class TonkatsuReheatSegment:
    duration_seconds: Decimal
    incomplete_rechecks: int
    safety_status: str
    core: TonkatsuCoreSummary

    def model_context(self):
        return {
            "duration_seconds": str(self.duration_seconds),
            "incomplete_rechecks": self.incomplete_rechecks,
            "safety_status": self.safety_status,
            "core_observation": self.core.model_context(),
        }


@dataclass(frozen=True)
class TonkatsuReheatHistory:
    initial_core: TonkatsuCoreSummary
    segments: tuple[TonkatsuReheatSegment, ...]

    def model_context(self):
        return {
            "initial_safety_status": "FAILED",
            "initial_core_observation": self.initial_core.model_context(),
            "additional_heat_segments": [segment.model_context() for segment in self.segments],
        }


def _reheat_duration(segment, *, after):
    started, lifted = recorded_instant(segment["started_at"]), recorded_instant(segment["lifted_at"])
    delta = lifted - started
    microseconds = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    duration = Decimal(microseconds) / 1000000
    if started < after or duration <= 0 or duration != recorded_number(segment["duration_seconds"], 0, 86400):
        raise ValueError("inconsistent additional-heat duration")
    return lifted, duration


def observe_tonkatsu_reheat(job, events, count):
    """Validate the immutable chain from the original failed check to current safety."""
    if job.get("pending_tonkatsu_reheat") is not None or not isinstance(job.get("latest_tonkatsu_reheat"), dict):
        raise ValueError("resolved additional-heat history required")
    safety_version = recorded_integer(job["safety_version"], 1, 10**18)
    if safety_version != recorded_integer(job["core_check_safety_version"], 1, 10**18):
        raise ValueError("current safety revision changed")
    if type(events) is not list or not 1 <= len(events) <= MAX_SAFETY_EVENTS:
        raise ValueError("bounded safety history required")
    indexed = {}
    for event in events:
        key = event["sk"]
        if type(key) is not str or not key.startswith("EVENT#SAFETY#") or not key[13:].isascii() or not key[13:].isdecimal():
            raise ValueError("invalid safety event key")
        version = int(key[13:])
        if (
            key != f"EVENT#SAFETY#{version:012d}" or not 1 <= version <= safety_version
            or event.get("pk") != job["pk"] or event.get("domain") != "SAFETY_STATUS" or version in indexed
        ):
            raise ValueError("safety event outside the current job")
        indexed[version] = event

    first = min((version for version, row in indexed.items() if row.get("trigger") == "TONKATSU_REHEATED"), default=0)
    initial = indexed.get(first - 1)
    lifted = recorded_instant(job["actual_lifted_at"])
    if (
        not initial or initial.get("trigger") != "CORE_CHECK" or initial.get("reheat_segment_id") is not None
        or initial.get("to_status") != "FAILED" or safety_version - first + 2 > MAX_SAFETY_EVENTS
    ):
        raise ValueError("original failed core assessment unavailable")
    core = summarize_tonkatsu_core(initial["probes"], count, lifted, "FAILED")
    if core is None:
        raise ValueError("original core measurements are inconsistent")
    previous_event_time = recorded_instant(initial["occurred_at"])
    if previous_event_time < lifted + timedelta(seconds=60):
        raise ValueError("original assessment predates its samples")
    previous_safety = "FAILED"
    previous_probes = initial["probes"]
    initial_core = core
    latest_segment = None
    active = None
    seen_ids = set()
    segments = []
    for version in range(first, safety_version + 1):
        event = indexed[version]
        occurred = recorded_instant(event["occurred_at"])
        if occurred < previous_event_time or event.get("from_status") != previous_safety:
            raise ValueError("safety event order or transition changed")
        previous_event_time = occurred
        status = event.get("to_status")
        if event.get("trigger") == "TONKATSU_REHEATED":
            if active is not None or previous_safety != "FAILED" or status != "INCOMPLETE":
                raise ValueError("additional heat requires a completed failed assessment")
            segment = event["segment"]
            segment_id = segment["segment_id"]
            item_ids = segment["item_ids"]
            if (
                type(segment_id) is not str or str(UUID(segment_id)) != segment_id or segment_id in seen_ids
                or type(item_ids) is not list or len(item_ids) != count or set(item_ids) != {p["item_id"] for p in previous_probes}
                or recorded_integer(segment["previous_safety_version"], 1, 10**18) != version - 1
            ):
                raise ValueError("additional-heat segment identity changed")
            seen_ids.add(segment_id)
            lifted, duration = _reheat_duration(segment, after=lifted + timedelta(seconds=60))
            if occurred < lifted:
                raise ValueError("additional heat is recorded before removal")
            latest_segment = segment
            active = (segment_id, set(item_ids), duration)
            incomplete_checks = 0
        else:
            if (
                event.get("trigger") != "CORE_CHECK" or active is None
                or event.get("reheat_segment_id") != active[0] or status not in {"PASSED", "FAILED", "INCOMPLETE"}
                or occurred < lifted + timedelta(seconds=60)
            ):
                raise ValueError("unbound additional-heat core check")
            probes = event.get("probes")
            if type(probes) is not list or len(probes) != count or {probe["item_id"] for probe in probes} != active[1]:
                raise ValueError("additional-heat products changed")
            if status == "INCOMPLETE":
                incomplete_checks += 1
            else:
                core = summarize_tonkatsu_core(probes, count, lifted, status)
                if core is None:
                    raise ValueError("invalid additional-heat core measurements")
                segments.append(TonkatsuReheatSegment(active[2], incomplete_checks, status, core))
                active = None
                previous_probes = probes
        previous_safety = status
    if (
        not segments or active is not None or latest_segment != job["latest_tonkatsu_reheat"]
        or previous_probes != job.get("core_check") or previous_safety != job.get("safety_status")
    ):
        raise ValueError("current safety state does not match additional-heat history")
    return TonkatsuReheatHistory(initial_core, tuple(segments))
