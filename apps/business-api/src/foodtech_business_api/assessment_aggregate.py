"""Immutable descriptive statistics for an explicit synthetic job sample."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .cooking import SCORE_ITEMS, _context, _pk, _store_call
from .cooking_store import Conflict

router = APIRouter(prefix="/api/customer/assessment-aggregates", tags=["assessment-aggregate"])
FORMULA_VERSION = "tonkatsu-selected-sample-v1"
MAX_SAMPLE = 20


class AggregateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_ids: list[UUID] = Field(min_length=1, max_length=MAX_SAMPLE)
    period_start: datetime
    period_end: datetime
    recipe_version: str = Field(min_length=1, max_length=80)
    load_band: Literal["PARTIAL", "REFERENCE"]


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _percent(numerator: int, denominator: int) -> str | None:
    if not denominator:
        return None
    return str((Decimal(numerator) / Decimal(denominator) * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))


def _reason(meta: dict, pointer: dict | None, assessment: dict | None,
            *, recipe: str, load_band: str, start: datetime, end: datetime) -> str | None:
    if meta["menu"] != "TONKATSU" or meta["equipment_profile"] != "synthetic-fryer" or \
            meta["recipe_version"] != recipe:
        return "DIMENSION_MISMATCH"
    if meta["status"] != "COMPLETED":
        return "JOB_NOT_COMPLETED"
    if meta["safety_status"] != "PASSED":
        return "SAFETY_NOT_PASSED"
    if meta["evaluation_blocked"]:
        return "EVALUATION_BLOCKED"
    if not pointer:
        return "NO_CURRENT_ASSESSMENT"
    if not pointer["usable"] or int(pointer["safety_version"]) != int(meta["safety_version"]):
        return "CURRENT_ASSESSMENT_BLOCKED"
    if not assessment or assessment["status"] != "COMPLETED" or \
            assessment["sk"] != pointer["assessment_sk"]:
        return "NO_COMPLETED_ASSESSMENT"
    if assessment["scale_version"] != "tonkatsu-common-v1" or assessment["menu"] != meta["menu"] or \
            assessment["equipment_profile"] != meta["equipment_profile"]:
        return "SCALE_OR_PROFILE_MISMATCH"
    if assessment["load_band"] != load_band:
        return "LOAD_BAND_MISMATCH"
    try:
        completed_at = datetime.fromisoformat(assessment["completed_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return "INVALID_COMPLETION_TIME"
    if completed_at.tzinfo is None or not start <= completed_at.astimezone(timezone.utc) < end:
        return "OUTSIDE_PERIOD"
    if set(assessment["scores"]) != set(SCORE_ITEMS) or any(
            type(score) is not int or not 1 <= score <= 5 for score in assessment["scores"].values()):
        return "INVALID_SCORE_SET"
    return None


@router.post("", status_code=201)
async def create_aggregate(request: Request, body: AggregateRequest) -> dict:
    if body.period_start.tzinfo is None or body.period_end.tzinfo is None:
        raise HTTPException(422, "집계 기간에는 시간대가 필요합니다.")
    start = body.period_start.astimezone(timezone.utc)
    end = body.period_end.astimezone(timezone.utc)
    if not start < end <= start + timedelta(days=366):
        raise HTTPException(422, "집계 기간은 0일 초과 366일 이하여야 합니다.")
    ids = sorted(str(value) for value in body.job_ids)
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "같은 조리작업을 중복 선택할 수 없습니다.")
    recipe = body.recipe_version.strip()
    if not recipe:
        raise HTTPException(422, "레시피 버전을 입력해야 합니다.")
    session, store = await _context(request, write=True)
    selection = {"job_ids": ids, "period_start": start.isoformat(), "period_end": end.isoformat(),
                 "menu": "TONKATSU", "equipment_profile": "synthetic-fryer",
                 "recipe_version": recipe, "scale_version": "tonkatsu-common-v1",
                 "load_band": body.load_band}
    group_id = _hash(selection)
    snapshots: list[tuple[str, dict, dict | None]] = []
    references = []
    included = []
    for job_id in ids:
        pk = _pk(session.subject, job_id)
        meta, pointer = await _store_call(store, store.read, pk)
        if not meta:
            raise HTTPException(404, "선택한 조리작업을 찾을 수 없습니다.")
        assessment = (await _store_call(store, store.assessment, pk, int(pointer["version"]))
                      if pointer else None)
        reason = _reason(meta, pointer, assessment, recipe=recipe, load_band=body.load_band,
                         start=start, end=end)
        snapshots.append((pk, meta, pointer))
        reference = {"job_id": job_id, "job_version": int(meta["version"]),
                     "safety_version": int(meta["safety_version"]),
                     "assessment_version": int(pointer["version"]) if pointer else None,
                     "included": reason is None, "exclusion_reason": reason}
        references.append(reference)
        if reason is None:
            included.append(assessment)
    result_id = _hash({"selection": selection, "references": references,
                       "formula_version": FORMULA_VERSION})
    count = len(included)
    excluded = [{"job_id": ref["job_id"], "reason": ref["exclusion_reason"]}
                for ref in references if not ref["included"]]
    score_sum = sum((Decimal(item["score_percent"]) for item in included), Decimal("0"))
    mean = str((score_sum / Decimal(count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if count else None
    item_means = ({name: str((Decimal(sum(item["scores"][name] for item in included)) / Decimal(count))
                             .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) for name in SCORE_ITEMS}
                  if count else None)
    record = {"group_id": group_id, "result_id": result_id, "formula_version": FORMULA_VERSION,
              "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "scope": "SELECTED_SAMPLE", "selection": selection, "references": references,
              "sample_count": count, "excluded_count": len(excluded), "excluded": excluded,
              "mean_quality_score_percent": mean, "item_score_means": item_means,
              "quality_pass_rate_percent": _percent(sum(bool(item["quality_passed"]) for item in included), count),
              "quality_target_rate_percent": _percent(sum(bool(item["quality_target"]) for item in included), count),
              "representativeness_status": "NOT_EVALUATED",
              "recipe_improvement_status": "NOT_EVALUATED", "provenance": "SYNTHETIC"}
    analysis_pk = f"SCOPE#USER#{session.subject}#AGGREGATE#{group_id}"
    try:
        return await _store_call(store, store.save_aggregate, analysis_pk, snapshots, record)
    except Conflict as exc:
        raise HTTPException(409, "집계 중 평가 또는 안전 상태가 변경되었습니다. 다시 계산해 주세요.") from exc


@router.get("/{group_id}/results/{result_id}")
async def get_aggregate(group_id: str, result_id: str, request: Request) -> dict:
    if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
           for value in (group_id, result_id)):
        raise HTTPException(400, "집계 기록 ID가 올바르지 않습니다.")
    session, store = await _context(request)
    pk = f"SCOPE#USER#{session.subject}#AGGREGATE#{group_id}"
    record = await _store_call(store, store.aggregate, pk, result_id)
    if not record:
        raise HTTPException(404, "집계 기록을 찾을 수 없습니다.")
    return record
