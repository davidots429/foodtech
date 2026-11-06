"""First synthetic tonkatsu job, safety, and completed assessment flow."""

import asyncio
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from foodtech_contracts import stream_binding_key
from foodtech_contracts.tonkatsu_process import RULE_VERSION, cooking_time_status, process_status, thickness_profile

from .auth import _csrf, _session
from .cooking_store import Conflict, MemoryCookingStore, get_cooking_store

router = APIRouter(prefix="/api/customer/cooking-jobs", tags=["cooking-jobs"])
SYNTHETIC_FRYER_BATCH_KG = Decimal("0.8")
SCORE_ITEMS = (
    "ingredient_condition", "preparation", "taste", "texture", "appearance",
    "aroma", "temperature_suitability",
)
TRANSITIONS = {
    ("PENDING", "PREHEAT_COMPLETED"): "READY",
    ("READY", "PREHEAT_CONDITION_LOST"): "PENDING",
    ("READY", "COOKING_STARTED"): "RUNNING",
    ("RUNNING", "COOKING_PAUSED"): "PAUSED",
    ("PAUSED", "COOKING_RESUMED"): "RUNNING",
    ("SAFETY_HOLD", "COOKING_RESUMED"): "RUNNING",
    ("RUNNING", "COOKING_COMPLETED"): "COMPLETED",
    ("RUNNING", "COOKING_CANCELED"): "CANCELED",
    ("PAUSED", "COOKING_CANCELED"): "CANCELED",
    ("SAFETY_HOLD", "COOKING_CANCELED"): "CANCELED",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pk(subject: str, job_id: str) -> str:
    return f"SCOPE#USER#{subject}#JOB#{job_id}"


def _conflict() -> HTTPException:
    return HTTPException(409, "작업 상태가 변경되었습니다. 다시 조회한 뒤 제출해 주세요.")


async def _store_call(store, method, *args):
    if isinstance(store, MemoryCookingStore):
        return method(*args)
    return await asyncio.to_thread(method, *args)


async def _context(request: Request, *, write: bool = False):
    session = await _session(request, "customer")
    if write:
        _csrf(request, session)
    if os.getenv("FOODTECH_SECURE_COOKIES", "true").lower() == "true" and not os.getenv("FOODTECH_COOKING_TABLE"):
        raise HTTPException(503, "배포 환경에는 공유 조리작업 저장소가 필요합니다.")
    return session, get_cooking_store()


async def _read(request: Request, job_id: str, *, write: bool = False):
    session, store = await _context(request, write=write)
    pk = _pk(session.subject, job_id)
    meta, pointer = await _store_call(store, store.read, pk)
    if not meta:
        raise HTTPException(404, "조리작업을 찾을 수 없습니다.")
    return session, store, pk, meta, pointer


async def _commit(store, pk: str, previous: dict, meta: dict,
                  assessment: dict | None = None, pointer: dict | None = None,
                  event: dict | None = None):
    try:
        await _store_call(store, store.commit, pk, int(previous["version"]), meta, assessment, pointer, event)
    except Conflict as exc:
        raise _conflict() from exc


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewJob(StrictModel):
    planned_portions: int = Field(gt=0, le=4)
    recipe_version: str = Field(min_length=1, max_length=80)
    thickness_cm: Decimal = Field(gt=0, max_digits=4, decimal_places=2)
    equipment_profile: str = "synthetic-fryer"


class SourceBinding(StrictModel):
    expected_version: int = Field(ge=1)
    source_id: str = Field(min_length=1, max_length=80)
    device_id: str = Field(min_length=1, max_length=80)
    batch_id: str = Field(min_length=1, max_length=80, pattern=r"^batch-a-[A-Za-z0-9-]+$")


class Transition(StrictModel):
    event: Literal["PREHEAT_COMPLETED", "PREHEAT_CONDITION_LOST", "COOKING_STARTED",
                   "COOKING_PAUSED", "COOKING_RESUMED", "COOKING_COMPLETED", "COOKING_CANCELED"]
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)
    actual_placed_at: datetime | None = None
    actual_lifted_at: datetime | None = None


