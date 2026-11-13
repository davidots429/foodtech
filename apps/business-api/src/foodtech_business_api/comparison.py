"""Versioned comparison of two currently usable cooking assessments."""

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .cooking import SCORE_ITEMS, _read, _store_call
from .cooking_store import Conflict

router = APIRouter(prefix="/api/customer/cooking-jobs", tags=["assessment-comparison"])


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_job_id: UUID


def _usable(meta: dict, pointer: dict | None) -> bool:
    return bool(meta["status"] == "COMPLETED" and meta["safety_status"] == "PASSED" and
                not meta["evaluation_blocked"] and pointer and pointer["usable"] and
                int(pointer["safety_version"]) == int(meta["safety_version"]))


@router.post("/{candidate_job_id}/comparisons", status_code=201)
async def compare(candidate_job_id: UUID, request: Request, body: ComparisonRequest) -> dict:
    candidate_id = str(candidate_job_id)
    baseline_id = str(body.baseline_job_id)
    if baseline_id == candidate_id:
        raise HTTPException(422, "서로 다른 두 조리작업을 선택해야 합니다.")
    _, store, candidate_pk, candidate_meta, candidate_pointer = await _read(
        request, candidate_id, write=True)
    _, _, baseline_pk, baseline_meta, baseline_pointer = await _read(request, baseline_id)
    if not _usable(candidate_meta, candidate_pointer) or not _usable(baseline_meta, baseline_pointer):
        raise HTTPException(409, "두 작업 모두 현재 사용 가능한 완료 평가와 안전 통과 상태가 필요합니다.")
    assert candidate_pointer is not None and baseline_pointer is not None
    baseline = await _store_call(store, store.assessment, baseline_pk, int(baseline_pointer["version"]))
    candidate = await _store_call(store, store.assessment, candidate_pk, int(candidate_pointer["version"]))
    if not baseline or not candidate or baseline["status"] != "COMPLETED" or candidate["status"] != "COMPLETED" or \
            baseline["sk"] != baseline_pointer["assessment_sk"] or \
            candidate["sk"] != candidate_pointer["assessment_sk"]:
        raise HTTPException(409, "현재 평가 기록이 변경되었습니다. 다시 조회해 주세요.")
    if baseline_meta["menu"] != baseline["menu"] or candidate_meta["menu"] != candidate["menu"] or \
            baseline_meta["equipment_profile"] != baseline["equipment_profile"] or \
            candidate_meta["equipment_profile"] != candidate["equipment_profile"] or \
            (baseline_meta["menu"], baseline["scale_version"], baseline["equipment_profile"]) != \
            (candidate_meta["menu"], candidate["scale_version"], candidate["equipment_profile"]) or \
            set(baseline["scores"]) != set(SCORE_ITEMS) or set(candidate["scores"]) != set(SCORE_ITEMS) or \
            baseline["load_band"] not in {"PARTIAL", "REFERENCE"} or \
            candidate["load_band"] not in {"PARTIAL", "REFERENCE"}:
        raise HTTPException(422, "메뉴·평가척도·장비와 조리량 구간이 비교 기준에 맞지 않습니다.")
    deltas = {name: int(candidate["scores"][name]) - int(baseline["scores"][name]) for name in SCORE_ITEMS}
    total_delta = sum(deltas.values())
    selector = {"baseline_job_id": baseline_id, "candidate_job_id": candidate_id,
                "baseline_job_version": int(baseline_meta["version"]),
                "candidate_job_version": int(candidate_meta["version"]),
                "baseline_assessment_version": int(baseline_pointer["version"]),
                "candidate_assessment_version": int(candidate_pointer["version"]),
                "baseline_safety_version": int(baseline_meta["safety_version"]),
                "candidate_safety_version": int(candidate_meta["safety_version"])}
    comparison_id = hashlib.sha256(json.dumps(selector, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = {"comparison_id": comparison_id, **selector,
              "derivation_version": "individual-improvement-v1",
              "compared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "menu": candidate_meta["menu"], "equipment_profile": candidate["equipment_profile"],
              "scale_version": candidate["scale_version"],
              "baseline_recipe_version": baseline_meta["recipe_version"],
              "candidate_recipe_version": candidate_meta["recipe_version"],
              "baseline_load_band": baseline["load_band"], "candidate_load_band": candidate["load_band"],
              "baseline_actual_portions": baseline["actual_portions"],
              "candidate_actual_portions": candidate["actual_portions"],
              "baseline_actual_mass_kg": baseline["actual_mass_kg"],
              "candidate_actual_mass_kg": candidate["actual_mass_kg"],
              "baseline_load_percent": baseline["load_percent"],
              "candidate_load_percent": candidate["load_percent"],
              "baseline_score_percent": baseline["score_percent"],
              "candidate_score_percent": candidate["score_percent"],
              "baseline_quality_passed": baseline["quality_passed"],
              "candidate_quality_passed": candidate["quality_passed"],
              "candidate_quality_target": candidate["quality_target"],
              "score_deltas": deltas, "score_total_delta": total_delta,
              "improvement_status": "OBSERVED" if total_delta >= 2 else "NOT_OBSERVED",
              "recipe_improvement_status": "NOT_EVALUATED", "provenance": "SYNTHETIC"}
    try:
        return await _store_call(store, store.save_comparison, baseline_pk, candidate_pk,
                                 baseline_meta, candidate_meta, baseline_pointer, candidate_pointer, record)
    except Conflict as exc:
        raise HTTPException(409, "평가 또는 안전 상태가 변경되었습니다. 다시 비교해 주세요.") from exc


@router.get("/{candidate_job_id}/comparisons/{comparison_id}")
async def get_comparison(candidate_job_id: UUID, comparison_id: str, request: Request) -> dict:
    if len(comparison_id) != 64 or any(char not in "0123456789abcdef" for char in comparison_id):
        raise HTTPException(400, "비교 기록 ID가 올바르지 않습니다.")
    _, store, pk, _, _ = await _read(request, str(candidate_job_id))
    record = await _store_call(store, store.comparison, pk, comparison_id)
    if not record:
        raise HTTPException(404, "비교 기록을 찾을 수 없습니다.")
    return record
