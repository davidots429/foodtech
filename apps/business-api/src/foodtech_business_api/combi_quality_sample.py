"""Read the current synthetic meal quality assessment for selected-sample analysis.

The caller supplies the customer-scoped, freshness-checked meal view. This
extractor does not infer recipe versions or load bands absent from the record.
"""
from decimal import Decimal

from .cooking import SCORE_ITEMS


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or \
            (isinstance(value, Decimal) and not value.is_finite()) or int(value) != value:
        raise ValueError('invalid numeric value')
    return int(value)


def current_quality(meal_view: dict, menu: str) -> dict:
    if menu not in {'RICE', 'MACKEREL'} or not isinstance(meal_view, dict):
        raise ValueError('invalid meal quality scope')
    meta, step = meal_view.get('meal'), meal_view.get(menu.lower())
    if not isinstance(meta, dict) or not isinstance(step, dict):
        raise ValueError('meal quality state unavailable')
    pointer = step.get('quality_current')
    if not pointer:
        return {'status': 'EXCLUDED', 'reason': 'NO_CURRENT_ASSESSMENT'}
    if not isinstance(pointer, dict):
        raise ValueError('invalid meal quality pointer')
    if step.get('status') != 'UNLOADED' or step.get('safety_status') != 'PASSED' or pointer.get('usable') is not True:
        return {'status': 'EXCLUDED', 'reason': 'CURRENT_ASSESSMENT_BLOCKED'}
    version, safety_version = _integer(pointer.get('version')), _integer(pointer.get('safety_version'))
    if version < 1 or safety_version < 0 or \
            safety_version != _integer(step.get('safety_version', 0)) or version != _integer(step.get('quality_version')):
        raise ValueError('invalid meal quality version')
    event_key = pointer.get('event_sk')
    events = meal_view.get('events', [])
    if not isinstance(event_key, str) or not isinstance(events, list):
        raise ValueError('invalid meal quality history')
    matches = [event for event in events if isinstance(event, dict) and event.get('sk') == event_key]
    if len(matches) != 1:
        raise ValueError('meal quality history unavailable')
    event = matches[0]
    assessment = event.get('assessment')
    if event.get('event') != f'{menu}_QUALITY_ASSESSED' or not isinstance(assessment, dict) or \
            assessment.get('version') != version or assessment.get('safety_version') != safety_version or \
            assessment.get('menu') != menu or assessment.get('status') != 'COMPLETED' or \
            assessment.get('scale_version') != f'{menu.lower()}-common-v1' or \
            assessment.get('quality_status') != pointer.get('quality_status') or \
            assessment.get('score_percent') != pointer.get('score_percent') or \
            assessment.get('quality_target') is not pointer.get('quality_target'):
        raise ValueError('meal quality pointer and history differ')
    scores = assessment.get('scores')
    if not isinstance(scores, dict) or set(scores) != set(SCORE_ITEMS):
        raise ValueError('invalid meal quality scores')
    values = [_integer(value) for value in scores.values()]
    if any(not 1 <= value <= 5 for value in values):
        raise ValueError('invalid meal quality scores')
    return {'status': 'ELIGIBLE', 'assessment': assessment.copy(),
            'meal_version': meta.get('version'), 'quality_version': version, 'safety_version': safety_version}