class SafetyEvent(StrictModel):
    event: Literal["SUSPECTED", "UNDER_REVIEW", "CLEARED", "FAILED"]
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    manual_stop_confirmed: bool = False
    manual_stopped_at: datetime | None = None
    partial_scores: dict[str, StrictInt] = Field(default_factory=dict)
    partial_observations: dict[str, str] = Field(default_factory=dict)


class CoreSample(StrictModel):
    at_second: int = Field(ge=0, le=60)
    celsius: Decimal | None = None


class Probe(StrictModel):
    item_id: str = Field(min_length=1, max_length=80)
    calibration_status: Literal["OK", "EXPIRED"]
    samples: list[CoreSample] = Field(min_length=1, max_length=61)


class CoreCheck(StrictModel):
    expected_version: int = Field(ge=1)
    probes: list[Probe] = Field(min_length=1, max_length=4)


class CompletedAssessment(StrictModel):
    expected_version: int = Field(ge=1)
    expected_current_version: int = Field(ge=0)
    kind: Literal["INITIAL", "REVISION", "REASSESSMENT"]
    reason: str | None = Field(default=None, max_length=500)
    scores: dict[str, StrictInt]
    observations: dict[str, str]
    overall_satisfaction: int | None = Field(default=None, ge=1, le=5)
    scale_version: str = "tonkatsu-common-v1"
    planned_portions: int = Field(gt=0, le=1000)
    actual_portions: int = Field(gt=0, le=4)
    actual_mass_kg: Decimal = Field(gt=0, le=SYNTHETIC_FRYER_BATCH_KG)
    load_percent: Decimal = Field(gt=0, le=100)
    load_band: Literal["PARTIAL", "REFERENCE"]


