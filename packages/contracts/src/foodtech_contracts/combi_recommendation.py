"""Encoded limits for internal synthetic combi programme candidates.

Baselines and evidence must be derived by a trusted server from owned records.
These checks do not prove source completeness, food safety or recipe adoption.
Fixed programme settings are not widened to observed-temperature bands.
"""
from dataclasses import dataclass
from decimal import Decimal

from .combi_cooking import TIME_BANDS, oven_load
from .recommendation_guard import Evidence, GuardResult, _evidence, _finite, _seconds

SCHEMA_VERSION = 'synthetic-combi-recommendation-output-v1'
RULE_VERSION = 'synthetic-combi-recommendation-guard-v1'


@dataclass(frozen=True)
class CombiBaseline:
    menu: str
    size: str
    rack_count: int
    pan_count: int
    ingredient_kg: Decimal
    pan_tare_kg: Decimal
    rack_tare_kg: Decimal
    previous_temperature_c: Decimal | None
    previous_duration_seconds: int | None
    process_interrupted: bool | None
    safety_basis_valid: bool | None
    humidity_available: bool | None
    transition_ready: bool | None
    dry_rice_kg: Decimal | None = None
    drained_rice_kg: Decimal | None = None


def fields(menu):
    if menu not in {'RICE', 'MACKEREL'}:
        raise ValueError('unsupported combi menu')
    rice = menu == 'RICE'
    result = {'temperature_c': {'type': 'number', 'enum': [100 if rice else 220]},
        'duration_seconds': {'type': 'integer', 'minimum': TIME_BANDS[menu][0], 'maximum': TIME_BANDS[menu][-1]},
        'climate_setpoint_percent': {'type': 'number', 'enum': [100 if rice else 10]},
        'fan_level': {'type': 'integer', 'enum': [4 if rice else 3]},
        'pan_positions': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'uniqueItems': True,
            'items': {'type': 'object', 'properties': {'rack': {'type': 'integer', 'minimum': 1, 'maximum': 2}, 'shelf': {'type': 'integer', 'minimum': 1, 'maximum': 10}},
                      'required': ['rack', 'shelf'], 'additionalProperties': False}}}
    if menu == 'RICE':
        result.update(effective_water_ratio={'type': 'number', 'minimum': 1.15, 'maximum': 1.25},
                      soak_seconds={'type': 'integer', 'enum': [1800]},
                      drain_seconds={'type': 'integer', 'enum': [300]})
    else:
        result['transition_duration_seconds'] = {'type': 'integer', 'minimum': 30, 'maximum': 300}
    return result


def _baseline(evidence, baseline):
    unknown = _evidence(evidence)
    if baseline.menu not in {'RICE', 'MACKEREL'}:
        unknown.append('MENU_UNAVAILABLE')
    if baseline.process_interrupted is not False:
        unknown.append('PROCESS_INTERRUPTED_OR_UNKNOWN')
    if baseline.safety_basis_valid is not True:
        unknown.append('SAFETY_BASIS_UNAVAILABLE')
    if type(baseline.humidity_available) is not bool or (baseline.menu == 'RICE' and not baseline.humidity_available):
        unknown.append('HUMIDITY_EVIDENCE_UNAVAILABLE')
    if baseline.menu == 'MACKEREL' and baseline.transition_ready is not True:
        unknown.append('MODE_TRANSITION_NOT_VERIFIED')
    try:
        if type(baseline.rack_count) is not int or baseline.rack_count < 1 or \
                type(baseline.pan_count) is not int or baseline.pan_count < 1 or \
                not _finite(baseline.ingredient_kg) or baseline.ingredient_kg <= 0:
            raise ValueError
        load = oven_load(baseline.size, baseline.rack_count, baseline.pan_count,
                         baseline.ingredient_kg, baseline.pan_tare_kg, baseline.rack_tare_kg)
        if load.violations or (baseline.size == 'LARGE_ROLL_IN_RACK_20' and
                baseline.rack_tare_kg != 15 * baseline.rack_count):
            raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        unknown.append('EQUIPMENT_OR_LOAD_BASELINE_UNAVAILABLE')
    if baseline.menu == 'RICE' and (not _finite(baseline.dry_rice_kg) or baseline.dry_rice_kg <= 0 or
            not _finite(baseline.drained_rice_kg) or baseline.drained_rice_kg < baseline.dry_rice_kg):
        unknown.append('RICE_WATER_BASELINE_UNAVAILABLE')
    if evidence.safety_status == 'FAILED' and (not _seconds(baseline.previous_duration_seconds) or
            not _finite(baseline.previous_temperature_c) or baseline.previous_temperature_c <= 0):
        unknown.append('PREVIOUS_SAFETY_BASELINE_UNAVAILABLE')
    return unknown


