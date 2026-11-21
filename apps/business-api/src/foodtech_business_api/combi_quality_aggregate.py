"""Immutable, customer-selected synthetic meal quality snapshots."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from foodtech_contracts.combi_quality_load import RULE_VERSION, classify

from .assessment_aggregate import _hash, _percent, _valid_hash
from .combi_meal import _pk, get_meal
from .combi_quality_sample import current_quality
from .cooking import SCORE_ITEMS, _context, _store_call
from .cooking_store import Conflict

router = APIRouter(prefix='/api/customer/combi-quality-aggregates', tags=['combi-quality-aggregate'])
FORMULA_VERSION = 'combi-selected-sample-v1'


class MealAggregateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    meal_ids: list[UUID] = Field(min_length=1, max_length=20)
    menu: Literal['RICE', 'MACKEREL']
    equipment_size: Literal['MEDIUM_FIXED_RACK_10', 'LARGE_ROLL_IN_RACK_20']
    recipe_version: str = Field(min_length=1, max_length=80)
    load_band: Literal['PARTIAL', 'REFERENCE', 'OVER_RECOMMENDED']
    period_start: datetime
    period_end: datetime


@router.post('', status_code=201)
async def create_meal_aggregate(request: Request, body: MealAggregateRequest) -> dict:
    session, store = await _context(request, write=True)
    if body.period_start.utcoffset() is None or body.period_end.utcoffset() is None:
        raise HTTPException(422, '집계 기간에는 시간대가 필요합니다.')
    start, end = body.period_start.astimezone(timezone.utc), body.period_end.astimezone(timezone.utc)
    if not start < end <= start + timedelta(days=366):
        raise HTTPException(422, '집계 기간은 0일 초과 366일 이하여야 합니다.')
    meal_ids = sorted(str(meal_id) for meal_id in body.meal_ids)
    if len(set(meal_ids)) != len(meal_ids) or not body.recipe_version.strip():
        raise HTTPException(422, '중복 배치 또는 빈 레시피 버전을 확인해 주세요.')
    selection = {'meal_ids': meal_ids, 'menu': body.menu, 'equipment_size': body.equipment_size,
                 'recipe_version': body.recipe_version.strip(), 'load_band': body.load_band,
                 'period_start': start.isoformat(), 'period_end': end.isoformat(), 'load_rule_version': RULE_VERSION}
    snapshots, references, included = [], [], []
    for meal_id in meal_ids:
        first = await get_meal(UUID(meal_id), request)
        view = await get_meal(UUID(meal_id), request)
        if first != view:
            raise HTTPException(409, '조회 중 식사 배치가 변경되었습니다. 다시 집계해 주세요.')
        meta = view['meal']
        try:
            version = int(meta['version'])
            if isinstance(meta['version'], bool) or version < 1 or version != meta['version']:
                raise ValueError
            sample = current_quality(view, body.menu)
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(409, '현재 품질평가 이력을 확인할 수 없습니다.') from exc
        snapshots.append((_pk(session.subject, meal_id), version))
        reason = None
        assessment = sample.get('assessment')
        if sample['status'] == 'EXCLUDED':
            reason = sample['reason']
        elif not meta.get(f'{body.menu.lower()}_recipe_version'):
            reason = 'MISSING_RECIPE_VERSION'
        elif assessment.get('actual_mass_kg') is None:
            reason = 'MISSING_ACTUAL_MASS'
        elif meta.get('equipment_size') != body.equipment_size or \
                meta.get(f'{body.menu.lower()}_recipe_version') != selection['recipe_version']:
            reason = 'DIMENSION_MISMATCH'
        else:
            try:
                load = classify(body.menu, meta['equipment_size'], assessment['actual_mass_kg'])
                completed = datetime.fromisoformat(assessment['completed_at'].replace('Z', '+00:00'))
                if completed.utcoffset() is None:
                    raise ValueError
                if type(assessment.get('quality_passed')) is not bool or type(assessment.get('quality_target')) is not bool:
                    raise ValueError
                score = Decimal(assessment['score_percent'])
                if not score.is_finite() or not 0 <= score <= 100:
                    raise ValueError
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise HTTPException(409, '평가의 조리량 또는 완료 정보를 확인할 수 없습니다.') from exc
            if load.band != body.load_band:
                reason = 'LOAD_BAND_MISMATCH'
            elif not start <= completed.astimezone(timezone.utc) < end:
                reason = 'OUTSIDE_PERIOD'
        references.append({'meal_id': meal_id, 'meal_version': version,
            'quality_version': sample.get('quality_version'), 'safety_version': sample.get('safety_version'),
            'included': reason is None, 'exclusion_reason': reason})
        if reason is None:
            included.append(assessment)
    group_id = _hash(selection)
    result_id = _hash({'selection': selection, 'references': references, 'formula_version': FORMULA_VERSION})
    count = len(included)
    mean = (str((sum((Decimal(item['score_percent']) for item in included), Decimal(0)) / count)
                .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) if count else None)
    item_means = ({name: str((sum(Decimal(item['scores'][name]) for item in included) / count)
                              .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) for name in SCORE_ITEMS}
                  if count else None)
    record = {'group_id': group_id, 'result_id': result_id, 'formula_version': FORMULA_VERSION,
              'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'scope': 'SELECTED_SAMPLE',
              'snapshot_status': 'HISTORICAL', 'selection': selection, 'references': references,
              'sample_count': count, 'excluded_count': len(references) - count,
              'mean_quality_score_percent': mean, 'item_score_means': item_means,
              'quality_pass_rate_percent': _percent(sum(item['quality_passed'] for item in included), count),
              'quality_target_rate_percent': _percent(sum(item['quality_target'] for item in included), count),
              'representativeness_status': 'NOT_EVALUATED', 'provenance': 'SYNTHETIC'}
    pk = f'SCOPE#USER#{session.subject}#MEAL_AGGREGATE#{group_id}'
    try:
        return await _store_call(store, store.save_meal_aggregate, pk, snapshots, record)
    except Conflict as exc:
        raise HTTPException(409, '집계 중 식사 배치가 변경되었습니다. 다시 계산해 주세요.') from exc


@router.get('/{group_id}/{result_id}')
async def get_meal_aggregate(group_id: str, result_id: str, request: Request) -> dict:
    if not _valid_hash(group_id) or not _valid_hash(result_id):
        raise HTTPException(400, '집계 기록 ID가 올바르지 않습니다.')
    session, store = await _context(request)
    pk = f'SCOPE#USER#{session.subject}#MEAL_AGGREGATE#{group_id}'
    result = await _store_call(store, store.aggregate, pk, result_id)
    if not result or result.get('group_id') != group_id or result.get('result_id') != result_id:
        raise HTTPException(404, '집계 기록을 찾을 수 없습니다.')
    return result


@router.get('/{group_id}/{result_id}/freshness')
async def meal_aggregate_freshness(group_id: str, result_id: str, request: Request) -> dict:
    """Compare captured versions with current customer-owned META records."""
    record = await get_meal_aggregate(group_id, result_id, request)
    session, store = await _context(request)
    try:
        selection, references = record['selection'], record['references']
        if not isinstance(selection, dict) or not isinstance(references, list) or not 1 <= len(references) <= 20 or \
                selection.get('meal_ids') != [ref['meal_id'] for ref in references] or \
                _hash(selection) != group_id or record.get('formula_version') != FORMULA_VERSION or \
                _hash({'selection': selection, 'references': references, 'formula_version': FORMULA_VERSION}) != result_id:
            raise ValueError
        seen = set()
        for ref in references:
            meal_id, version = ref['meal_id'], ref['meal_version']
            if not isinstance(meal_id, str) or str(UUID(meal_id)) != meal_id or meal_id in seen or \
                    type(version) is not int or version < 1:
                raise ValueError
            seen.add(meal_id)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(409, '집계의 배치 참조를 확인할 수 없습니다.') from None
    changed = []
    for ref in references:
        meta = await _store_call(store, store.read_meta, _pk(session.subject, ref['meal_id']))
        if not meta:
            changed.append({'meal_id': ref['meal_id'], 'reason': 'UNAVAILABLE'})
        elif meta.get('version') != ref['meal_version']:
            changed.append({'meal_id': ref['meal_id'], 'reason': 'VERSION_CHANGED'})
    return {'status': 'CHANGED' if changed else 'UNCHANGED_AS_OBSERVED', 'changed': changed,
            'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