class Recook(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


@router.post("", status_code=201)
async def create_job(request: Request, body: NewJob) -> dict:
    session, store = await _context(request, write=True)
    if body.equipment_profile != "synthetic-fryer":
        raise HTTPException(422, "첫 흐름은 합성 튀김기 프로파일만 지원합니다.")
    thickness_status, thickness_band = thickness_profile(body.thickness_cm)
    if thickness_status == "ABOVE_RANGE":
        raise HTTPException(422, "PROFILE_REJECTED: 두께가 합성 돈까스 프로파일 상한 2.0cm를 넘습니다.")
    job_id = str(uuid.uuid4())
    meta = {
        "job_id": job_id, "owner_subject": session.subject, "menu": "TONKATSU",
        "provenance": "SYNTHETIC", "equipment_profile": body.equipment_profile,
        "recipe_version": body.recipe_version, "planned_portions": body.planned_portions,
        "thickness_cm": str(body.thickness_cm), "thickness_profile": thickness_band,
        "thickness_time_rule_version": RULE_VERSION,
        "status": "PENDING", "version": 1, "job_state_version": 1,
        "safety_status": "NOT_MEASURED", "safety_version": 0,
        "evaluation_blocked": True, "evaluation_status": "NOT_COMPLETED",
        "safety_hold_requested_at": None, "manual_stop_confirmed_at": None,
        "manual_stopped_at": None,
        "created_at": _now(), "updated_at": _now(),
        "source_job_id": None, "recook_reason": None, "source_binding": None,
        "actual_placed_at": None, "actual_lifted_at": None,
        "completion_recorded_at": None, "actual_cooking_seconds": None,
        "process_assessment_status": process_status("NOT_MEASURED", thickness_status, "NOT_MEASURED"),
        "process_temperature_status": "NOT_MEASURED", "process_thickness_status": thickness_status,
        "process_time_status": "NOT_MEASURED",
    }
    await _store_call(store, store.create, _pk(session.subject, job_id), meta)
    return meta


@router.post("/{job_id}/source-binding")
async def bind_source(job_id: str, request: Request, body: SourceBinding) -> dict:
    _, store, pk, old, _ = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old["status"] not in {"PENDING", "READY"} or old.get("source_binding"):
        raise HTTPException(409, "조리 시작 전, 아직 연결되지 않은 작업에만 수집 배치를 연결할 수 있습니다.")
    if body.source_id != "synthetic-rest" or body.device_id != "mockprofile-100-a-001":
        raise HTTPException(422, "첫 자동판정은 승인된 합성 REST 프로파일만 지원합니다.")
    stream_pk = stream_binding_key(body.source_id, body.device_id, body.batch_id)
    meta = {**old, "source_binding": {"source_id": body.source_id, "device_id": body.device_id,
                                       "batch_id": body.batch_id},
            "version": old["version"] + 1, "updated_at": _now()}
    try:
        await _store_call(store, store.bind_stream, pk, int(old["version"]), meta, stream_pk)
    except Conflict as exc:
        raise HTTPException(409, "이 수집 배치는 이미 연결되었거나 작업이 변경되었습니다.") from exc
    return meta


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    _, _, _, meta, pointer = await _read(request, job_id)
    return {"job": meta, "current_assessment": pointer}


@router.get("/{job_id}/events")
async def list_events(job_id: str, request: Request) -> list[dict]:
    _, store, pk, _, _ = await _read(request, job_id)
    return await _store_call(store, store.events, pk)


@router.post("/{job_id}/transitions")
async def transition(job_id: str, request: Request, body: Transition) -> dict:
    session, store, pk, old, pointer = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    next_status = TRANSITIONS.get((old["status"], body.event))
    if not next_status:
        raise HTTPException(409, "이 상태에서는 해당 전이를 사용할 수 없습니다.")
    if old["safety_status"] in {"SUSPECTED", "UNDER_REVIEW", "FAILED"} and next_status != "CANCELED":
        raise HTTPException(409, "안전 확인 중에는 조리 진행을 허용하지 않습니다.")
    if old["status"] == "SAFETY_HOLD":
        if body.event == "COOKING_RESUMED" and (old["safety_status"] != "CLEARED" or
                                                   not old.get("manual_stop_confirmed_at")):
            raise HTTPException(409, "수동 중단 확인과 의심 해제 후에만 조리를 재개할 수 있습니다.")
        if not body.reason or not body.reason.strip():
            raise HTTPException(422, "안전 보류 작업의 재개·취소 사유가 필요합니다.")
    now = datetime.now(timezone.utc)
    placed_at = None
    lifted_at = None
    time_status = old.get("process_time_status", "NOT_MEASURED")
    cooking_seconds = None
    if body.event == "COOKING_STARTED":
        if body.actual_placed_at is None or body.actual_lifted_at is not None or \
                body.actual_placed_at.tzinfo is None:
            raise HTTPException(422, "투입시각은 시간대를 포함해 입력해야 합니다.")
        placed_at = body.actual_placed_at.astimezone(timezone.utc)
        if placed_at > now:
            raise HTTPException(422, "투입시각은 현재보다 미래일 수 없습니다.")
    elif body.event == "COOKING_COMPLETED":
        if body.actual_lifted_at is None or body.actual_placed_at is not None or \
                body.actual_lifted_at.tzinfo is None:
            raise HTTPException(422, "인양시각은 시간대를 포함해 입력해야 합니다.")
        lifted_at = body.actual_lifted_at.astimezone(timezone.utc)
        previous_placed = old.get("actual_placed_at")
        if not previous_placed:
            raise HTTPException(409, "투입시각이 기록되지 않아 조리시간을 판정할 수 없습니다.")
        placed_at = datetime.fromisoformat(previous_placed.replace("Z", "+00:00"))
        if lifted_at < placed_at or lifted_at > now:
            raise HTTPException(422, "인양시각은 투입 이후이면서 현재 이전이어야 합니다.")
        cooking_seconds = int((lifted_at - placed_at).total_seconds())
        time_status = cooking_time_status(old.get("thickness_profile"), cooking_seconds)
        if time_status in {"EARLY_REMOVAL", "MAX_TIME_EXCEEDED"}:
            next_status = "FAILED"
    meta = {**old, "status": next_status, "version": old["version"] + 1,
            "job_state_version": old["job_state_version"] + 1, "updated_at": _now(),
            "last_transition_event": body.event, "last_transition_reason": body.reason}
    if body.event == "COOKING_STARTED":
        meta["actual_placed_at"] = placed_at.isoformat().replace("+00:00", "Z")
    elif body.event == "COOKING_COMPLETED":
        meta.update({"actual_lifted_at": lifted_at.isoformat().replace("+00:00", "Z"),
                     "completion_recorded_at": now.isoformat().replace("+00:00", "Z"),
                     "completion_recording_delay_seconds": int((now - lifted_at).total_seconds()),
                     "actual_cooking_seconds": cooking_seconds, "process_time_status": time_status})
        meta["process_assessment_status"] = process_status(
            old.get("process_temperature_status", "NOT_MEASURED"),
            old.get("process_thickness_status", "NOT_MEASURED"), time_status,
            data_gap=bool(old.get("process_assessment", {}).get("data_gap")),
        )
        if next_status == "FAILED":
            meta["last_transition_reason"] = time_status
    if next_status in {"FAILED", "CANCELED"}:
        meta["evaluation_blocked"] = True
    new_pointer = _blocked_pointer(pointer, "JOB_TERMINAL_FAILURE") if next_status in {"FAILED", "CANCELED"} else None
    event = {"sk": f"EVENT#JOB#{int(meta['job_state_version']):012d}",
             "domain": "COOKING_JOB_STATUS", "trigger": body.event,
             "from_status": old["status"], "to_status": next_status,
             "reason": meta["last_transition_reason"], "actor": session.subject, "occurred_at": _now(),
             "actual_placed_at": meta.get("actual_placed_at") if body.event == "COOKING_STARTED" else None,
             "actual_lifted_at": meta.get("actual_lifted_at") if body.event == "COOKING_COMPLETED" else None,
             "actual_cooking_seconds": cooking_seconds,
             "process_time_status": time_status if body.event == "COOKING_COMPLETED" else None,
             "thickness_time_rule_version": RULE_VERSION if body.event == "COOKING_COMPLETED" else None}
    await _commit(store, pk, old, meta, pointer=new_pointer, event=event)
    return meta


def _blocked_pointer(pointer: dict | None, reason: str) -> dict | None:
    return {**pointer, "usable": False, "blocked_reason": reason, "blocked_at": _now()} if pointer else None


@router.post("/{job_id}/safety/events")
async def safety_event(job_id: str, request: Request, body: SafetyEvent) -> dict:
    session, store, pk, old, pointer = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    allowed = {
        "SUSPECTED": {"NOT_MEASURED", "PASSED", "INCOMPLETE", "CLEARED"},
        "UNDER_REVIEW": {"SUSPECTED"},
        "CLEARED": {"SUSPECTED", "UNDER_REVIEW"},
        "FAILED": {"SUSPECTED", "UNDER_REVIEW"},
    }
    if old["safety_status"] not in allowed[body.event]:
        raise HTTPException(409, "이 안전 상태에서는 해당 조치를 사용할 수 없습니다.")
    if set(body.partial_scores) - set(SCORE_ITEMS) or any(score < 1 or score > 5 for score in body.partial_scores.values()):
        raise HTTPException(422, "중단 전 평가 점수는 승인된 7개 항목의 1~5점만 기록할 수 있습니다.")
    if set(body.partial_observations) - set(SCORE_ITEMS) or any(
            not value.strip() or len(value) > 500 for value in body.partial_observations.values()):
        raise HTTPException(422, "중단 전 관찰 근거는 승인된 항목의 500자 이내 내용만 기록할 수 있습니다.")
    if body.event != "SUSPECTED" and (body.partial_scores or body.partial_observations):
        raise HTTPException(422, "중단 전 관찰값은 이상 의심 신고와 함께 기록해야 합니다.")
    if old["status"] == "SAFETY_HOLD" and body.event == "CLEARED" and not (
            old.get("manual_stop_confirmed_at") or body.manual_stop_confirmed):
        raise HTTPException(409, "조리 중 안전 이상은 운영자의 수동 중단 확인 후 해제할 수 있습니다.")
    now = _now()
    job_status = old["status"]
    if body.event == "SUSPECTED" and job_status in {"RUNNING", "PAUSED"}:
        job_status = "SAFETY_HOLD"
    elif body.event == "FAILED" and job_status == "SAFETY_HOLD":
        job_status = "FAILED"
    stop_confirmed_at = None if body.event == "SUSPECTED" else old.get("manual_stop_confirmed_at")
    stopped_at = None if body.event == "SUSPECTED" else old.get("manual_stopped_at")
    if body.manual_stopped_at and not body.manual_stop_confirmed:
        raise HTTPException(422, "수동 중단 시각에는 운영자의 중단 확인이 필요합니다.")
    if body.manual_stop_confirmed:
        if old["status"] not in {"RUNNING", "PAUSED", "SAFETY_HOLD"}:
            raise HTTPException(422, "조리 중인 작업의 수동 중단만 확인할 수 있습니다.")
        if not stop_confirmed_at:
            if body.manual_stopped_at is None or body.manual_stopped_at.tzinfo is None:
                raise HTTPException(422, "수동 중단 시각은 시간대를 포함해 입력해야 합니다.")
            actual_stop = body.manual_stopped_at.astimezone(timezone.utc)
            placed = old.get("actual_placed_at")
            if actual_stop > datetime.now(timezone.utc) or (placed and actual_stop < datetime.fromisoformat(
                    placed.replace("Z", "+00:00"))):
                raise HTTPException(422, "수동 중단 시각은 재료 투입 이후이면서 현재 이전이어야 합니다.")
            stopped_at = actual_stop.isoformat().replace("+00:00", "Z")
            stop_confirmed_at = now
    meta = {**old, "safety_status": body.event, "safety_version": old["safety_version"] + 1,
            "status": job_status, "job_state_version": int(old["job_state_version"]) + (job_status != old["status"]),
            "version": old["version"] + 1, "evaluation_blocked": True, "updated_at": now,
            "safety_reason": body.reason, "safety_reporter": session.subject,
            "manual_stop_confirmed": bool(stop_confirmed_at),
            "manual_stop_confirmed_at": stop_confirmed_at,
            "manual_stopped_at": stopped_at,
            "safety_hold_requested_at": now if job_status == "SAFETY_HOLD" and old["status"] != "SAFETY_HOLD"
                                        else old.get("safety_hold_requested_at"),
            "evaluation_status": "INTERRUPTED" if body.event in {"SUSPECTED", "FAILED"}
                                 else old.get("evaluation_status", "NOT_COMPLETED")}
    event = {"sk": f"EVENT#SAFETY#{int(meta['safety_version']):012d}",
             "domain": "SAFETY_STATUS", "trigger": body.event,
             "from_status": old["safety_status"], "to_status": body.event,
             "from_job_status": old["status"], "to_job_status": job_status,
             "reason": body.reason, "manual_stop_confirmed": bool(stop_confirmed_at),
             "manual_stop_confirmed_at": stop_confirmed_at,
             "manual_stopped_at": stopped_at,
             "partial_scores": body.partial_scores if body.event == "SUSPECTED" else {},
             "partial_observations": body.partial_observations if body.event == "SUSPECTED" else {},
             "evaluation_status": meta["evaluation_status"],
             "actor": session.subject, "occurred_at": now}
    await _commit(store, pk, old, meta, pointer=_blocked_pointer(pointer, f"SAFETY_{body.event}"), event=event)
    return meta


@router.post("/{job_id}/safety/core-check")
async def core_check(job_id: str, request: Request, body: CoreCheck) -> dict:
    session, store, pk, old, pointer = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old["status"] != "COMPLETED" or old["safety_status"] in {"SUSPECTED", "UNDER_REVIEW", "FAILED"}:
        raise HTTPException(409, "완료된 작업의 안전 확인 가능 상태에서만 검사합니다.")
    ids = [probe.item_id for probe in body.probes]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "제품별 탐침 기록이 중복되었습니다.")
    if len(body.probes) != old["planned_portions"]:
        raise HTTPException(422, "계획 인분마다 독립 탐침 기록이 필요합니다.")
    failed = any(probe.calibration_status == "OK" and any(
        sample.celsius is not None and sample.celsius < 75 for sample in probe.samples)
        for probe in body.probes)
    complete = all(
        probe.calibration_status == "OK" and len(probe.samples) == 61
        and [sample.at_second for sample in probe.samples] == list(range(61))
        and all(sample.celsius is not None for sample in probe.samples)
        for probe in body.probes
    )
    if failed:
        status = "FAILED"
    elif not complete:
        status = "INCOMPLETE"
    else:
        status = "PASSED"
    meta = {**old, "safety_status": status, "safety_version": old["safety_version"] + 1,
            "version": old["version"] + 1, "evaluation_blocked": status != "PASSED",
            "core_check": [probe.model_dump(mode="json") for probe in body.probes],
            "core_check_by": session.subject, "updated_at": _now()}
    event = {"sk": f"EVENT#SAFETY#{int(meta['safety_version']):012d}",
             "domain": "SAFETY_STATUS", "trigger": "CORE_CHECK",
             "from_status": old["safety_status"], "to_status": status,
             "probes": meta["core_check"], "actor": session.subject, "occurred_at": _now()}
    await _commit(store, pk, old, meta, pointer=_blocked_pointer(pointer, f"SAFETY_{status}"), event=event)
    return meta


