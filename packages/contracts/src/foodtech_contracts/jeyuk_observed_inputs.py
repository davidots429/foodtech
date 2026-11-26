"""Facts from an already authorized, complete synthetic batch snapshot.

This does not assign an Evidence grade, establish immediately previous history,
certify manual safety/initial-temperature suitability, or authorize a model.
"""
from dataclasses import dataclass, field, asdict
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from .jeyuk_feed_timing import read_feed_timing, _instant
from .jeyuk_mock import decode_jeyuk_mock_sample
from .jeyuk_process import ProcessMonitor, PROFILES
from .jeyuk_safety import JeyukSafetyMonitor, SafetyReading
from .recommendation_output import JeyukBaseline


@dataclass(frozen=True)
class ObservedJeyukBatch:
    job_id: str = field(repr=False)
    profile: str
    heating_setpoint_c: Decimal | None
    target_rpm: Decimal | None
    cooking_seconds: int
    load_kg: Decimal
    vegetable_offset_seconds: int | None
    sauce_offset_seconds: int | None
    sampled_jam_or_overload: bool
    sampled_process_reasons: tuple[str, ...]
    recorded_safety_status: str
    sampled_mixture_safety: SafetyReading | None
    sampled_calibration_all_ok: bool | None
    sampled_final_feed_aligned: bool | None
    feed_mass_matches_load: bool | None
    initial_temperatures_c: tuple[Decimal | None, Decimal | None, Decimal | None]
    unresolved: tuple[str, ...]

    def as_baseline(self):
        """Caller must independently establish which batch is the prior baseline."""
        return JeyukBaseline(self.profile, self.heating_setpoint_c, self.target_rpm, self.cooking_seconds,
            self.load_kg, self.vegetable_offset_seconds, self.sauce_offset_seconds, self.sampled_jam_or_overload)

    def model_context(self):
        # No notes, actor/source IDs, ingredient identity or raw text is copied.
        return {'basis': 'SYNTHETIC_SAMPLES_ONLY', 'profile': self.profile,
            'heating_setpoint_c': str(self.heating_setpoint_c) if self.heating_setpoint_c is not None else None,
            'target_rpm': str(self.target_rpm) if self.target_rpm is not None else None,
            'cooking_seconds': self.cooking_seconds, 'load_kg': str(self.load_kg),
            'vegetable_offset_seconds': self.vegetable_offset_seconds, 'sauce_offset_seconds': self.sauce_offset_seconds,
            'sampled_jam_or_overload': self.sampled_jam_or_overload,
            'sampled_process_reasons': list(self.sampled_process_reasons),
            'recorded_safety_status': self.recorded_safety_status,
            'sampled_mixture_safety': asdict(self.sampled_mixture_safety) if self.sampled_mixture_safety is not None else None,
            'sampled_calibration_all_ok': self.sampled_calibration_all_ok,
            'sampled_final_feed_aligned': self.sampled_final_feed_aligned, 'feed_mass_matches_load': self.feed_mass_matches_load,
            'initial_temperatures_c': {name: str(value) if value is not None else None for name, value in
                zip(('MEAT', 'VEGETABLE', 'SAUCE'), self.initial_temperatures_c, strict=True)},
            'unresolved': list(self.unresolved)}


def observe_jeyuk_batch(job, raws):
    try:
        return _observe(job, raws)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError, OverflowError):
        raise ValueError('synthetic batch observation unavailable') from None


