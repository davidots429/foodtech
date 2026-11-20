"""Immutable descriptive statistics for an explicit synthetic job sample."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .cooking import SCORE_ITEMS, _context, _pk, _read, _store_call
from .cooking_store import Conflict

router = APIRouter(prefix="/api/customer/assessment-aggregates", tags=["assessment-aggregate"])
FORMULA_VERSION = "tonkatsu-selected-sample-v1"
JEYUK_FORMULA_VERSION = "jeyuk-selected-sample-v1"
MAX_SAMPLE = 20
PROFILES = {"TONKATSU": ("synthetic-fryer", "tonkatsu-common-v1", FORMULA_VERSION),
            "JEYUK": ("jeyuk-small", "jeyuk-common-v1", JEYUK_FORMULA_VERSION)}


class AggregateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_ids: list[UUID] = Field(min_length=1, max_length=MAX_SAMPLE)
    period_start: datetime
    period_end: datetime
    recipe_version: str = Field(min_length=1, max_length=80)
    menu: Literal["TONKATSU", "JEYUK"] = "TONKATSU"
    equipment_profile: Literal["synthetic-fryer", "synthetic-fryer-approved-v2", "jeyuk-small"] | None = None
    load_band: Literal["PARTIAL", "REFERENCE", "OVER_RECOMMENDED"]


def _selection(body: AggregateRequest) -> dict:
    default_profile, scale, _ = PROFILES[body.menu]
    profile = body.equipment_profile or default_profile
    if profile not in ({"synthetic-fryer", "synthetic-fryer-approved-v2"} if body.menu == "TONKATSU" else {"jeyuk-small"}):
        raise HTTPException(422, "메뉴와 장비 프로파일이 일치하지 않습니다.")
    return {"job_ids": sorted(str(value) for value in body.job_ids),
            "period_start": body.period_start.astimezone(timezone.utc).isoformat(),
            "period_end": body.period_end.astimezone(timezone.utc).isoformat(),
            "menu": body.menu, "equipment_profile": profile,
            "recipe_version": body.recipe_version.strip(), "scale_version": scale,
            "load_band": body.load_band}


def _stored_request(selection: dict) -> AggregateRequest:
    return AggregateRequest.model_validate({key: selection[key] for key in
                                            ("job_ids", "period_start", "period_end",
                                             "recipe_version", "load_band", "menu", "equipment_profile")})


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _percent(numerator: int, denominator: int) -> str | None:
    if not denominator:
        return None
    return str((Decimal(numerator) / Decimal(denominator) * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP))


def _reason(meta: dict, pointer: dict | None, assessment: dict | None,
            *, selection: dict, start: datetime, end: datetime) -> str | None:
    if meta["menu"] != selection["menu"] or meta["equipment_profile"] != selection["equipment_profile"] or \
            meta["recipe_version"] != selection["recipe_version"]:
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
    if assessment["scale_version"] != selection["scale_version"] or assessment["menu"] != meta["menu"] or \
            assessment["equipment_profile"] != meta["equipment_profile"]:
        return "SCALE_OR_PROFILE_MISMATCH"
    if assessment["load_band"] != selection["load_band"]:
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


async def _calculate(session, store, body: AggregateRequest) -> dict:
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
    if body.menu == "TONKATSU" and body.load_band == "OVER_RECOMMENDED":
        raise HTTPException(422, "돈까스 첫 척도에는 권장량 초과 구간이 없습니다.")
    selection = _selection(body)
    _, _, formula_version = PROFILES[body.menu]
    group_id = _hash(selection)
    snapshots: list[tuple[str, dict, dict | None, dict | None]] = []
    references = []
    included = []
    for job_id in ids:
        pk = _pk(session.subject, job_id)
        meta, pointer = await _store_call(store, store.read, pk)
        if not meta:
            raise HTTPException(404, "선택한 조리작업을 찾을 수 없습니다.")
        process_pointer = await _store_call(store, store.process_pointer, pk)
        assessment = (await _store_call(store, store.assessment, pk, int(pointer["version"]))
                      if pointer else None)
        reason = _reason(meta, pointer, assessment, selection=selection, start=start, end=end)
        snapshots.append((pk, meta, pointer, process_pointer))
        reference = {"job_id": job_id, "job_version": int(meta["version"]),
                     "safety_version": int(meta["safety_version"]),
                     "job_status": meta["status"], "safety_status": meta["safety_status"],
                     "evaluation_blocked": meta["evaluation_blocked"],
                     "assessment_version": int(pointer["version"]) if pointer else None,
                     "assessment_usable": pointer["usable"] if pointer else None,
                     "assessment_safety_version": int(pointer["safety_version"]) if pointer else None,
                     "process_restatement_version": int(process_pointer["version"]) if process_pointer else None,
                     "process_result_sk": process_pointer["result_sk"] if process_pointer else None,
                     "included": reason is None, "exclusion_reason": reason}
        references.append(reference)
        if reason is None:
            included.append(assessment)
    result_id = _hash({"selection": selection, "references": references,
                       "formula_version": formula_version})
    count = len(included)
    excluded = [{"job_id": ref["job_id"], "reason": ref["exclusion_reason"]}
                for ref in references if not ref["included"]]
    score_sum = sum((Decimal(item["score_percent"]) for item in included), Decimal("0"))
    mean = str((score_sum / Decimal(count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if count else None
    item_means = ({name: str((Decimal(sum(item["scores"][name] for item in included)) / Decimal(count))
                             .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) for name in SCORE_ITEMS}
                  if count else None)
    record = {"group_id": group_id, "result_id": result_id, "formula_version": formula_version,
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


async def refresh_dependents(store, job_pk: str) -> int:
    """Recalculate saved groups that explicitly selected this job."""
    prefix = "SCOPE#USER#"
    if not job_pk.startswith(prefix) or "#JOB#" not in job_pk:
        raise ValueError("invalid aggregate dependency job scope")
    subject, job_id = job_pk[len(prefix):].rsplit("#JOB#", 1)
    if not subject or not job_id:
        raise ValueError("invalid aggregate dependency job scope")
    dependencies = await _store_call(store, store.aggregate_dependencies, job_pk)
    for dependency in dependencies:
        group_id = dependency["sk"].removeprefix("AGGREGATE#DEP#")
        selection = dependency["selection"]
        if not _valid_hash(group_id) or _hash(selection) != group_id or \
                dependency["aggregate_pk"] != f"SCOPE#USER#{subject}#AGGREGATE#{group_id}" or \
                job_id not in selection["job_ids"]:
            raise ValueError("invalid aggregate dependency")
        body = _stored_request(selection)
        result = await _calculate(SimpleNamespace(subject=subject), store, body)
        if result["group_id"] != group_id:
            raise ValueError("aggregate dependency selection changed")
    return len(dependencies)


@router.post("", status_code=201)
async def create_aggregate(request: Request, body: AggregateRequest) -> dict:
    session, store = await _context(request, write=True)
    return await _calculate(session, store, body)


def _valid_hash(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


@router.get("/jobs/{job_id}/groups")
async def list_groups_for_job(job_id: UUID, request: Request,
                              limit: int = Query(20, ge=1, le=50), cursor: str | None = None) -> dict:
    if cursor and not _valid_hash(cursor):
        raise HTTPException(400, "집계 그룹 목록 커서가 올바르지 않습니다.")
    session, store, job_pk, _, _ = await _read(request, str(job_id))
    page = await _store_call(store, store.list_aggregate_dependencies, job_pk, limit, cursor)
    groups = []
    for dependency in page["items"]:
        group_id = dependency["sk"].removeprefix("AGGREGATE#DEP#")
        selection = dependency.get("selection")
        try:
            if not isinstance(selection, dict) or not _valid_hash(group_id) or \
                    _hash(selection) != group_id or str(job_id) not in selection["job_ids"] or \
                    dependency["aggregate_pk"] != \
                    f"SCOPE#USER#{session.subject}#AGGREGATE#{group_id}":
                raise ValueError
            body = _stored_request(selection)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise HTTPException(409, "집계 그룹 연결 기록이 올바르지 않습니다.") from exc
        pointer = await _store_call(store, store.current_aggregate, dependency["aggregate_pk"])
        result_id = pointer.get("result_id") if pointer else None
        if result_id is not None and not _valid_hash(result_id):
            raise HTTPException(409, "집계 그룹의 현재 결과 포인터가 올바르지 않습니다.")
        groups.append({"group_id": group_id, "result_id": result_id,
                       "updated_at": pointer.get("updated_at") if pointer else None,
                       "menu": body.menu, "recipe_version": body.recipe_version,
                       "load_band": body.load_band,
                       "period_start": selection["period_start"],
                       "period_end": selection["period_end"],
                       "job_count": len(body.job_ids)})
    return {"items": groups, "next_cursor": page["next_cursor"]}


async def _record(request: Request, group_id: str, result_id: str, *, write: bool = False):
    if not _valid_hash(group_id) or not _valid_hash(result_id):
        raise HTTPException(400, "집계 기록 ID가 올바르지 않습니다.")
    session, store = await _context(request, write=write)
    pk = f"SCOPE#USER#{session.subject}#AGGREGATE#{group_id}"
    record = await _store_call(store, store.aggregate, pk, result_id)
    if not record or record.get("group_id") != group_id or record.get("result_id") != result_id:
        raise HTTPException(404, "집계 기록을 찾을 수 없습니다.")
    return session, store, record


@router.get("/{group_id}/results/{result_id}")
async def get_aggregate(group_id: str, result_id: str, request: Request) -> dict:
    _, _, record = await _record(request, group_id, result_id)
    return record


@router.get("/{group_id}/results")
async def list_group_results(group_id: str, request: Request,
                             limit: int = Query(20, ge=1, le=50), cursor: str | None = None) -> dict:
    if not _valid_hash(group_id):
        raise HTTPException(400, "집계 그룹 ID가 올바르지 않습니다.")
    if cursor and not _valid_hash(cursor):
        raise HTTPException(400, "집계 결과 목록 커서가 올바르지 않습니다.")
    session, store = await _context(request)
    pk = f"SCOPE#USER#{session.subject}#AGGREGATE#{group_id}"
    page = await _store_call(store, store.list_aggregate_results, pk, limit, cursor)
    if cursor is None and not page["items"]:
        raise HTTPException(404, "집계 그룹을 찾을 수 없습니다.")
    pointer = await _store_call(store, store.current_aggregate, pk)
    current_result_id = pointer.get("result_id") if pointer else None
    if current_result_id is not None and not _valid_hash(current_result_id):
        raise HTTPException(409, "집계 그룹의 현재 결과 포인터가 올바르지 않습니다.")
    return {**page, "current_result_id": current_result_id}


@router.get("/{group_id}/current")
async def get_current_aggregate(group_id: str, request: Request) -> dict:
    if not _valid_hash(group_id):
        raise HTTPException(400, "집계 그룹 ID가 올바르지 않습니다.")
    session, store = await _context(request)
    pk = f"SCOPE#USER#{session.subject}#AGGREGATE#{group_id}"
    pointer = await _store_call(store, store.current_aggregate, pk)
    if not pointer or not _valid_hash(pointer.get("result_id", "")):
        raise HTTPException(404, "현재 집계 결과를 찾을 수 없습니다.")
    record = await _store_call(store, store.aggregate, pk, pointer["result_id"])
    if not record or record.get("group_id") != group_id or record.get("result_id") != pointer["result_id"]:
        raise HTTPException(404, "현재 집계 결과를 찾을 수 없습니다.")
    return record


@router.get("/{group_id}/results/{result_id}/status")
async def aggregate_status(group_id: str, result_id: str, request: Request) -> dict:
    session, store, record = await _record(request, group_id, result_id)
    changed = []
    for reference in record["references"]:
        job_id = reference["job_id"]
        job_pk = _pk(session.subject, job_id)
        meta, pointer = await _store_call(store, store.read, job_pk)
        process_pointer = await _store_call(store, store.process_pointer, job_pk)
        if not meta or int(meta["version"]) != int(reference["job_version"]) or \
                int(meta["safety_version"]) != int(reference["safety_version"]) or \
                (int(pointer["version"]) if pointer else None) != reference["assessment_version"] or \
                ("job_status" in reference and meta["status"] != reference["job_status"]) or \
                any(meta.get(field) != reference[field] for field in
                    ("safety_status", "evaluation_blocked") if field in reference) or \
                ("assessment_usable" in reference and
                 (pointer["usable"] if pointer else None) != reference["assessment_usable"]) or \
                ("assessment_safety_version" in reference and
                 (int(pointer["safety_version"]) if pointer else None) != reference["assessment_safety_version"]) or \
                (int(process_pointer["version"]) if process_pointer else None) != \
                reference.get("process_restatement_version") or \
                (process_pointer["result_sk"] if process_pointer else None) != reference.get("process_result_sk"):
            changed.append(job_id)
    return {"group_id": group_id, "result_id": result_id,
            "status": "STALE" if changed else "CURRENT", "changed_job_ids": changed,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@router.post("/{group_id}/results/{result_id}/refresh", status_code=201)
async def refresh_aggregate(group_id: str, result_id: str, request: Request) -> dict:
    session, store, record = await _record(request, group_id, result_id, write=True)
    selection = record["selection"]
    if _hash(selection) != group_id:
        raise HTTPException(409, "집계 그룹 조건이 변경되었습니다.")
    try:
        body = _stored_request(selection)
    except (KeyError, ValidationError) as exc:
        raise HTTPException(409, "저장된 집계 조건을 다시 사용할 수 없습니다.") from exc
    normalized = _selection(body)
    if normalized != selection:
        raise HTTPException(409, "집계 그룹 조건이 변경되었습니다.")
    result = await _calculate(session, store, body)
    return result