@router.post("/{job_id}/assessments", status_code=201)
async def complete_assessment(job_id: str, request: Request, body: CompletedAssessment) -> dict:
    session, store, pk, old, pointer = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    current_version = int(pointer["version"]) if pointer else 0
    if current_version != body.expected_current_version:
        raise _conflict()
    if old["status"] != "COMPLETED" or old["safety_status"] != "PASSED" or old["evaluation_blocked"]:
        raise HTTPException(409, "작업 완료와 유효한 안전 판정 후에만 평가를 완료할 수 있습니다.")
    if set(body.scores) != set(SCORE_ITEMS) or any(type(score) is not int or score < 1 or score > 5
                                                  for score in body.scores.values()):
        raise HTTPException(422, "공통 척도의 7개 항목에 각각 1~5점을 입력해야 합니다.")
    if set(body.observations) != set(SCORE_ITEMS) or any(not text.strip() or len(text) > 500
                                                        for text in body.observations.values()):
        raise HTTPException(422, "7개 항목의 관찰 근거가 필요합니다.")
    if body.planned_portions != old["planned_portions"] or body.scale_version != "tonkatsu-common-v1":
        raise HTTPException(422, "작업의 인분 수와 승인된 첫 척도 버전을 사용해야 합니다.")
    calculated_load = (body.actual_mass_kg / SYNTHETIC_FRYER_BATCH_KG * 100).quantize(Decimal("0.01"))
    expected_band = "PARTIAL" if calculated_load <= 50 else "REFERENCE"
    if body.load_percent != calculated_load or body.load_band != expected_band:
        raise HTTPException(422, "합성 튀김기 적재량과 구간이 일치하지 않습니다.")
    if (current_version == 0 and body.kind != "INITIAL") or (current_version > 0 and body.kind == "INITIAL"):
        raise HTTPException(422, "최초 평가와 수정·재평가 유형이 일치하지 않습니다.")
    if current_version > 0 and not body.reason:
        raise HTTPException(422, "수정·재평가 사유가 필요합니다.")
    total = sum(body.scores.values())
    score = (Decimal(total) / Decimal(35) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    version = current_version + 1
    assessment = {
        "sk": f"ASSESSMENT#REV#{version:012d}", "version": version,
        "previous_version": current_version or None, "kind": body.kind, "reason": body.reason,
        "status": "COMPLETED", "scores": body.scores, "observations": body.observations,
        "score_total": total, "score_percent": str(score),
        "quality_passed": min(body.scores.values()) >= 3 and score >= 60,
        "quality_target": min(body.scores.values()) >= 4 and score >= 80,
        "overall_satisfaction": body.overall_satisfaction,
        "scale_version": body.scale_version, "formula_version": "quality-v1",
        "safety_version": old["safety_version"], "job_state_version": old["job_state_version"],
        "menu": old["menu"], "equipment_profile": old["equipment_profile"],
        "planned_portions": body.planned_portions, "actual_portions": body.actual_portions,
        "actual_mass_kg": str(body.actual_mass_kg), "load_percent": str(body.load_percent),
        "load_band": body.load_band, "assessed_by": session.subject, "completed_at": _now(),
    }
    new_pointer = {"version": version, "usable": True, "blocked_reason": None,
                   "assessment_sk": assessment["sk"], "quality_passed": assessment["quality_passed"],
                   "quality_target": assessment["quality_target"], "score_percent": str(score),
                   "safety_version": old["safety_version"]}
    meta = {**old, "version": old["version"] + 1, "updated_at": _now(),
            "evaluation_status": "COMPLETED"}
    await _commit(store, pk, old, meta, assessment, new_pointer)
    return assessment


@router.get("/{job_id}/assessments")
async def list_assessments(job_id: str, request: Request) -> list[dict]:
    _, store, pk, _, _ = await _read(request, job_id)
    return await _store_call(store, store.assessments, pk)


@router.post("/{job_id}/recook", status_code=201)
async def recook(job_id: str, request: Request, body: Recook) -> dict:
    session, store, parent_pk, old, _ = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old["status"] not in {"COMPLETED", "FAILED", "CANCELED"}:
        raise HTTPException(409, "종료된 작업만 재조리 작업과 연결할 수 있습니다.")
    new_id = str(uuid.uuid4())
    meta = {"job_id": new_id, "owner_subject": session.subject, "menu": old["menu"],
            "provenance": "SYNTHETIC", "equipment_profile": old["equipment_profile"],
            "recipe_version": old["recipe_version"], "planned_portions": old["planned_portions"],
            "thickness_cm": old.get("thickness_cm"), "thickness_profile": old.get("thickness_profile"),
            "thickness_time_rule_version": old.get("thickness_time_rule_version", RULE_VERSION),
            "status": "PENDING", "version": 1, "job_state_version": 1,
            "safety_status": "NOT_MEASURED", "safety_version": 0,
            "evaluation_blocked": True, "evaluation_status": "NOT_COMPLETED",
            "safety_hold_requested_at": None, "manual_stop_confirmed_at": None,
            "manual_stopped_at": None,
            "created_at": _now(), "updated_at": _now(),
            "source_job_id": job_id, "recook_reason": body.reason, "source_binding": None,
            "actual_placed_at": None, "actual_lifted_at": None,
            "completion_recorded_at": None, "actual_cooking_seconds": None,
            "process_assessment_status": process_status("NOT_MEASURED", old.get("process_thickness_status", "NOT_MEASURED"), "NOT_MEASURED"),
            "process_temperature_status": "NOT_MEASURED",
            "process_thickness_status": old.get("process_thickness_status", "NOT_MEASURED"),
            "process_time_status": "NOT_MEASURED"}
    try:
        await _store_call(store, store.create_recook, parent_pk, body.expected_version,
                          _pk(session.subject, new_id), meta)
    except Conflict as exc:
        raise _conflict() from exc
    return meta
