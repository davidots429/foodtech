"""Fresh observations must satisfy preserved candidate intent before work advances."""
from datetime import datetime, timezone
from decimal import Decimal
import re
from .combi_recipe_candidate import validate_combi_candidate
from foodtech_contracts.combi_cooking import rice_water


def selected_programme(meta, rows, menu):
    selection = meta.get('recipe_candidate_selection')
    if selection is None:
        if any('planned_pan_positions' in row or 'target_duration_seconds' in row for row in rows.values()):
            raise ValueError('candidate intent has no preserved selection')
        return None
    if type(selection) is not dict or selection.get('schema_version') != 'combi-candidate-selection-v1' or \
            selection.get('request_id') != meta['meal_batch_id'] or \
            not re.fullmatch('[a-f0-9]{64}', selection.get('request_hash', '')):
        raise ValueError('invalid preserved selection')
    candidate = validate_combi_candidate(selection['candidate'], selection['review'])
    chosen = candidate['menu']
    if (selection.get('source_meal_id'), selection.get('source_meal_version'), selection.get('source_job_id'),
            selection.get('menu'), selection.get('review_version'), meta.get('equipment_size'),
            meta.get(chosen.lower() + '_recipe_version')) != \
            (candidate['meal_id'], candidate['job_version'], candidate['job_id'], chosen, candidate['review_version'],
             candidate['equipment_size'], candidate['candidate_id']) or candidate['meal_id'] == meta['meal_batch_id']:
        raise ValueError('selection references changed')
    step = rows[f"JOB#{chosen}#{meta[chosen.lower() + '_cooking_job_id']}"]
    data = candidate['proposal']['data']
    expected = {'program_celsius': data['temperature_c'], 'climate_setpoint_percent': data['climate_setpoint_percent'],
                'fan_level': data['fan_level'], 'target_program_seconds': data['duration_seconds'],
                'planned_pan_positions': data['pan_positions']}
    if chosen == 'RICE':
        expected.update(target_effective_water_ratio=data['effective_water_ratio'],
                        target_soaking_seconds=data['soak_seconds'], target_draining_seconds=data['drain_seconds'])
    elif rows[f"TRANSITION#{meta['mode_transition_id']}"].get('target_duration_seconds') != data['transition_duration_seconds']:
        raise ValueError('transition intent changed')
    if any(step.get(key) != value for key, value in expected.items()):
        raise ValueError('programme intent changed')
    return data if chosen == menu else None


def transition_minimum(meta, rows):
    data = selected_programme(meta, rows, 'MACKEREL')
    return data['transition_duration_seconds'] if data else 30


def require_fresh_rice_preparation(meta, rows, observed):
    if selected_programme(meta, rows, 'RICE') is None:
        return
    step = rows[f"JOB#RICE#{meta['rice_cooking_job_id']}"]
    prep = step['rice_preparation']
    def instant(value):
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.utcoffset() != timezone.utc.utcoffset(result):
            raise ValueError('UTC preparation observation required')
        return result
    def number(value):
        if type(value) not in (str, int, Decimal):
            raise ValueError('finite preparation value required')
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError('finite preparation value required')
        return result
    start, soaked, drained = (instant(prep[k]) for k in ('soaking_started_at', 'soaking_completed_at', 'draining_completed_at'))
    def seconds(delta):
        return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1_000_000
    if prep.get('provenance') != 'SYNTHETIC' or not start <= soaked <= drained <= observed or \
            type(prep['wash_count']) is not int or not 0 <= prep['wash_count'] <= 20 or \
            not 0 <= number(prep['soaking_water_celsius']) <= 100 or \
            number(prep['soaking_seconds']) != seconds(soaked-start) or number(prep['draining_seconds']) != seconds(drained-soaked) or \
            any(number(prep[k]) <= 0 for k in ('washed_rice_kg','soaked_rice_kg') if prep.get(k) is not None):
        raise ValueError('fresh preparation is incomplete')
    events = [row for row in rows.values() if row.get('event') in {'RICE_PREPARATION_RECORDED','RICE_PREPARATION_REVISED'}]
    if not events:
        raise ValueError('fresh preparation audit required')
    latest = max(events, key=lambda row: row['sk'])
    if latest.get('preparation') != prep or latest.get('observed_at') != prep['draining_completed_at'] or \
            instant(latest['recorded_at']) < instant(meta['created_at']):
        raise ValueError('fresh preparation audit mismatch')
    water = rice_water(*(number(step[k]) for k in ('dry_rice_kg','drained_rice_kg','added_water_kg')),
                       target_ratio=number(step['target_effective_water_ratio']))
    if water.status != 'NORMAL' or water.target_added_water_kg is None or \
            number(step['target_added_water_kg']) != water.target_added_water_kg or \
            number(step['effective_water_ratio']) != water.effective_water_ratio:
        raise ValueError('fresh water observation unavailable')