def _observe(job, raws):
    if not isinstance(job, dict) or job.get('menu') != 'JEYUK' or job.get('provenance') != 'SYNTHETIC' or \
            not isinstance(job.get('job_id'), str) or job.get('jeyuk_size_profile') not in PROFILES:
        raise ValueError
    profile = job['jeyuk_size_profile']
    if str(UUID(job['job_id'])) != job['job_id']:
        raise ValueError
    if job.get('equipment_profile') != 'jeyuk-' + profile.lower():
        raise ValueError
    duration = job['actual_cooking_seconds']
    if isinstance(duration, bool) or not isinstance(duration, (int, Decimal)) or not Decimal(duration).is_finite() or \
            duration != int(duration) or not 1 <= duration <= PROFILES[profile].time_bands[-1]:
        raise ValueError
    duration = int(duration)
    if not isinstance(raws, list) or len(raws) != duration:
        raise ValueError
    load = Decimal(str(job['actual_load_kg']))
    if not load.is_finite() or load <= 0:
        raise ValueError
    binding = job['source_binding']
    if not isinstance(binding, dict) or binding.get('source_id') != 'synthetic-rest' or \
            binding.get('device_id') != 'mockprofile-110-a-001' or not isinstance(binding.get('batch_id'), str):
        raise ValueError
    placed, lifted = _instant(job.get('actual_placed_at')), _instant(job.get('actual_lifted_at'))
    if not placed or not lifted or lifted - placed != timedelta(seconds=duration):
        raise ValueError
    samples = []
    for second, row in enumerate(raws, 1):
        if not isinstance(row, dict) or row.get('pk') != 'JEYUK#' + binding['batch_id'] or \
                row.get('sk') != f'RAW#{second:06d}' or row.get('provenance') != 'SYNTHETIC':
            raise ValueError
        sample = decode_jeyuk_mock_sample(row['raw'])
        if sample.fixture_id != binding['batch_id'] or sample.second != second or sample.profile != profile or \
                sample.actual_load_kg != load or sample.observed_at != placed + timedelta(seconds=second):
            raise ValueError
        samples.append(sample)
    unresolved = []
    def setting(name, reason):
        values = [getattr(sample, name) for sample in samples]
        if any(value is None for value in values) or len(set(values)) != 1:
            unresolved.append(reason)
            return None
        return values[0]
    heating = setting('heating_setpoint_celsius', 'HEATING_SETPOINT_NOT_STABLE_OR_MISSING')
    rpm = setting('target_rpm', 'TARGET_RPM_NOT_STABLE_OR_MISSING')
    timing = read_feed_timing(job) or {}
    intervals = []
    for name in ('vegetable_start_offset_us', 'sauce_start_offset_us'):
        value = timing.get(name)
        if timing.get('status') != 'RECORDED' or type(value) is not int or value < 0 or value % 1_000_000:
            intervals.append(None)
            unresolved.append('WHOLE_SECOND_' + name.upper() + '_UNAVAILABLE')
        else:
            intervals.append(value // 1_000_000)
    feeds = job.get('jeyuk_feeds')
    feeds = feeds if isinstance(feeds, dict) and set(feeds) == {'MEAT', 'VEGETABLE', 'SAUCE'} else {}
    initial, masses = [], []
    for name in ('MEAT', 'VEGETABLE', 'SAUCE'):
        feed = feeds.get(name)
        feed = feed if isinstance(feed, dict) else {}
        try:
            mass = Decimal(str(feed['mass_kg']))
            if not mass.is_finite() or not 0 < mass <= 100:
                raise ValueError
            masses.append(mass)
        except (KeyError, ValueError, ArithmeticError):
            pass
        try:
            temperature = Decimal(str(feed['initial_temperature_c']))
            observed, started = _instant(feed.get('temperature_observed_at')), _instant(feed.get('started_at'))
            if not temperature.is_finite() or not Decimal('-273.15') <= temperature <= 300 or \
                    not observed or not started or observed > started:
                raise ValueError
            initial.append(temperature)
        except (KeyError, ValueError, ArithmeticError):
            initial.append(None)
            unresolved.append('INITIAL_TEMPERATURE_RECORD_UNVERIFIED_' + name)
    mass_match = sum(masses, Decimal(0)) == load if len(masses) == 3 else None
    if mass_match is not True:
        unresolved.append('FEED_MASS_MISMATCH' if mass_match is False else 'FEED_MASS_UNAVAILABLE')
    process, reasons, jam = ProcessMonitor(profile, load), set(), False
    safety = JeyukSafetyMonitor()
    safety_present = all(sample.mixture_celsius is not None for sample in samples)
    for sample in samples:
        reading = process.observe(sample.second, surface_celsius=sample.surface_celsius,
            actual_rpm=sample.actual_rpm, target_rpm=sample.target_rpm, motor_load_percent=sample.motor_load_percent)
        reasons.update(reading.reason_codes)
        # Do not lose a sampled overload while the Worker trip discrepancy is
        # tracked separately in #254. This does not rewrite stored verdicts.
        jam = jam or 'AGITATOR_JAM_WARNING' in reading.reason_codes or sample.motor_load_percent > 100
        if safety_present:
            mixture = safety.observe(sample.second, mixture_celsius=sample.mixture_celsius,
                calibration_status=sample.calibration_status, safety_sensor_position=sample.safety_sensor_position,
                last_ingredient_addition_completed=sample.last_ingredient_addition_completed)
    if not safety_present:
        unresolved.append('MIXTURE_SAFETY_SAMPLES_MISSING')
    alignment = None
    if safety_present and timing.get('status') == 'RECORDED':
        ends = [_instant(feeds.get(name, {}).get('completed_at')) for name in ('VEGETABLE', 'SAUCE')]
        if all(ends):
            marker = (placed + timedelta(seconds=mixture.last_addition_second)
                      if mixture.last_addition_second is not None else None)
            alignment = bool(marker and timedelta(0) <= marker - max(ends) <= timedelta(seconds=1))
            if not alignment:
                unresolved.append('SAMPLED_FINAL_FEED_NOT_ALIGNED')
    status = job.get('safety_status')
    if status not in {'PASSED', 'FAILED', 'INCOMPLETE', 'NOT_MEASURED', 'SUSPECTED', 'UNDER_REVIEW'}:
        status = 'NOT_MEASURED'
    return ObservedJeyukBatch(job_id=job['job_id'], profile=profile, heating_setpoint_c=heating, target_rpm=rpm,
        cooking_seconds=duration, load_kg=load, vegetable_offset_seconds=intervals[0], sauce_offset_seconds=intervals[1],
        sampled_jam_or_overload=jam, sampled_process_reasons=tuple(sorted(reasons)), recorded_safety_status=status,
        sampled_mixture_safety=mixture if safety_present else None,
        sampled_calibration_all_ok=all(sample.calibration_status == 'OK' for sample in samples) if safety_present else None,
        sampled_final_feed_aligned=alignment, feed_mass_matches_load=mass_match, initial_temperatures_c=tuple(initial),
        unresolved=tuple(dict.fromkeys(unresolved)))
