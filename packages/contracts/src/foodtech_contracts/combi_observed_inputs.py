"""Facts from a complete synthetic combi stream and its recorded work history.

The trusted caller must independently authorize the owned snapshot and establish
current/prior history. These observations are neither credentials nor a source
grant, history witness, completeness attestation or food-safety approval.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import re
from uuid import UUID

from .combi_cooking import RULE_VERSION, cooking_time_status, oven_load, rice_water
from .combi_stream import CombiStreamValidator
from .combi_rice_process import Sample as RiceSample, assess as assess_rice
from .combi_mackerel_process import Sample as FishSample, assess as assess_fish
from .combi_transition import TransitionSample, assess_transition
from .combi_probe import summarize
from .combi_sampling import selection_issues
from .combi_recommendation import CombiBaseline


def _number(value):
    if type(value) not in (str, int, Decimal):
        raise ValueError('finite decimal required')
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError('finite decimal required')
    return result


def _integer(value, minimum=1):
    if type(value) not in (int, Decimal):
        raise ValueError('bounded integer required')
    number = _number(value)
    if not minimum <= number < 2**63 or number != number.to_integral_value():
        raise ValueError('bounded integer required')
    return int(number)


def _time(value):
    if not isinstance(value, str):
        raise ValueError('recorded UTC time required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() != timedelta(0):
        raise ValueError('recorded UTC time required')
    return result


def _same(expected, actual):
    """DynamoDB integral Decimal is allowed; booleans are never integers."""
    if type(expected) is int:
        return type(actual) in (int, Decimal) and _number(actual) == expected
    if type(expected) is dict:
        return type(actual) is dict and set(actual) == set(expected) and all(_same(v, actual[k]) for k, v in expected.items())
    if type(expected) is list:
        return type(actual) is list and len(actual) == len(expected) and all(_same(a, b) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


@dataclass(frozen=True)
class ObservedCombiBatch:
    meal_id: str = field(repr=False)
    job_id: str = field(repr=False)
    meal_version: int
    source_digest: str = field(repr=False)
    baseline: CombiBaseline
    recorded_safety_status: str
    sampled_process_status: str
    sampled_safety_status: str | None
    unresolved: tuple[str, ...]
    humidity_source: str
    transition_humidity_source: str | None
    thermal_exposure_seconds: int
    preceding_steam: tuple[tuple[str, object], ...] = ()
    preparation: tuple[tuple[str, object], ...] = ()
    safety_summary: tuple[tuple[str, object], ...] = ()
    effective_water_ratio: str | None = None
    added_water_kg: str | None = None

    def model_context(self):
        # Do not copy source IDs, notes, actor identities, raw text or timestamps.
        b = self.baseline
        return {'basis': 'SYNTHETIC_COMBI_SAMPLES_ONLY', 'menu': b.menu, 'size': b.size,
            'rack_count': b.rack_count, 'pan_count': b.pan_count,
            'ingredient_kg': str(b.ingredient_kg), 'pan_tare_kg': str(b.pan_tare_kg),
            'rack_tare_kg': str(b.rack_tare_kg),
            'program_temperature_c': str(b.previous_temperature_c),
            'program_seconds': b.previous_duration_seconds,
            'thermal_exposure_seconds': self.thermal_exposure_seconds,
            'dry_rice_kg': str(b.dry_rice_kg) if b.dry_rice_kg is not None else None,
            'drained_rice_kg': str(b.drained_rice_kg) if b.drained_rice_kg is not None else None,
            'effective_water_ratio': self.effective_water_ratio, 'added_water_kg': self.added_water_kg,
            'recorded_safety_status': self.recorded_safety_status,
            'sampled_safety_status': self.sampled_safety_status,
            'sampled_process_status': self.sampled_process_status,
            'humidity_available': b.humidity_available, 'transition_ready': b.transition_ready,
            'humidity_value_source': self.humidity_source,
            'transition_humidity_value_source': self.transition_humidity_source,
            'preceding_steam': dict(self.preceding_steam) if self.preceding_steam else None,
            'rice_preparation': dict(self.preparation) if self.preparation else None,
            'safety_summary': dict(self.safety_summary) if self.safety_summary else None,
            'unresolved': list(self.unresolved)}


def _stream(meta, raws):
    source = meta['source']
    total = _integer(source['record_count'])
    if type(raws) is not list or not 3 <= total <= 5000 or len(raws) != total or any(type(r) is not str for r in raws):
        raise ValueError('complete bounded stream required')
    encoded = [r.encode('utf-8') for r in raws]
    if any(len(r) > 2048 for r in encoded) or sum(len(r) + 1 for r in encoded) > 2_500_000:
        raise ValueError('source size exceeded')
    digest = hashlib.sha256(b'\n'.join(encoded) + b'\n').hexdigest()
    if digest != source['raw_sha256']:
        raise ValueError('source digest changed')
    names = {'RICE': 'rice_cooking_job_id', 'TRANSITION': 'mode_transition_id', 'MACKEREL': 'mackerel_cooking_job_id'}
    jobs = {phase: meta[key] for phase, key in names.items()}
    if any(str(UUID(value)) != value for value in (meta['meal_batch_id'], *jobs.values())):
        raise ValueError('invalid work identifiers')
    validator = CombiStreamValidator(meta['meal_batch_id'], source['device_id'], jobs)
    groups = {phase: [] for phase in names}
    for raw in raws:
        record = validator.accept(raw)
        groups[record['phase']].append(record)
    counts = {phase: _integer(source['phase_counts'][phase]) for phase in names}
    if set(source['phase_counts']) != set(names) or counts != {phase: len(rows) for phase, rows in groups.items()} or \
            source['first_observed_at'] != groups['RICE'][0]['occurredAt']:
        raise ValueError('source receipt mismatch')
    return groups, digest, jobs


def _lineage(lineage, selected, digest, phase):
    return _same({'raw_sha256': digest, 'phase': phase,
                  'first_sequence': selected[0]['sequence'], 'last_sequence': selected[-1]['sequence']}, lineage)


def _load(meta, step, menu, rows):
    if menu == 'RICE':
        water = rice_water(*[_number(step[k]) for k in ('dry_rice_kg', 'drained_rice_kg', 'added_water_kg')])
        if _number(step['effective_water_ratio']) != water.effective_water_ratio:
            raise ValueError('water snapshot mismatch')
        load = oven_load(meta['equipment_size'], _integer(step['rack_count']), _integer(step['pan_count']),
            _number(step['drained_rice_kg']) + _number(step['added_water_kg']),
            _number(step['pan_tare_kg']), _number(step['rack_tare_kg']))
        if any(_number(step[k]) != getattr(load, k) for k in ('ingredient_kg', 'payload_kg', 'gross_kg', 'load_ratio')) or \
                step['load_basis'] != load.load_basis:
            raise ValueError('rice load snapshot mismatch')
    else:
        products, snapshot = step['products'], step['load_snapshot']
        load_events = [r for r in rows.values() if r.get('event') == 'MACKEREL_STARTED']
        load_event, = load_events
        if not _same(snapshot, load_event['load_snapshot']) or load_event['observed_at'] != snapshot['observed_at']:
            raise ValueError('loading audit differs from current snapshot')
        if type(products) is not list or not 1 <= len(products) <= 200 or \
                len({p['product_id'] for p in products}) != len(products):
            raise ValueError('product inventory unavailable')
        for product in products:
            if any(not isinstance(product[k], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}', product[k])
                   for k in ('product_id', 'pan_id', 'rack_id')) or not 1 <= _integer(product['shelf']) <= 10 or \
                    not Decimal('.12') <= _number(product['mass_kg']) <= Decimal('.15') or \
                    not Decimal('1.5') <= _number(product['thickness_cm']) <= Decimal('2.0'):
                raise ValueError('product outside synthetic profile')
        racks = {p['rack_id'] for p in products}
        pans = {p['pan_id']: (p['rack_id'], p['shelf']) for p in products}
        shelves = {(p['rack_id'], p['shelf']): p['pan_id'] for p in products}
        if any(pans[p['pan_id']] != (p['rack_id'], p['shelf']) or shelves[(p['rack_id'], p['shelf'])] != p['pan_id']
               for p in products) or any(sum(p['pan_id'] == pan for p in products) > 10 for pan in pans):
            raise ValueError('ambiguous product layout')
        load = oven_load(meta['equipment_size'], len(racks), len(pans),
            sum((_number(p['mass_kg']) for p in products), Decimal(0)), _number(snapshot['pan_tare_kg']),
            Decimal(15) * len(racks) if meta['equipment_size'] == 'LARGE_ROLL_IN_RACK_20' else Decimal(0))
        expected = {'rule_version': RULE_VERSION, 'size': load.size, 'rack_count': load.rack_count,
            'pan_count': load.pan_count, 'load_basis': load.load_basis, 'observed_at': step['active_cooking_started_at']}
        if any(not _same(value, snapshot.get(key)) for key, value in expected.items()) or any(
                _number(snapshot[k]) != getattr(load, k) for k in
                ('ingredient_kg', 'pan_tare_kg', 'rack_tare_kg', 'payload_kg', 'gross_kg', 'rated_capacity_kg', 'load_ratio')):
            raise ValueError('fish load snapshot mismatch')
    if load.violations or load.ingredient_kg <= 0 or (load.size == 'LARGE_ROLL_IN_RACK_20' and
                                                  load.rack_tare_kg != 15 * load.rack_count):
        raise ValueError('load unavailable')
    return load


def _safety(step, rows, menu, process, selected, now):
    version = _integer(step['safety_version'])
    kind = 'RICE_SAFETY_CONFIRMED' if menu == 'RICE' else 'MACKEREL_CORE_CHECK'
    events = [row for row in rows.values() if row.get('event') == kind and
              type(row.get('safety_version')) in (int, Decimal) and row['safety_version'] == version]
    event, = events
    if menu == 'RICE':
        if any(r.get('criticalAlarm') is not False for r in selected) or \
                'SENSOR_ANOMALY_SUSPECTED' in process['reason_codes'] or \
                process['status'] not in {'NORMAL', 'WARNING'} or step['rice_safety_observation'] != 'NO_ANOMALY' or \
                event['observation'] != 'NO_ANOMALY' or event['process_status'] != process['status'] or \
                not _same(process['reason_codes'], event['process_reason_codes']) or any(
                    event.get(key) is not True for key in ('source_checked', 'fan_checked', 'climate_checked')) or \
                event.get('critical_alarm_observed') is not False or not \
                _time(step['food_unloaded_at']) <= _time(event['observed_at']) <= now:
            raise ValueError('rice safety evidence unavailable')
        result = 'PASSED'
        summary = {'basis': 'PROCESS_AND_OPERATOR_OBSERVATION'}
    else:
        if step.get('latest_reheat') or event.get('reheat_segment_id') is not None:
            raise ValueError('reheated work needs its own source observation')
        products = {p['product_id']: p for p in step['products']}
        probes = event['probes']
        if type(probes) is not list or len(probes) != min(3, len(products)) or \
                len({p['probe_id'] for p in probes}) != len(probes) or len({p['product_id'] for p in probes}) != len(probes) or \
                not _same(step['products'], event['products']):
            raise ValueError('probe inventory mismatch')
        start = _time(event['observed_at'])
        if start != _time(step['food_unloaded_at']) or start + timedelta(seconds=60) > now or \
                _time(step['safety_measured_through']) != start + timedelta(seconds=60) or \
                event.get('selection_issues') != [] or \
                selection_issues(list(products.values()), {p['product_id'] for p in probes}, event.get('selection_note')):
            raise ValueError('probe window or selection unavailable')
        summaries = []
        for probe in probes:
            if probe['calibration_status'] != 'OK' or type(probe['readings']) is not list or len(probe['readings']) != 60:
                raise ValueError('complete calibrated probes required')
            readings = [{'second': _integer(r['second'], 0), 'celsius': _number(r['celsius'])} for r in probe['readings']]
            if [r['second'] for r in readings] != list(range(60)) or any(not -50 <= r['celsius'] <= 150 for r in readings):
                raise ValueError('invalid probe samples')
            summaries.append(summarize({**probe, 'readings': readings}, products[probe['product_id']], start))
        if not _same(summaries, step['safety_probe_summaries']) or not _same(summaries, event['probe_summaries']):
            raise ValueError('probe summary mismatch')
        result = 'FAILED' if any(s['status'] == 'FAILED' for s in summaries) else 'PASSED'
        minimum = min(_number(s['full_interval_min_celsius']) for s in summaries)
        if _number(step['batch_representative_core_temperature']) != minimum:
            raise ValueError('probe minimum mismatch')
        summary = {'basis': 'CALIBRATED_REPRESENTATIVE_PROBES', 'probe_count': len(summaries),
            'full_interval_min_celsius': str(minimum),
            'minimum_75c_hold_seconds': min(s['longest_75c_hold_seconds'] for s in summaries)}
    if step['safety_status'] != result or event['safety_status'] != result:
        raise ValueError('recorded safety differs from observations')
    return result, tuple(summary.items())


def observe_combi_batch(rows, raws, menu, *, now):
    """Observe unprojected persisted rows from an independently authorized stable snapshot."""
    try:
        return _observe(rows, raws, menu, now)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError, OverflowError):
        raise ValueError('synthetic combi observation unavailable') from None


def _observe(rows, raws, menu, now):
    if type(rows) is not dict or menu not in {'RICE', 'MACKEREL'} or not isinstance(now, datetime) or \
            now.utcoffset() != timedelta(0):
        raise ValueError('explicit current UTC scope required')
    if any(type(row) is not dict or row.get('sk') != key for key, row in rows.items()):
        raise ValueError('persisted row identity mismatch')
    meta = rows['META']
    if meta['provenance'] != 'SYNTHETIC':
        raise ValueError('synthetic records required')
    version = _integer(meta['version'])
    groups, digest, jobs = _stream(meta, raws)
    step, selected = rows[f'JOB#{menu}#{jobs[menu]}'], groups[menu]
    if step['job_id'] != jobs[menu] or step['meal_batch_id'] != meta['meal_batch_id'] or \
            step['menu'] != menu or step['status'] != 'UNLOADED' or not _lineage(
                step['monitored_process_source'], selected, digest, menu):
        raise ValueError('bound completed work required')
    duration = _integer(step['program_cooking_duration'])
    start, completed, unloaded = (_time(step[k]) for k in
                                  ('active_cooking_started_at', 'program_completed_at', 'food_unloaded_at'))
    if duration != len(selected) or start != _time(selected[0]['occurredAt']) or \
            completed != start + timedelta(seconds=duration) or not completed <= unloaded <= now:
        raise ValueError('recorded work interval mismatch')
    exposure = int((unloaded - start).total_seconds())
    if _integer(step['thermal_exposure_duration']) != exposure:
        raise ValueError('recorded thermal exposure mismatch')
    timing = cooking_time_status(menu, duration)
    if step['time_status'] != timing:
        raise ValueError('recorded time status mismatch')
    humidity = {r['humidityValueSource'] for r in selected}
    if len(humidity) != 1:
        raise ValueError('changing humidity capability')
    humidity_available = next(iter(humidity)) != 'NOT_SUPPORTED'
    if menu == 'RICE':
        process = assess_rice([RiceSample(r['phaseSecond'], _number(r['chamberCelsius']),
            _number(r['measuredHumidityPercent']) if r['measuredHumidityPercent'] is not None else None,
            r['doorState'], r['fanLevel'], _number(r['climateSetpointPercent'])) for r in selected], duration, timing,
            next(iter(humidity)))
    else:
        process = assess_fish([FishSample(r['phaseSecond'], _number(r['chamberCelsius']),
            r['doorState'], r['fanLevel'], _number(r['climateSetpointPercent'])) for r in selected], duration, timing)
    if not _same(process, step['monitored_process_assessment']):
        raise ValueError('recorded process differs from samples')
    load = _load(meta, step, menu, rows)
    unresolved = []
    affected = selected if menu == 'RICE' else [r for group in groups.values() for r in group]
    if any(r.get('criticalAlarm') is not False for r in affected):
        unresolved.append('CRITICAL_ALARM_OR_UNKNOWN')
    if process['status'] == 'INSUFFICIENT_DATA' or 'SENSOR_ANOMALY_SUSPECTED' in process['reason_codes']:
        unresolved.append('PROCESS_EVIDENCE_UNAVAILABLE')
    safety, safety_summary = None, ()
    try:
        safety, safety_summary = _safety(step, rows, menu, process, selected, now)
    except (ValueError, KeyError, TypeError, ArithmeticError):
        unresolved.append('SAFETY_EVIDENCE_UNAVAILABLE')
    preparation, steam = (), ()
    if menu == 'RICE':
        try:
            prep = step['rice_preparation']
            began, soaked, drained = (_time(prep[k]) for k in
                ('soaking_started_at', 'soaking_completed_at', 'draining_completed_at'))
            wash, temperature = _integer(prep['wash_count'], 0), _number(prep['soaking_water_celsius'])
            soak_seconds, drain_seconds = (_number(prep[k]) for k in ('soaking_seconds', 'draining_seconds'))
            def seconds(delta):
                return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000
            if prep['provenance'] != 'SYNTHETIC' or not began <= soaked <= drained <= start or \
                    wash > 20 or not 0 <= temperature <= 100 or \
                    seconds(soaked - began) != soak_seconds or seconds(drained - soaked) != drain_seconds:
                raise ValueError
            preparation = tuple({'wash_count': wash, 'soaking_water_celsius': str(temperature),
                'soaking_seconds': str(soak_seconds), 'draining_seconds': str(drain_seconds),
                **{k: str(_number(prep[k])) if prep.get(k) is not None else None
                   for k in ('washed_rice_kg', 'soaked_rice_kg')}}.items())
            if any(_number(prep[k]) <= 0 for k in ('washed_rice_kg', 'soaked_rice_kg') if prep.get(k) is not None):
                raise ValueError
        except (ValueError, KeyError, TypeError, ArithmeticError):
            preparation = ()
            unresolved.append('RICE_PREPARATION_UNAVAILABLE')
    transition_ready, transition_humidity = None, None
    if menu == 'MACKEREL':
        transition, samples = rows['TRANSITION#' + jobs['TRANSITION']], groups['TRANSITION']
        steam = tuple({'sample_count': len(groups['RICE']),
            'minimum_chamber_celsius': str(min(_number(r['chamberCelsius']) for r in groups['RICE'])),
            'maximum_chamber_celsius': str(max(_number(r['chamberCelsius']) for r in groups['RICE'])),
            'humidity_value_sources': tuple(sorted({r['humidityValueSource'] for r in groups['RICE']}))}.items())
        try:
            sources, vents = {r['humidityValueSource'] for r in samples}, {r['ventSupported'] for r in samples}
            if len(sources) != 1 or sources - {'MEASURED', 'NOT_SUPPORTED'} or len(vents) != 1 or \
                    any(r['schemaVersion'] != 'synthetic-combi-v2' for r in samples) or not _lineage(
                        transition['source'], samples, digest, 'TRANSITION') or \
                    _time(transition['started_at']) != _time(samples[0]['occurredAt']):
                raise ValueError
            calculated = assess_transition([TransitionSample(r['phaseSecond'], _number(r['chamberCelsius']),
                _number(r['climateSetpointPercent']), r['doorState'] == 'CLOSED', r['criticalAlarm'],
                _number(r['measuredHumidityPercent']) if r['measuredHumidityPercent'] is not None else None,
                r['ventEvent']) for r in samples], humidity_supported=sources == {'MEASURED'}, vent_supported=next(iter(vents)))
            if any(not _same(value, transition.get(key)) for key, value in calculated.items()) or \
                    calculated['status'] != 'DRY_HEAT_READY' or _time(transition['started_at']) + timedelta(seconds=len(samples)) > start:
                raise ValueError
            transition_ready = True
            transition_humidity = next(iter(sources))
            humidity_available = transition_humidity == 'MEASURED'
        except (ValueError, KeyError, TypeError, ArithmeticError):
            humidity_available = None
            unresolved.append('MODE_TRANSITION_UNVERIFIED')
    if _number(step['program_celsius']) != (100 if menu == 'RICE' else 220) or \
            _number(step['climate_setpoint_percent']) != (100 if menu == 'RICE' else 10) or \
            _integer(step['fan_level']) != (4 if menu == 'RICE' else 3):
        raise ValueError('program baseline unavailable')
    baseline = CombiBaseline(menu, load.size, load.rack_count, load.pan_count, load.ingredient_kg,
        load.pan_tare_kg, load.rack_tare_kg, _number(step['program_celsius']), duration,
        True if any(r.get('criticalAlarm') is True for r in affected) else
        False if all(r.get('criticalAlarm') is False for r in affected) else None, safety is not None,
        humidity_available, transition_ready,
        _number(step['dry_rice_kg']) if menu == 'RICE' else None,
        _number(step['drained_rice_kg']) if menu == 'RICE' else None)
    return ObservedCombiBatch(meal_id=meta['meal_batch_id'], job_id=jobs[menu], meal_version=version,
        source_digest=digest, baseline=baseline, recorded_safety_status=step['safety_status'],
        sampled_process_status=process['status'], sampled_safety_status=safety, unresolved=tuple(unresolved),
        humidity_source=next(iter(humidity)), transition_humidity_source=transition_humidity,
        thermal_exposure_seconds=exposure, preceding_steam=steam, preparation=preparation, safety_summary=safety_summary,
        effective_water_ratio=str(_number(step['effective_water_ratio'])) if menu == 'RICE' else None,
        added_water_kg=str(_number(step['added_water_kg'])) if menu == 'RICE' else None)