def _decimal(value):
    if type(value) is int:
        value = Decimal(value)
    if not _finite(value):
        raise ValueError('finite numeric proposal required')
    return value


def proposal(value, evidence: Evidence, baseline: CombiBaseline):
    """Return a checked candidate copy. Unknown members are always invalid."""
    if set(value) != set(fields(baseline.menu)):
        raise ValueError('unexpected combi proposal fields')
    decimals = {'temperature_c', 'climate_setpoint_percent'}
    if baseline.menu == 'RICE':
        decimals.add('effective_water_ratio')
    candidate = {}
    for name, raw in value.items():
        if name in decimals:
            candidate[name] = str(_decimal(raw))
        elif name != 'pan_positions':
            if type(raw) is not int:
                raise ValueError('integer proposal required')
            candidate[name] = raw
    positions = value['pan_positions']
    if not isinstance(positions, list) or not 1 <= len(positions) <= 20 or any(
            type(p) is not dict or set(p) != {'rack', 'shelf'} or
            any(type(p[k]) is not int for k in ('rack', 'shelf')) for p in positions):
        raise ValueError('structured pan positions required')
    candidate['pan_positions'] = [dict(p) for p in positions]
    unknown = _baseline(evidence, baseline)
    if unknown:
        return GuardResult('INDETERMINATE', tuple(unknown), RULE_VERSION), {}
    reasons = []
    rice = baseline.menu == 'RICE'
    temperature = _decimal(value['temperature_c'])
    if temperature != (100 if rice else 220) or _decimal(value['climate_setpoint_percent']) != (100 if rice else 10) or \
            value['fan_level'] != (4 if rice else 3):
        reasons.append('UNAPPROVED_PROGRAMME_SETTINGS')
    earliest, *_, maximum = TIME_BANDS[baseline.menu]
    if not earliest <= value['duration_seconds'] <= maximum:
        reasons.append('DURATION_OUTSIDE_PROCESS_LIMITS')
    if len(positions) != baseline.pan_count or len({(p['rack'],p['shelf']) for p in positions}) != len(positions) or \
            any(not 1 <= p['rack'] <= baseline.rack_count or not 1 <= p['shelf'] <= 10 for p in positions):
        reasons.append('PAN_LAYOUT_OUTSIDE_PROFILE')
    if evidence.safety_status == 'FAILED':
        if value['duration_seconds'] < baseline.previous_duration_seconds:
            reasons.append('SHORTENING_AFTER_SAFETY_FAILURE')
        if temperature < baseline.previous_temperature_c:
            reasons.append('LOWERING_TEMPERATURE_AFTER_SAFETY_FAILURE')
    if rice:
        ratio = _decimal(value['effective_water_ratio'])
        if not Decimal('1.15') <= ratio <= Decimal('1.25'):
            reasons.append('EFFECTIVE_WATER_RATIO_OUTSIDE_RANGE')
        else:
            try:
                added = baseline.dry_rice_kg * ratio - (baseline.drained_rice_kg - baseline.dry_rice_kg)
                if added < 0:
                    reasons.append('NEGATIVE_ADDED_WATER')
                load = oven_load(baseline.size, baseline.rack_count, baseline.pan_count,
                    baseline.dry_rice_kg * (1 + ratio), baseline.pan_tare_kg, baseline.rack_tare_kg)
                reasons.extend(load.violations)
            except (ValueError, ArithmeticError):
                reasons.append('CANDIDATE_LOAD_UNAVAILABLE')
        if value['soak_seconds'] != 1800 or value['drain_seconds'] != 300:
            reasons.append('UNAPPROVED_PREPARATION_TIMES')
    elif not (30 if baseline.humidity_available else 180) <= value['transition_duration_seconds'] <= 300:
        reasons.append('TRANSITION_DURATION_OUTSIDE_LIMITS')
    result = GuardResult('REJECTED' if reasons else 'WITHIN_ENCODED_LIMITS', tuple(dict.fromkeys(reasons)), RULE_VERSION)
    return result, candidate if not reasons else {}
