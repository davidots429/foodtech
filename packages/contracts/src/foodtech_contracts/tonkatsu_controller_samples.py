"""Validate explicit synthetic 1Hz controller samples; never infer them from five-second envelopes.

Envelope transport is every five seconds. Temperature spans describe adjacent
recorded one-second controller points. They are not physical sensor attestation.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


def _integer(value):
    if type(value) not in (int, Decimal) or not Decimal(value).is_finite() or value != int(value):
        raise ValueError('explicit controller integer required')
    return int(value)


def _instant(value):
    if type(value) is not str or len(value) > 40: raise ValueError('controller UTC time required')
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.utcoffset() != timedelta(0): raise ValueError('controller UTC time required')
    return at


@dataclass(frozen=True)
class TonkatsuControllerSummary:
    sample_count: int
    calibration_all_ok: bool
    minimum_sample_c: Decimal
    maximum_sample_c: Decimal
    preplacement_sample_c: Decimal | None
    preplacement_age_us: int | None
    preplacement_calibration_ok: bool | None
    below_160_sample_span_seconds: int
    above_180_sample_span_seconds: int
    longest_above_180_sample_span_seconds: int
    # start, first return >=165, stable 165..175 confirmation offsets in us.
    recoveries: tuple[tuple[int, int | None, int | None], ...]

    def model_context(self):
        return {'schema_version': 'tonkatsu-controller-observation-v1', 'basis': 'EXPLICIT_SYNTHETIC_1HZ_SAMPLES',
            'sample_count': self.sample_count, 'calibration_all_ok': self.calibration_all_ok,
            'minimum_sample_c': str(self.minimum_sample_c), 'maximum_sample_c': str(self.maximum_sample_c),
            'preplacement_sample_c': str(self.preplacement_sample_c) if self.preplacement_sample_c is not None else None,
            'preplacement_age_us': self.preplacement_age_us,
            'preplacement_calibration_ok': self.preplacement_calibration_ok,
            'below_160_sample_span_seconds': self.below_160_sample_span_seconds,
            'above_180_sample_span_seconds': self.above_180_sample_span_seconds,
            'longest_above_180_sample_span_seconds': self.longest_above_180_sample_span_seconds,
            'recovery_episodes': [{'first_below_offset_us': start, 'first_return_offset_us': entry,
                                   'hold_confirmed_offset_us': confirmed}
                                  for start,entry,confirmed in self.recoveries]}


def observe_controller(records, *, placed, lifted):
    """None means absent controller samples, never a default success.

    A partially present, changed or malformed nested contract is rejected.
    All envelopes must explicitly retain five consecutive 1Hz points, and the
    final point must agree with the corresponding ordinary REST measurement.
    """
    payloads = [row.get('extensions', {}).get('tonkatsuSamples') for row in records]
    if all(value is None for value in payloads): return None
    points = []
    for sequence,(record,payload) in enumerate(zip(records,payloads),1):
        if type(payload) is not dict or set(payload) != {'schemaVersion','sampleIntervalSeconds','samples'} or \
                payload['schemaVersion'] != 'tonkatsu-controller-samples-v1' or \
                _integer(payload['sampleIntervalSeconds']) != 1 or _integer(record['sequenceNumber']) != sequence or \
                type(payload['samples']) is not list or len(payload['samples']) != 5:
            raise ValueError('complete explicit controller envelopes required')
        envelope_time = _instant(record['eventTime'])
        for offset,sample in enumerate(payload['samples']):
            if type(sample) is not dict or set(sample) != {'second','observedAt','oilTemperatureCelsius','calibrationStatus'} or \
                    _integer(sample['second']) != (sequence-1)*5+offset+1 or \
                    sample['calibrationStatus'] not in {'OK','EXPIRED'} or \
                    type(sample['oilTemperatureCelsius']) not in (int,Decimal):
                raise ValueError('invalid controller point')
            at = _instant(sample['observedAt']); value = Decimal(sample['oilTemperatureCelsius'])
            if not value.is_finite() or not Decimal('-273.15') <= value <= 300 or \
                    at != envelope_time-timedelta(seconds=4-offset) or \
                    (points and at-points[-1][0] != timedelta(seconds=1)):
                raise ValueError('controller point gap or inconsistent time')
            points.append((at,value,sample['calibrationStatus']))
        if points[-1][1] != Decimal(str(record['measurements']['temperatureCelsius'])):
            raise ValueError('controller and envelope temperature mismatch')
    if not points or points[0][0] > placed or points[-1][0] < lifted:
        raise ValueError('controller samples do not bracket cooking')
    cooking = [point for point in points if placed <= point[0] <= lifted]
    if len(cooking) < 2: raise ValueError('controller cooking samples unavailable')
    low = high = longest = high_run = 0
    episodes = []; episode = None; normal_since = None
    def micros(at):
        delta = at-placed
        return (delta.days*86400+delta.seconds)*1_000_000+delta.microseconds
    previous = None
    for at,value,calibration in cooking:
        if previous:
            low += previous[1] < 160 and value < 160
            if previous[1] > 180 and value > 180: high += 1; high_run += 1
            else: high_run = 0
            longest = max(longest,high_run)
        if value < 165:
            if episode is None or episode[1] is not None:
                if episode is not None: episodes.append(tuple(episode))
                episode = [micros(at),None,None]
            normal_since = None
        elif episode is not None:
            if episode[1] is None: episode[1] = micros(at)
            if 165 <= value <= 175:
                normal_since = normal_since or at
                if at-normal_since >= timedelta(seconds=5):
                    episode[2] = micros(at); episodes.append(tuple(episode)); episode = None; normal_since = None
            else: normal_since = None
        previous = at,value
    if episode is not None: episodes.append(tuple(episode))
    before = [point for point in points if point[0] < placed]
    last_before = before[-1] if before else None
    return TonkatsuControllerSummary(len(cooking), all(point[2] == 'OK' for point in cooking),
                                     min(point[1] for point in cooking), max(point[1] for point in cooking),
                                     last_before[1] if last_before else None,
                                     -micros(last_before[0]) if last_before else None,
                                     last_before[2] == 'OK' if last_before else None, low, high, longest, tuple(episodes))
