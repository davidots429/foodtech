"""Current recorded quality facts; no free text, authors or authority assertions."""
from datetime import datetime, timedelta
from decimal import Decimal
import re

from .quality_score import SCORE_ITEMS, summarize_quality_scores


def _integer(value, low=1, high=10**18):
    if type(value) not in (int, Decimal) or (type(value) is Decimal and not value.is_finite()) or \
            not low <= value <= high or value != int(value):
        raise ValueError('invalid recorded quality integer')
    return int(value)


def _instant(value):
    if type(value) is not str:
        raise ValueError('recorded quality time required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() != timedelta(0):
        raise ValueError('recorded UTC quality time required')
    return result


def observe_quality(rows, step, meta, menu, *, now):
    """Return immutable allowlisted fields from the current completed assessment.

    Safety invalidation never restores a previous usable pointer automatically.
    Caller already verifies owned, stable rows, safety and source lineage.
    """
    if step.get('safety_status') != 'PASSED':
        return (('status', 'BLOCKED_BY_SAFETY'),)
    pointer = step.get('quality_current')
    if type(pointer) is not dict or pointer.get('usable') is not True:
        return (('status', 'UNAVAILABLE'),)
    version = _integer(pointer.get('version'))
    safety_version = _integer(pointer.get('safety_version'))
    key = pointer.get('event_sk')
    if type(key) is not str or not re.fullmatch('EVENT#[0-9]{12,19}', key) or \
            key != f'EVENT#{int(key[6:]):012d}' or \
            not 1 <= int(key[6:]) <= _integer(meta['version']) or \
            version != _integer(step.get('quality_version')) or safety_version != _integer(step.get('safety_version')):
        raise ValueError('recorded quality pointer mismatch')
    event = rows.get(key)
    if type(event) is not dict or event.get('event') != menu + '_QUALITY_ASSESSED':
        raise ValueError('recorded quality event unavailable')
    a = event.get('assessment')
    if type(a) is not dict or _integer(a.get('version')) != version or \
            _integer(a.get('safety_version')) != safety_version or a.get('safety_status') != 'PASSED' or \
            a.get('menu') != menu or a.get('status') != 'COMPLETED' or \
            a.get('scale_version') != menu.lower() + '-common-v1' or a.get('formula_version') != 'quality-v1' or \
            a.get('equipment_size') != meta.get('equipment_size'):
        raise ValueError('recorded quality assessment mismatch')
    if (version == 1 and (a.get('kind') != 'INITIAL' or a.get('previous_version') is not None)) or \
            (version > 1 and (a.get('kind') not in ('REVISION', 'REASSESSMENT') or
             _integer(a.get('previous_version')) != version - 1 or type(a.get('reason')) is not str or
             not a['reason'].strip() or len(a['reason']) > 500)):
        raise ValueError('recorded quality revision mismatch')
    completed = _instant(a.get('completed_at'))
    if not _instant(step['food_unloaded_at']) <= completed <= now or \
            _instant(event.get('observed_at')) != completed or _instant(event.get('recorded_at')) != completed:
        raise ValueError('recorded quality interval mismatch')
    current_matches = 0
    for item in rows.values():
        other = item.get('assessment')
        if item.get('event') == menu + '_QUALITY_ASSESSED' and type(other) is dict and other.get('status') == 'COMPLETED':
            if _integer(other.get('version')) > version:
                raise ValueError('recorded quality pointer is not current')
            if _integer(other.get('version')) == version:
                current_matches += 1
    if current_matches != 1:
        raise ValueError('ambiguous recorded quality version')
    scores = a.get('scores')
    observations = a.get('observations')
    if type(scores) is not dict or set(scores) != set(SCORE_ITEMS) or \
            type(observations) is not dict or set(observations) != set(SCORE_ITEMS) or \
            any(type(v) is not str or not v.strip() or len(v) > 500 for v in observations.values()):
        raise ValueError('complete recorded quality scores and observations required')
    values, total, percent = summarize_quality_scores(scores)
    reasons = a.get('quality_reason_codes')
    allowed = {'UNDERCOOKED', 'EXCESS_MOISTURE', 'OVERCOOKED', 'UNEVEN_COOK', 'BAD_SMELL'} | (
        {'BURNT_BOTTOM', 'RICE_CLUMPED'} if menu == 'RICE' else {'BURNT_SURFACE', 'DRY_TEXTURE', 'SKIN_NOT_CRISP'})
    passed, target = a.get('quality_passed'), a.get('quality_target')
    if type(reasons) is not list or any(type(v) is not str for v in reasons) or \
            len(reasons) != len(set(reasons)) or set(reasons) - allowed or \
            type(passed) is not bool or type(target) is not bool or passed != (not reasons) or \
            (passed and (min(v for _, v in values) < 3 or percent < 60)) or \
            target != (passed and min(v for _, v in values) >= 4 and percent >= 80) or \
            _integer(a.get('score_total'), 7, 35) != total or \
            type(a.get('score_percent')) not in (str, Decimal) or Decimal(a['score_percent']) != percent:
        raise ValueError('recorded quality outcome mismatch')
    status = 'QUALITY_PASSED' if passed else 'QUALITY_FAILED'
    if any(obj.get('quality_status') != status for obj in (step, pointer, a)) or \
            pointer.get('quality_target') is not target or type(pointer.get('score_percent')) not in (str, Decimal) or \
            Decimal(pointer['score_percent']) != percent:
        raise ValueError('recorded quality summary mismatch')
    planned = _integer(a.get('planned_portions'), 1, 1000)
    actual = _integer(a.get('actual_portions'), 1, planned)
    if menu == 'MACKEREL' and actual > len(step.get('products', [])):
        raise ValueError('recorded quality portions exceed products')
    satisfaction = a.get('overall_satisfaction')
    if satisfaction is not None:
        satisfaction = _integer(satisfaction, 1, 5)
    return (('status', 'CURRENT'), ('version', version), ('safety_version', safety_version),
            ('scale_version', a['scale_version']), ('scores', values), ('score_percent', str(percent)),
            ('quality_status', status), ('quality_target', target), ('reason_codes', tuple(reasons)),
            ('planned_portions', planned), ('actual_portions', actual), ('overall_satisfaction', satisfaction))
