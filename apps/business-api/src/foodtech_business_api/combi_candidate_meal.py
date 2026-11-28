"""Fresh candidate meal planning; creation and transition enforcement are separate.

These records contain adopted programme intent, not measured food safety or an
instruction to equipment. The write path must atomically fence the source review
and candidate, and must not expose this builder as an unchecked public create.
"""
import copy
from decimal import Decimal
import hashlib
from typing import Literal
from uuid import UUID, uuid5
import rfc8785
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, UUID4, model_validator
from foodtech_contracts.combi_cooking import TIME_BANDS
from .combi_meal import CreateMeal, new_meal_records
from .combi_recipe_candidate import validate_combi_candidate, _instant


class FreshCombiMeal(CreateMeal):
    rack_count: StrictInt = Field(ge=1, le=2)
    pan_count: StrictInt = Field(ge=1, le=20)



class PanPosition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rack: StrictInt = Field(ge=1, le=2)
    shelf: StrictInt = Field(ge=1, le=10)


class CandidateMealRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID4
    meal_id: UUID
    menu: Literal['RICE', 'MACKEREL']
    expected_meal_version: StrictInt = Field(ge=1, le=2**53-1)
    proposal_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed_pan_positions: list[PanPosition] = Field(min_length=1, max_length=20)
    new_meal: FreshCombiMeal
    acknowledged: StrictBool

    @model_validator(mode='after')
    def explicit_fresh_selection(self):
        positions = [(p.rack, p.shelf) for p in self.confirmed_pan_positions]
        if not self.acknowledged or self.request_id == self.meal_id or len(set(positions)) != len(positions) or \
                getattr(self.new_meal, self.menu.lower() + '_recipe_version') is not None:
            raise ValueError('explicit fresh meal and unique confirmed layout required')
        return self


def request_hash(body, candidate):
    values = body.model_dump(mode='json')
    for name in ('dry_rice_kg','drained_rice_kg','added_water_kg','pan_tare_kg','rack_tare_kg'):
        values['new_meal'][name] = format(getattr(body.new_meal,name).normalize(),'f')
    values['confirmed_pan_positions'] = sorted(values['confirmed_pan_positions'],key=lambda p:(p['rack'],p['shelf']))
    return hashlib.sha256(rfc8785.dumps({'schema_version':'combi-candidate-meal-request-v1',
        'candidate':candidate, 'input':values})).hexdigest()


def _programme(candidate, body):
    menu, data = candidate['menu'], candidate['proposal']['data']
    rice = menu == 'RICE'
    if (Decimal(data['temperature_c']), Decimal(data['climate_setpoint_percent']), data['fan_level']) != \
            (Decimal(100 if rice else 220), Decimal(100 if rice else 10), 4 if rice else 3) or \
            not TIME_BANDS[menu][0] <= data['duration_seconds'] <= TIME_BANDS[menu][-1]:
        raise ValueError('candidate programme is outside approved limits')
    if candidate['equipment_size'] != body.new_meal.equipment_size:
        raise ValueError('candidate equipment size differs from the new batch')
    maximum_racks = 1 if body.new_meal.equipment_size == 'MEDIUM_FIXED_RACK_10' else 2
    positions = data['pan_positions']
    if sorted((p['rack'],p['shelf']) for p in positions) != sorted((p.rack,p.shelf) for p in body.confirmed_pan_positions) or \
            any(p['rack'] > maximum_racks for p in positions):
        raise ValueError('confirmed layout differs from the candidate or profile')
    if rice:
        if (not Decimal('1.15') <= Decimal(data['effective_water_ratio']) <= Decimal('1.25') or
                data['soak_seconds'] != 1800 or data['drain_seconds'] != 300 or
                len(positions) != body.new_meal.pan_count or any(p['rack'] > body.new_meal.rack_count for p in positions)):
            raise ValueError('rice candidate preparation or layout is outside approved limits')
    elif not 30 <= data['transition_duration_seconds'] <= 300:
        raise ValueError('candidate transition duration is outside approved limits')
    return data


def candidate_meal_records(body, candidate, review, now):
    """Plan four fresh rows; caller must fence source/target/listing/history atomically."""
    if type(body) is not CandidateMealRequest:
        raise ValueError('validated fresh candidate meal request required')
    candidate = validate_combi_candidate(candidate, review)
    if (candidate['meal_id'],candidate['menu'],candidate['job_version'],candidate['proposal_digest']) != \
            (str(body.meal_id),body.menu,body.expected_meal_version,body.proposal_digest):
        raise ValueError('candidate source and request differ')
    if _instant(now) < _instant(candidate['recorded_at']):
        raise ValueError('new meal predates the preserved candidate')
    data = _programme(candidate, body)
    namespace = body.request_id
    identifiers = (str(namespace), *(str(uuid5(namespace,name)) for name in ('rice-job','mode-transition','mackerel-job')))
    ratio = Decimal(data['effective_water_ratio']) if body.menu == 'RICE' else Decimal('1.20')
    records = new_meal_records(body.new_meal, identifiers, now, water_target_ratio=ratio)
    meta, rice, transition, fish = records
    selected = rice if body.menu == 'RICE' else fish
    selected.update(program_celsius=data['temperature_c'],climate_setpoint_percent=data['climate_setpoint_percent'],
        fan_level=data['fan_level'],target_program_seconds=data['duration_seconds'],
        planned_pan_positions=copy.deepcopy(data['pan_positions']))
    if body.menu == 'RICE':
        if rice['target_added_water_kg'] is None:
            raise ValueError('fresh drained rice cannot achieve the selected target ratio')
        rice.update(target_effective_water_ratio=data['effective_water_ratio'],
                    target_soaking_seconds=data['soak_seconds'],target_draining_seconds=data['drain_seconds'])
    else:
        transition['target_duration_seconds'] = data['transition_duration_seconds']
    meta[body.menu.lower()+'_recipe_version'] = candidate['candidate_id']
    meta['recipe_candidate_selection'] = {'schema_version':'combi-candidate-selection-v1',
        'request_id':str(body.request_id),'request_hash':request_hash(body,candidate),
        'source_meal_id':candidate['meal_id'],'source_meal_version':candidate['job_version'],
        'source_job_id':candidate['job_id'],'menu':body.menu,'review_version':candidate['review_version'],
        'candidate':copy.deepcopy(candidate),'review':copy.deepcopy(review)}
    return records
