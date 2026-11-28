"""Recorded synthetic fryer facts, without source/history or safety attestation.

The caller must independently authorize and receipt-match the whole raw
snapshot. A valid declared value is not proof of ingredient or oil suitability.
Point samples are never interpolated into a continuous temperature history.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from .profile100 import canonical_record, decode_record, stream_binding_key
from .collection_evidence import MAX_RECEIPT_RECORDS
from .tonkatsu_batch import MAX_BATCH_G, PROFILE, WEIGHT_TOLERANCE_G, batch as check_batch
from .tonkatsu_process import TIME_BANDS, cooking_time_status, thickness_profile
from .recommendation_output import TonkatsuBaseline
from .tonkatsu_controller_samples import observe_controller, TonkatsuControllerSummary


def _number(value, minimum, maximum):
    if type(value) not in (str, int, Decimal):
        raise ValueError('recorded number required')
    result = Decimal(value)
    if not result.is_finite() or not minimum <= result <= maximum:
        raise ValueError('recorded number out of bounds')
    return result


def _integer(value, minimum, maximum):
    result = _number(value, minimum, maximum)
    if type(value) is str or result != int(result):
        raise ValueError('recorded integer required')
    return int(result)


def _instant(value):
    if type(value) is not str or len(value) > 40:
        raise ValueError('recorded timestamp required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() != timedelta(0):
        raise ValueError('recorded UTC timestamp required')
    return result


@dataclass(frozen=True)
class TonkatsuBatchFacts:
    thickness_cm: Decimal
    product_weights_g: tuple[int, ...]
    batch_mode: str
    batch_rule_version: str
    rated_capacity_kg: Decimal
    load_percent: Decimal

    def model_context(self):
        maximum_count, target_weight = PROFILE[thickness_profile(self.thickness_cm)[1]]
        return {
            "product_classification": "NON_MINCED_PORK_LOIN_RAW",
            "thickness_cm": str(self.thickness_cm),
            "product_weights_g": list(self.product_weights_g),
            "product_count": len(self.product_weights_g),
            "batch_mode": self.batch_mode,
            "batch_rule_version": self.batch_rule_version,
            "rated_capacity_kg": str(self.rated_capacity_kg),
            "load_percent": str(self.load_percent),
            "maximum_product_count": maximum_count,
            "target_product_weight_g": target_weight,
            "product_weight_tolerance_g": WEIGHT_TOLERANCE_G,
            "maximum_batch_mass_kg": str(Decimal(MAX_BATCH_G) / 1000),
        }


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


@dataclass(frozen=True)
class ObservedTonkatsuBatch:
    job_id: str = field(repr=False)
    profile: str
    cooking_seconds: int
    load_kg: Decimal
    sample_count: int
    minimum_sample_c: Decimal
    maximum_sample_c: Decimal
    last_sample_c: Decimal
    preplacement_sample_c: Decimal | None
    preplacement_age_us: int | None
    ingredient_temperature_c: Decimal | None
    storage_state: str
    oil_capacity_l: Decimal | None
    oil_batches_since_change: int | None
    oil_cooked_mass_kg: Decimal | None
    recorded_safety_status: str
    core_summary: TonkatsuCoreSummary | None
    batch_facts: TonkatsuBatchFacts
    reference_recipe_version: str | None = field(repr=False)
    recorded_process_status: str
    controller_summary: TonkatsuControllerSummary | None = field(repr=False)
    unresolved: tuple[str, ...]

    @property
    def core_series_consistent(self):
        return self.core_summary is not None

    def as_baseline(self):
        return TonkatsuBaseline(self.profile, self.cooking_seconds)

    def model_context(self):
        number = lambda value: str(value) if value is not None else None
        return {'basis': 'SYNTHETIC_POINT_SAMPLES_ONLY', 'profile': self.profile,
            'cooking_seconds': self.cooking_seconds, 'load_kg': str(self.load_kg),
            'sample_count': self.sample_count, 'minimum_sample_c': str(self.minimum_sample_c),
            'maximum_sample_c': str(self.maximum_sample_c), 'last_sample_c': str(self.last_sample_c),
            'preplacement_sample_c': number(self.preplacement_sample_c),
            'preplacement_age_us': self.preplacement_age_us,
            'initial_context_status': 'DECLARED_UNVERIFIED',
            'ingredient_temperature_c': number(self.ingredient_temperature_c),
            'storage_state': self.storage_state, 'oil_capacity_l': number(self.oil_capacity_l),
            'oil_batches_since_change': self.oil_batches_since_change,
            'oil_cooked_mass_kg': number(self.oil_cooked_mass_kg),
            'recorded_safety_status': self.recorded_safety_status,
            'recorded_process_status': self.recorded_process_status,
            'cooking_time_status': cooking_time_status(self.profile, self.cooking_seconds),
            'batch': self.batch_facts.model_context(),
            'core_observation': self.core_summary.model_context() if self.core_summary else None,
            'core_series_consistent': self.core_series_consistent,
            'controller_observation': self.controller_summary.model_context() if self.controller_summary else None,
            'unresolved': list(self.unresolved)}


def observe_tonkatsu_batch(job, raws):
    try:
        return _observe(job, raws)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError, OverflowError):
        raise ValueError('synthetic fryer observation unavailable') from None


def _core_summary(job, count):
    """Summarize each calibrated series before checking the stored decision."""
    try:
        if _integer(job['core_check_safety_version'], 1, 10**18) != _integer(job['safety_version'], 1, 10**18):
            return None
        probes = job['core_check']
        if type(probes) is not list or len(probes) != count:
            return None
        ids, probe_ids, removals = set(), set(), set()
        minima = []
        lifted = _instant(job['actual_lifted_at'])
        for probe in probes:
            item = probe['item_id']
            if type(item) is not str or not 1 <= len(item) <= 80 or item in ids or probe.get('calibration_status') != 'OK':
                return None
            probe_id = probe.get('probe_id')
            removal = _integer(probe.get('removal_sequence'), 1, count)
            if type(probe_id) is not str or not 1 <= len(probe_id) <= 80 or probe_id in probe_ids or removal in removals or \
                    _instant(probe.get('measurement_start')) != lifted or \
                    _instant(probe.get('measurement_end')) != lifted + timedelta(seconds=60):
                return None
            ids.add(item); probe_ids.add(probe_id); removals.add(removal)
            samples = probe['samples']
            if type(samples) is not list or len(samples) != 61:
                return None
            temperatures = []
            for second, sample in enumerate(samples):
                if _integer(sample['at_second'], 0, 60) != second:
                    return None
                temperatures.append(_number(sample['celsius'], Decimal('-273.15'), Decimal(300)))
            minima.append(min(temperatures))
        below = sum(temperature < 75 for temperature in minima)
        if job.get('safety_status') != ('FAILED' if below else 'PASSED'):
            return None
        return TonkatsuCoreSummary(count, min(minima), below)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
        return None


def _observe(job, raws):
    if type(job) is not dict or job.get('menu') != 'TONKATSU' or job.get('provenance') != 'SYNTHETIC' or \
            job.get('equipment_profile') != 'synthetic-fryer-approved-v2' or \
            type(job.get('job_id')) is not str or str(UUID(job['job_id'])) != job['job_id'] or \
            job.get('status') not in {'COMPLETED', 'FAILED'}:
        raise ValueError('unsupported recorded fryer job')
    profile = job.get('thickness_profile')
    thickness = _number(job['thickness_cm'], Decimal('0.01'), Decimal(10))
    if type(profile) is not str or profile not in TIME_BANDS or thickness_profile(thickness)[1] != profile:
        raise ValueError('inconsistent recorded thickness')
    seconds = _integer(job['actual_cooking_seconds'], 1, 86400)
    placed, lifted = _instant(job['actual_placed_at']), _instant(job['actual_lifted_at'])
    if lifted - placed != timedelta(seconds=seconds):
        raise ValueError('inconsistent recorded cooking interval')
    recorded_batch = job['tonkatsu_batch']
    weights = [_integer(value, 1, 10000) for value in recorded_batch['product_weights_g']]
    expected = check_batch(profile, weights, recorded_batch['mode'])
    if recorded_batch != expected:
        raise ValueError('inconsistent recorded load')
    load = _number(job['actual_load_kg'], Decimal('0.001'), Decimal('2.5'))
    if load != Decimal(expected['total_mass_kg']):
        raise ValueError('inconsistent actual load')
    binding = job['source_binding']
    if type(binding) is not dict or set(binding) != {'source_id', 'device_id', 'batch_id'} or \
            binding['source_id'] != 'synthetic-rest' or binding['device_id'] != 'mockprofile-100-a-001' or \
            type(binding['batch_id']) is not str or not binding['batch_id'].startswith('batch-a-') or \
            type(raws) is not list or not 1 <= len(raws) <= MAX_RECEIPT_RECORDS:
        raise ValueError('explicit synthetic fryer source required')
    stream = stream_binding_key(**binding)
    samples = []; source_records = []; record_ids = set(); sequences = set(); unresolved = []
    for row in raws:
        if type(row) is not dict or row.get('pk') != stream or row.get('provenance') != 'SYNTHETIC' or \
                type(row.get('sk')) is not str or not row['sk'].startswith('RAW#'):
            raise ValueError('foreign raw fryer row')
        decoded = decode_record(row['raw'])
        sample = canonical_record(decoded)
        source_records.append(decoded)
        if any(sample[k] != v for k,v in binding.items()) or sample['record_id'] in record_ids or \
                sample['sequence_number'] in sequences:
            raise ValueError('mixed or duplicate raw fryer records')
        record_ids.add(sample['record_id']); sequences.add(sample['sequence_number'])
        at = _instant(sample['event_time'])
        temperature = _number(sample['temperature_celsius'], Decimal('-273.15'), Decimal(300))
        if sample['device_status'] != 'running' or sample.get('error_reason'):
            unresolved.append('SOURCE_DEVICE_NOT_RUNNING_OR_ERROR')
        samples.append((sample['sequence_number'], at, temperature))
    samples.sort()
    source_records.sort(key=lambda row: row['sequenceNumber'])
    controller = observe_controller(source_records, placed=placed, lifted=lifted)
    if controller is None: unresolved.append('EXPLICIT_1HZ_CONTROLLER_SAMPLES_MISSING')
    elif not controller.calibration_all_ok: unresolved.append('CONTROLLER_CALIBRATION_NOT_OK')
    if samples[0][0] != 1 or any(b[0] != a[0]+1 or b[1]-a[1] != timedelta(seconds=5)
            for a,b in zip(samples,samples[1:])):
        unresolved.append('SOURCE_SEQUENCE_OR_CADENCE_GAP')
    if samples[0][1] > placed or samples[-1][1] < lifted:
        unresolved.append('SOURCE_DOES_NOT_BRACKET_COOKING_INTERVAL')
    during = [row for row in samples if placed <= row[1] <= lifted]
    if not during:
        raise ValueError('no recorded cooking temperature samples')
    before = [row for row in samples if row[1] < placed]
    preload = before[-1] if before else None
    age = None
    if preload:
        delta = placed-preload[1]
        age = (delta.days*86400+delta.seconds)*1_000_000+delta.microseconds
    else:
        unresolved.append('PREPLACEMENT_TEMPERATURE_NOT_CAPTURED')
    preload_value = preload[2] if preload else None
    if controller is not None:
        preload_value, age = controller.preplacement_sample_c, controller.preplacement_age_us
        if preload_value is None: unresolved.append('PREPLACEMENT_TEMPERATURE_NOT_CAPTURED')
        if controller.preplacement_calibration_ok is not True: unresolved.append('PREPLACEMENT_CALIBRATION_NOT_OK')
    initial = job.get('tonkatsu_initial_context')
    values = {}; storage = 'UNKNOWN'
    if type(initial) is not dict or initial.get('schema_version') != 'tonkatsu-initial-context-v1' or \
            initial.get('provenance') != 'SYNTHETIC' or initial.get('status') != 'DECLARED_UNVERIFIED' or \
            _integer(initial.get('batch_version'), 1, 10**18) != _integer(job.get('tonkatsu_batch_version'), 1, 10**18) or \
            initial.get('product_classification') != 'NON_MINCED_PORK_LOIN_RAW':
        unresolved.append('INITIAL_CONTEXT_MISSING_OR_STALE')
        initial = {}
    for name,minimum,maximum in [('ingredient_temperature_c', Decimal('-273.15'), Decimal(300)),
            ('oil_capacity_l', Decimal('0.001'), Decimal('99999.99')),
            ('oil_cooked_mass_kg', Decimal(0), Decimal('999999.999'))]:
        try: values[name] = _number(initial[name], minimum, maximum)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            values[name] = None; unresolved.append(name.upper()+'_UNAVAILABLE')
    try:
        values['oil_batches_since_change'] = _integer(initial['oil_batches_since_change'], 0, 1_000_000)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        values['oil_batches_since_change'] = None; unresolved.append('OIL_BATCH_COUNT_UNAVAILABLE')
    for name in ('temperature_observed_at', 'oil_changed_at'):
        try:
            if _instant(initial[name]) > placed: raise ValueError
        except (KeyError, TypeError, ValueError): unresolved.append(name.upper()+'_UNAVAILABLE')
    if initial.get('storage_state') in {'CHILLED', 'FROZEN', 'OTHER'}:
        storage = initial['storage_state']
    else: unresolved.append('STORAGE_STATE_UNAVAILABLE')
    if storage not in {'CHILLED', 'UNKNOWN'}:
        unresolved.append('STORAGE_OUTSIDE_SYNTHETIC_SCENARIO')
    core = _core_summary(job, expected['product_count'])
    if core is None:
        unresolved.append('CORE_SERIES_MISSING_STALE_OR_INCONSISTENT')
    recipe = job.get('recipe_version')
    if type(recipe) is not str or not 1 <= len(recipe) <= 80 or not recipe.strip():
        recipe = None
        unresolved.append('REFERENCE_RECIPE_VERSION_UNAVAILABLE')
    facts = TonkatsuBatchFacts(thickness, tuple(weights), expected['mode'], expected['rule_version'],
                              Decimal(expected['rated_capacity_kg']), Decimal(expected['load_percent']))
    recorded_process = job.get('process_assessment_status')
    if type(recorded_process) is not str or recorded_process not in {'NORMAL', 'WARNING', 'FAILED', 'INSUFFICIENT_DATA'}:
        recorded_process = 'NOT_RECORDED'
    if job.get('pending_tonkatsu_reheat') or job.get('latest_tonkatsu_reheat'):
        unresolved.append('REHEAT_TIMELINE_REQUIRES_SEPARATE_OBSERVATION')
    return ObservedTonkatsuBatch(
        job_id=job['job_id'], profile=profile, cooking_seconds=seconds, load_kg=load,
        sample_count=len(during), minimum_sample_c=min(row[2] for row in during),
        maximum_sample_c=max(row[2] for row in during), last_sample_c=during[-1][2],
        preplacement_sample_c=preload_value, preplacement_age_us=age,
        ingredient_temperature_c=values['ingredient_temperature_c'], storage_state=storage,
        oil_capacity_l=values['oil_capacity_l'], oil_batches_since_change=values['oil_batches_since_change'],
        oil_cooked_mass_kg=values['oil_cooked_mass_kg'],
        recorded_safety_status=job['safety_status'] if job.get('safety_status') in {'PASSED', 'FAILED'} else 'NOT_MEASURED',
        core_summary=core, batch_facts=facts, reference_recipe_version=recipe,
        recorded_process_status=recorded_process, controller_summary=controller,
        unresolved=tuple(sorted(set(unresolved))),
    )
