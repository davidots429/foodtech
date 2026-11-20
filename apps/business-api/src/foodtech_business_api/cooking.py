"""Synthetic cooking jobs; tonkatsu safety/evaluation and Jeyuk process linkage."""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from foodtech_contracts import stream_binding_key
from foodtech_contracts.cooking_live_time import observe as observe_live_time
from foodtech_contracts.tonkatsu_process import (RULE_VERSION, TEMPERATURE_RULE_VERSION,
                                                 cooking_time_status, process_status, thickness_profile)
from foodtech_contracts.jeyuk_process import (RULE_VERSION as JEYUK_RULE_VERSION,
                                              cooking_time_status as jeyuk_cooking_time_status,
                                              load_status as jeyuk_load_status)

from .auth import _csrf, _session
from .cooking_store import Conflict, MemoryCookingStore, get_cooking_store

router = APIRouter(prefix="/api/customer/cooking-jobs", tags=["cooking-jobs"])
logger = logging.getLogger(__name__)
SYNTHETIC_FRYER_BATCH_KG = Decimal("0.8")
SYNTHETIC_JEYUK_RECOMMENDED_KG = Decimal("1.8")
JEYUK_QUALITY_REASONS = frozenset({
    "UNDERCOOKED", "EXCESS_MOISTURE", "OVERCOOKED", "UNEVEN_COOK", "BAD_SMELL",
    "BURNT_BOTTOM", "UNEVEN_MIX", "INGREDIENT_DAMAGE", "SAUCE_NOT_COATED",
})
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


def _validation_digest(context: dict) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


async def _read_jeyuk_source_records(table_name: str, batch_id: str, last_second: int):
    from .jeyuk_source_evidence import read_source_records
    return await asyncio.to_thread(read_source_records, table_name, batch_id, last_second)


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
    from .assessment_aggregate import refresh_dependents as refresh_aggregates
    from .comparison import refresh_dependents as refresh_comparisons
    try:
        await refresh_aggregates(store, pk)
        await refresh_comparisons(store, pk)
    except Exception as exc:
        logger.exception("Dependent derived-result refresh failed after cooking job commit")
        raise HTTPException(503, "작업 변경은 저장되었으나 관련 파생 결과 갱신을 완료하지 못했습니다."
                                 " 작업을 다시 조회하고 집계·비교 갱신을 재시도해 주세요.") from exc


@router.post("/{job_id}/refresh-aggregates")
async def refresh_job_aggregates(job_id: str, request: Request) -> dict:
    _, store, pk, _, _ = await _read(request, job_id, write=True)
    from .assessment_aggregate import refresh_dependents
    return {"refreshed_group_count": await refresh_dependents(store, pk)}


@router.post("/{job_id}/refresh-comparisons")
async def refresh_job_comparisons(job_id: str, request: Request) -> dict:
    _, store, pk, _, _ = await _read(request, job_id, write=True)
    from .comparison import refresh_dependents
    return await refresh_dependents(store, pk)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewJob(StrictModel):
    planned_portions: int = Field(gt=0, le=4)
    recipe_version: str = Field(min_length=1, max_length=80)
    thickness_cm: Decimal | None = Field(default=None, gt=0, max_digits=4, decimal_places=2)
    actual_load_kg: Decimal | None = Field(default=None, gt=0, max_digits=5, decimal_places=2)
    equipment_profile: str = "synthetic-fryer"


class SourceBinding(StrictModel):
    expected_version: int = Field(ge=1)
    source_id: str = Field(min_length=1, max_length=80)
    device_id: str = Field(min_length=1, max_length=80)
    batch_id: str = Field(min_length=1, max_length=80,
                          pattern=r"^(?:batch-a-|jeyuk-)[A-Za-z0-9-]+$")


class JeyukValidationContext(StrictModel):
    schema_version: Literal["jeyuk-validation-context-v1"]
    expected_version: int = Field(ge=1)
    ingredient_spec_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    ingredient_lot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    supplier_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    sensor_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    sensor_calibration_record_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    process_condition_profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    process_condition_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")


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
    celsius: Decimal | None = Field(default=None, ge=Decimal("-273.15"), le=Decimal("300"))


class Probe(StrictModel):
    item_id: str = Field(min_length=1, max_length=80)
    calibration_status: Literal["OK", "EXPIRED"]
    samples: list[CoreSample] = Field(min_length=1, max_length=61)


class CoreCheck(StrictModel):
    expected_version: int = Field(ge=1)
    probes: list[Probe] = Field(min_length=1, max_length=4)


class JeyukDischargeSample(StrictModel):
    phase: Literal["EARLY", "MIDDLE", "LATE"]
    probe_id: str = Field(min_length=1, max_length=80)
    meat_sample_id: str = Field(min_length=1, max_length=80)
    thickest_or_clumped_part: bool
    sampled_at: datetime
    calibration_status: Literal["OK", "EXPIRED"]
    celsius: Decimal | None = Field(default=None, ge=Decimal("-273.15"), le=Decimal("300"))


class JeyukDischargeCheck(StrictModel):
    expected_version: int = Field(ge=1)
    kind: Literal["INITIAL", "REVALIDATION"]
    samples: list[JeyukDischargeSample] = Field(min_length=1, max_length=3)


class CompletedAssessment(StrictModel):
    expected_version: int = Field(ge=1)
    expected_current_version: int = Field(ge=0)
    kind: Literal["INITIAL", "REVISION", "REASSESSMENT"]
    reason: str | None = Field(default=None, max_length=500)
    scores: dict[str, StrictInt]
    observations: dict[str, str]
    overall_satisfaction: int | None = Field(default=None, ge=1, le=5)
    scale_version: str = "tonkatsu-common-v1"
    quality_decision: Literal["QUALITY_PASSED", "QUALITY_FAILED"] | None = None
    quality_reason_codes: list[str] = Field(default_factory=list, max_length=9)
    quality_note: str | None = Field(default=None, max_length=500)
    planned_portions: int = Field(gt=0, le=1000)
    actual_portions: int = Field(gt=0, le=4)
    actual_mass_kg: Decimal = Field(gt=0, le=3)
    load_percent: Decimal = Field(gt=0, le=167)
    load_band: Literal["PARTIAL", "REFERENCE", "OVER_RECOMMENDED"]


class Recook(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


@router.post("", status_code=201)
async def create_job(request: Request, body: NewJob) -> dict:
    session, store = await _context(request, write=True)
    if body.equipment_profile == "synthetic-fryer":
        if body.thickness_cm is None or body.actual_load_kg is not None:
            raise HTTPException(422, "합성 돈까스 작업에는 두께만 입력해야 합니다.")
        thickness_status, thickness_band = thickness_profile(body.thickness_cm)
        if thickness_status == "ABOVE_RANGE":
            raise HTTPException(422, "PROFILE_REJECTED: 두께가 합성 돈까스 프로파일 상한 2.0cm를 넘습니다.")
        menu, load_status = "TONKATSU", None
    elif body.equipment_profile == "jeyuk-small":
        if body.actual_load_kg is None or body.thickness_cm is not None:
            raise HTTPException(422, "합성 제육볶음 작업에는 실제 투입량만 입력해야 합니다.")
        load_status = jeyuk_load_status("SMALL", body.actual_load_kg)
        if load_status == "RATED_LOAD_EXCEEDED":
            raise HTTPException(422, "PROFILE_REJECTED: 합성 소형 볶음·교반기의 정격 하중을 넘습니다.")
        menu, thickness_status, thickness_band = "JEYUK", "NOT_APPLICABLE", None
    else:
        raise HTTPException(422, "지원되지 않는 합성 장비 프로파일입니다.")
    job_id = str(uuid.uuid4())
    meta = {
        "job_id": job_id, "owner_subject": session.subject, "menu": menu,
        "provenance": "SYNTHETIC", "equipment_profile": body.equipment_profile,
        "recipe_version": body.recipe_version, "planned_portions": body.planned_portions,
        "thickness_cm": str(body.thickness_cm) if body.thickness_cm is not None else None,
        "thickness_profile": thickness_band,
        "thickness_time_rule_version": RULE_VERSION if menu == "TONKATSU" else None,
        "jeyuk_rule_version": JEYUK_RULE_VERSION if menu == "JEYUK" else None,
        "jeyuk_size_profile": "SMALL" if menu == "JEYUK" else None,
        "actual_load_kg": str(body.actual_load_kg) if body.actual_load_kg is not None else None,
        "process_load_status": load_status,
        "status": "PENDING", "version": 1, "job_state_version": 1,
        "safety_status": "NOT_MEASURED", "safety_version": 0,
        "evaluation_blocked": True, "evaluation_status": "NOT_COMPLETED",
        "safety_hold_requested_at": None, "manual_stop_confirmed_at": None,
        "manual_stopped_at": None,
        "created_at": _now(), "updated_at": _now(),
        "source_job_id": None, "recook_reason": None, "source_binding": None,
        "validation_context": None, "validation_context_digest": None,
        "validation_context_version": 0, "validation_context_status": "NOT_RECORDED",
        "actual_placed_at": None, "actual_lifted_at": None,
        "completion_recorded_at": None, "actual_cooking_seconds": None,
        "process_assessment_status": ("WARNING" if load_status == "OVER_RECOMMENDED_LOAD" else
                                      "INSUFFICIENT_DATA") if menu == "JEYUK" else
                                     process_status("NOT_MEASURED", thickness_status, "NOT_MEASURED"),
        "process_temperature_status": "NOT_MEASURED", "process_thickness_status": thickness_status,
        "process_surface_status": "NOT_MEASURED", "process_rpm_status": "NOT_MEASURED",
        "auto_safety_status": "NOT_MEASURED", "mixture_hold_seconds": 0,
        "safety_passed_at_second": None,
        "discharge_check_version": 0, "discharge_check_status": "NOT_MEASURED",
        "process_time_status": "NOT_MEASURED",
    }
    await _store_call(store, store.create, _pk(session.subject, job_id), meta)
    return meta


@router.get("")
async def list_jobs(request: Request, limit: int = Query(20, ge=1, le=50),
                    cursor: str | None = None) -> dict:
    if cursor:
        parts = cursor.split("#")
        try:
            if len(parts) != 3 or parts[0] != "CREATED" or \
                    datetime.fromisoformat(parts[1].replace("Z", "+00:00")).tzinfo is None or \
                    str(uuid.UUID(parts[2])) != parts[2]:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(400, "작업 목록 커서가 올바르지 않습니다.") from exc
    session, store = await _context(request)
    return await _store_call(store, store.list_jobs, session.subject, limit, cursor)


@router.post("/{job_id}/list-entry")
async def register_job_in_list(job_id: str, request: Request) -> dict:
    _, store, pk, meta, _ = await _read(request, job_id, write=True)
    await _store_call(store, store.ensure_listed, pk, meta)
    return {"job_id": meta["job_id"]}


@router.post("/{job_id}/source-binding")
async def bind_source(job_id: str, request: Request, body: SourceBinding) -> dict:
    _, store, pk, old, _ = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old["status"] not in {"PENDING", "READY"} or old.get("source_binding"):
        raise HTTPException(409, "조리 시작 전, 아직 연결되지 않은 작업에만 수집 배치를 연결할 수 있습니다.")
    expected_device = ("mockprofile-110-a-001" if old["menu"] == "JEYUK" else
                       "mockprofile-100-a-001")
    expected_prefix = "jeyuk-" if old["menu"] == "JEYUK" else "batch-a-"
    if (body.source_id != "synthetic-rest" or body.device_id != expected_device
            or not body.batch_id.startswith(expected_prefix)):
        raise HTTPException(422, "첫 자동판정은 승인된 합성 REST 프로파일만 지원합니다.")
    stream_pk = (f"JEYUK#{body.batch_id}" if old["menu"] == "JEYUK" else
                 stream_binding_key(body.source_id, body.device_id, body.batch_id))
    meta = {**old, "source_binding": {"source_id": body.source_id, "device_id": body.device_id,
                                       "batch_id": body.batch_id},
            "version": old["version"] + 1, "updated_at": _now()}
    try:
        await _store_call(store, store.bind_stream, pk, int(old["version"]), meta, stream_pk)
    except Conflict as exc:
        raise HTTPException(409, "이 수집 배치는 이미 연결되었거나 작업이 변경되었습니다.") from exc
    return meta


@router.post("/{job_id}/validation-context")
async def record_jeyuk_validation_context(job_id: str, request: Request,
                                          body: JeyukValidationContext) -> dict:
    session, store, pk, old, _ = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old["menu"] != "JEYUK":
        raise HTTPException(422, "제육볶음 작업에만 재검증 조합을 기록할 수 있습니다.")
    if old["status"] not in {"PENDING", "READY"} or not old.get("source_binding"):
        raise HTTPException(409, "수집 배치를 연결한 뒤 조리 시작 전에 조합을 기록할 수 있습니다.")
    binding = old["source_binding"]
    context = {
        "schema_version": body.schema_version,
        "recipe_version": old["recipe_version"],
        "equipment_profile": old["equipment_profile"],
        "equipment_device_id": binding["device_id"],
        "source_id": binding["source_id"],
        "actual_load_kg": str(Decimal(old["actual_load_kg"]).quantize(Decimal("0.01"))),
        "ingredient_spec_id": body.ingredient_spec_id,
        "ingredient_lot_id": body.ingredient_lot_id,
        "supplier_id": body.supplier_id,
        "sensor_id": body.sensor_id,
        "sensor_calibration_record_id": body.sensor_calibration_record_id,
        "process_condition_profile_id": body.process_condition_profile_id,
        "process_condition_version": body.process_condition_version,
    }
    # Batch and job IDs identify observations, not the reusable validation combination.
    digest = _validation_digest(context)
    version = int(old.get("validation_context_version", 0)) + 1
    now = _now()
    meta = {**old, "validation_context": context, "validation_context_digest": digest,
            "validation_context_version": version, "validation_context_status": "DECLARED_UNVERIFIED",
            "version": int(old["version"]) + 1, "updated_at": now}
    event = {"sk": f"EVENT#VALIDATION_CONTEXT#{version:012d}",
             "domain": "VALIDATION_CONTEXT", "trigger": "VALIDATION_CONTEXT_RECORDED",
             "from_status": old.get("validation_context_status", "NOT_RECORDED"),
             "to_status": "DECLARED_UNVERIFIED", "previous_digest": old.get("validation_context_digest"),
             "context_digest": digest, "context_version": version, "context": context,
             "actor_subject": session.subject, "occurred_at": now}
    await _commit(store, pk, old, meta, event=event)
    return meta


@router.get("/{job_id}/validation-evidence")
async def jeyuk_validation_evidence(job_id: str, request: Request) -> dict:
    _, _, _, job, _ = await _read(request, job_id)
    if job["menu"] != "JEYUK":
        raise HTTPException(422, "제육볶음 작업에만 재검증 증거를 조회할 수 있습니다.")
    binding = job.get("source_binding")
    process = job.get("process_assessment") or {}
    context = job.get("validation_context") or {}
    expected_pk = f"JEYUK#{binding['batch_id']}" if binding else None
    second = process.get("second")
    process_linked = (type(second) is int and second > 0 and
                      process.get("source_assessment_pk") == expected_pk and
                      process.get("source_assessment_sk") == f"ASSESSMENT#{second:06d}" and
                      process.get("rule_version") == JEYUK_RULE_VERSION)
    context_matches_job = (bool(binding) and context.get("recipe_version") == job["recipe_version"] and
                           context.get("equipment_profile") == job["equipment_profile"] and
                           context.get("equipment_device_id") == binding["device_id"] and
                           context.get("source_id") == binding["source_id"] and
                           context.get("actual_load_kg") ==
                           str(Decimal(job["actual_load_kg"]).quantize(Decimal("0.01"))) and
                           job.get("validation_context_digest") == _validation_digest(context))
    source_checks = {"source_records_contiguous": False,
                     "source_records_match_binding": False,
                     "source_derived_results_match_raw": False,
                     "source_process_conditions_normal": False,
                     "source_safety_samples_valid": False,
                     "source_validation_identity_matches": False,
                     "source_process_span_covers_cooking": False}
    source_table = os.getenv("FOODTECH_DATA_TABLE", "")
    if process_linked and context_matches_job and source_table and second <= 600:
        from .jeyuk_source_evidence import audit_source_records
        try:
            assessments, raws = await _read_jeyuk_source_records(source_table, binding["batch_id"], second)
            source_checks = audit_source_records(job, assessments, raws)
        except Exception as exc:
            logger.exception("Jeyuk source evidence read failed")
            raise HTTPException(503, "원천 수집 증거를 조회하지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    checks = {
        "context_recorded": (job.get("validation_context_status") == "DECLARED_UNVERIFIED" and
                             bool(job.get("validation_context_digest"))),
        "context_matches_job": context_matches_job,
        "job_completed": job["status"] == "COMPLETED",
        "source_bound": bool(binding),
        "process_reference_linked": process_linked,
        "automatic_safety_passed": (job.get("auto_safety_status") == "PASSED" and
                                     int(job.get("mixture_hold_seconds", 0)) >= 60 and
                                     job.get("safety_passed_at_second") is not None),
        "manual_discharge_passed": (job.get("discharge_check_status") == "PASSED" and
                                    int(job.get("discharge_check_version", 0)) > 0 and
                                    len(job.get("discharge_check") or []) == 3),
        "current_safety_passed": job.get("safety_status") == "PASSED" and not job.get("evaluation_blocked"),
        **source_checks,
    }
    return {
        "job_id": job_id,
        "status": "LINKED_UNVERIFIED" if all(checks.values()) else "INCOMPLETE",
        "checks": checks,
        "missing_checks": [name for name, passed in checks.items() if not passed],
        "context_digest": job.get("validation_context_digest"),
        "context_version": int(job.get("validation_context_version", 0)),
        "job_version": int(job["version"]), "safety_version": int(job["safety_version"]),
        "discharge_check_version": int(job.get("discharge_check_version", 0)),
        "process_reference": ({"pk": process["source_assessment_pk"],
                               "sk": process["source_assessment_sk"], "second": second}
                              if process_linked else None),
        "source_table_configured": bool(source_table),
        "activation_allowed": False,
        "remaining_verification": ["SENSOR_AND_CALIBRATION_IDENTITY",
                                   "INGREDIENT_LOT_AND_SUPPLIER_TRACEABILITY",
                                   "PROCESS_CONDITION_PROFILE_IDENTITY"],
    }


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request,
                  process_view: Literal["LATEST_RESTATED", "AS_PROCESSED"] = "LATEST_RESTATED") -> dict:
    _, store, pk, meta, pointer = await _read(request, job_id)
    derived = (await _store_call(store, store.latest_process_result, pk)
               if process_view == "LATEST_RESTATED" else None)
    process = ({"view": "LATEST_RESTATED", "status": derived["process_assessment_status"],
                "assessment": derived["process_assessment"], "result_sk": derived["sk"],
                "observation_coverage": derived.get("observation_coverage"),
                "created_at": derived["created_at"],
                "job_version_at_derivation": derived["job_version_at_derivation"],
                "job_version_changed": int(meta["version"]) != int(derived["job_version_at_derivation"])}
               if derived else
               {"view": "AS_PROCESSED", "status": meta["process_assessment_status"],
                "assessment": meta.get("process_assessment"), "result_sk": None,
                "observation_coverage": None,
                "created_at": None, "job_version_at_derivation": None,
                "job_version_changed": False})
    return {"job": meta, "current_assessment": pointer, "process_assessment_view": process}


@router.get("/{job_id}/time-monitor")
async def get_time_monitor(job_id: str, request: Request, response: Response) -> dict:
    _, _, _, job, _ = await _read(request, job_id)
    response.headers["Cache-Control"] = "no-store"
    return {"job_id": job_id, "job_version": int(job["version"]),
            **observe_live_time(job, datetime.now(timezone.utc))}


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
        time_status = (jeyuk_cooking_time_status(old["jeyuk_size_profile"], cooking_seconds)
                       if old["menu"] == "JEYUK" else
                       cooking_time_status(old.get("thickness_profile"), cooking_seconds))
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
        if old["menu"] == "JEYUK":
            prior = old.get("process_assessment_status", "INSUFFICIENT_DATA")
            meta["process_assessment_status"] = (
                "FAILED" if prior == "FAILED" or time_status in {"EARLY_REMOVAL", "MAX_TIME_EXCEEDED"}
                else "INSUFFICIENT_DATA" if prior == "INSUFFICIENT_DATA"
                else "WARNING" if prior == "WARNING" or time_status != "NORMAL"
                else "NORMAL")
        else:
            assessment = old.get("process_assessment")
            gap_or_unknown = ((assessment.get("data_gap") is not False or
                               assessment.get("gap_history_verified") is not True or
                               assessment.get("rule_version") != TEMPERATURE_RULE_VERSION)
                              if isinstance(assessment, dict) else
                              old.get("process_temperature_status", "NOT_MEASURED") != "NOT_MEASURED")
            meta["process_assessment_status"] = process_status(
                old.get("process_temperature_status", "NOT_MEASURED"),
                old.get("process_thickness_status", "NOT_MEASURED"), time_status,
                data_gap=gap_or_unknown,
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
    if old["menu"] != "TONKATSU":
        raise HTTPException(422, "제육볶음 안전 확인은 혼합물 온도 시계열 계약에서 수행해야 합니다.")
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


@router.post("/{job_id}/safety/discharge-check")
async def jeyuk_discharge_check(job_id: str, request: Request, body: JeyukDischargeCheck) -> dict:
    session, store, pk, old, pointer = await _read(request, job_id, write=True)
    if old["menu"] != "JEYUK":
        raise HTTPException(422, "제육볶음 작업에만 배출 표본 확인을 기록할 수 있습니다.")
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old["status"] != "COMPLETED" or old["safety_status"] in {"SUSPECTED", "UNDER_REVIEW", "FAILED"}:
        raise HTTPException(409, "완료된 작업의 안전 확인 가능 상태에서만 검사합니다.")
    if (body.kind == "INITIAL") != (int(old.get("discharge_check_version", 0)) == 0):
        raise HTTPException(422, "최초 검증과 재검증 유형이 이전 기록과 일치해야 합니다.")
    phases = [sample.phase for sample in body.samples]
    if len(set(phases)) != len(phases):
        raise HTTPException(422, "배출 단계별 표본은 한 번씩만 기록합니다.")
    if any(sample.sampled_at.tzinfo is None for sample in body.samples):
        raise HTTPException(422, "측정시각에는 시간대가 필요합니다.")
    discharge_at = datetime.fromisoformat(old["actual_lifted_at"].replace("Z", "+00:00"))
    if any(sample.sampled_at.astimezone(timezone.utc) < discharge_at for sample in body.samples):
        raise HTTPException(422, "배출 시작 전 측정값은 사용할 수 없습니다.")
    checked_at = datetime.now(timezone.utc)
    if any(sample.sampled_at.astimezone(timezone.utc) > checked_at for sample in body.samples):
        raise HTTPException(422, "미래 시각의 배출 표본은 사용할 수 없습니다.")
    if any(body.samples[i].sampled_at > body.samples[i + 1].sampled_at for i in range(len(body.samples) - 1)) or \
            phases != [phase for phase in ("EARLY", "MIDDLE", "LATE") if phase in phases]:
        raise HTTPException(422, "배출 초기·중간·말기 순서대로 측정해야 합니다.")
    valid_low = any(sample.calibration_status == "OK" and sample.thickest_or_clumped_part and
                    sample.celsius is not None and sample.celsius < 75 for sample in body.samples)
    complete = (phases == ["EARLY", "MIDDLE", "LATE"] and
                len({sample.meat_sample_id for sample in body.samples}) == 3 and
                all(sample.calibration_status == "OK" and sample.thickest_or_clumped_part and
                    sample.celsius is not None for sample in body.samples))
    manual_status = "FAILED" if valid_low else "PASSED" if complete else "ASSESSMENT_INCOMPLETE"
    auto_status = old.get("auto_safety_status", "NOT_MEASURED")
    status = ("FAILED" if manual_status == "FAILED" else
              "PASSED" if manual_status == "PASSED" and auto_status == "PASSED" else
              "INCOMPLETE")
    now = _now()
    version = int(old.get("discharge_check_version", 0)) + 1
    evidence = [sample.model_dump(mode="json") for sample in body.samples]
    meta = {**old, "safety_status": status, "safety_version": int(old["safety_version"]) + 1,
            "discharge_check_version": version, "discharge_check_status": manual_status,
            "discharge_check_kind": body.kind, "discharge_check": evidence,
            "discharge_check_by": session.subject, "version": int(old["version"]) + 1,
            "evaluation_blocked": status != "PASSED", "updated_at": now}
    event = {"sk": f"EVENT#SAFETY#{int(meta['safety_version']):012d}",
             "domain": "SAFETY_STATUS", "trigger": "JEYUK_DISCHARGE_CHECK",
             "from_status": old["safety_status"], "to_status": status,
             "manual_status": manual_status, "auto_safety_status": auto_status,
             "kind": body.kind, "samples": evidence, "actor": session.subject, "occurred_at": now}
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
    expected_scale = "jeyuk-common-v1" if old["menu"] == "JEYUK" else "tonkatsu-common-v1"
    if body.planned_portions != old["planned_portions"] or body.scale_version != expected_scale:
        raise HTTPException(422, "작업의 인분 수와 승인된 첫 척도 버전을 사용해야 합니다.")
    if old["menu"] == "JEYUK":
        if body.actual_mass_kg != Decimal(str(old["actual_load_kg"])) or body.actual_portions > old["planned_portions"]:
            raise HTTPException(422, "제육볶음 실제 적재량과 인분은 작업 기록에 맞아야 합니다.")
        if body.quality_decision is None or len(body.quality_reason_codes) != len(set(body.quality_reason_codes)) or \
                set(body.quality_reason_codes) - JEYUK_QUALITY_REASONS:
            raise HTTPException(422, "제육볶음 품질 판정과 승인된 사유코드가 필요합니다.")
        if (body.quality_decision == "QUALITY_FAILED") != bool(body.quality_reason_codes):
            raise HTTPException(422, "품질 실패에는 하나 이상의 사유가, 통과에는 실패 사유가 없어야 합니다.")
    elif body.actual_mass_kg > SYNTHETIC_FRYER_BATCH_KG or body.quality_decision is not None or \
            body.quality_reason_codes or body.quality_note:
        raise HTTPException(422, "돈까스 첫 척도에는 합성 튀김기 적재량만 적용합니다.")
    reference_kg = SYNTHETIC_JEYUK_RECOMMENDED_KG if old["menu"] == "JEYUK" else SYNTHETIC_FRYER_BATCH_KG
    calculated_load = (body.actual_mass_kg / reference_kg * 100).quantize(Decimal("0.01"))
    expected_band = ("PARTIAL" if calculated_load <= 50 else "REFERENCE" if calculated_load <= 100
                     else "OVER_RECOMMENDED")
    if body.load_percent != calculated_load or body.load_band != expected_band:
        raise HTTPException(422, "합성 장비 적재율과 구간이 일치하지 않습니다.")
    if (current_version == 0 and body.kind != "INITIAL") or (current_version > 0 and body.kind == "INITIAL"):
        raise HTTPException(422, "최초 평가와 수정·재평가 유형이 일치하지 않습니다.")
    if current_version > 0 and not body.reason:
        raise HTTPException(422, "수정·재평가 사유가 필요합니다.")
    total = sum(body.scores.values())
    score = (Decimal(total) / Decimal(35) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    score_passed = min(body.scores.values()) >= 3 and score >= 60
    if old["menu"] == "JEYUK" and body.quality_decision == "QUALITY_PASSED" and not score_passed:
        raise HTTPException(422, "품질 합격 점수 기준에 미달한 평가는 통과시킬 수 없습니다.")
    quality_passed = score_passed and body.quality_decision != "QUALITY_FAILED"
    version = current_version + 1
    assessment = {
        "sk": f"ASSESSMENT#REV#{version:012d}", "version": version,
        "previous_version": current_version or None, "kind": body.kind, "reason": body.reason,
        "status": "COMPLETED", "scores": body.scores, "observations": body.observations,
        "score_total": total, "score_percent": str(score),
        "quality_passed": quality_passed,
        "quality_target": quality_passed and min(body.scores.values()) >= 4 and score >= 80,
        "quality_status": "QUALITY_PASSED" if quality_passed else "QUALITY_FAILED",
        "quality_decision": body.quality_decision,
        "quality_reason_codes": body.quality_reason_codes,
        "quality_note": body.quality_note,
        "overall_satisfaction": body.overall_satisfaction,
        "scale_version": body.scale_version, "formula_version": "quality-v1",
        "safety_version": old["safety_version"], "job_state_version": old["job_state_version"],
        "menu": old["menu"], "equipment_profile": old["equipment_profile"],
        "planned_portions": body.planned_portions, "actual_portions": body.actual_portions,
        "actual_mass_kg": str(body.actual_mass_kg), "load_percent": str(body.load_percent),
        "mass_per_portion_kg": str((body.actual_mass_kg / body.actual_portions).quantize(Decimal("0.001"))),
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
            "jeyuk_rule_version": old.get("jeyuk_rule_version"),
            "jeyuk_size_profile": old.get("jeyuk_size_profile"),
            "actual_load_kg": old.get("actual_load_kg"),
            "process_load_status": old.get("process_load_status"),
            "status": "PENDING", "version": 1, "job_state_version": 1,
            "safety_status": "NOT_MEASURED", "safety_version": 0,
            "evaluation_blocked": True, "evaluation_status": "NOT_COMPLETED",
            "safety_hold_requested_at": None, "manual_stop_confirmed_at": None,
            "manual_stopped_at": None,
            "created_at": _now(), "updated_at": _now(),
            "source_job_id": job_id, "recook_reason": body.reason, "source_binding": None,
            "validation_context": None, "validation_context_digest": None,
            "validation_context_version": 0, "validation_context_status": "NOT_RECORDED",
            "actual_placed_at": None, "actual_lifted_at": None,
            "completion_recorded_at": None, "actual_cooking_seconds": None,
            "process_assessment_status": ("WARNING" if old.get("process_load_status") == "OVER_RECOMMENDED_LOAD"
                                          else "INSUFFICIENT_DATA") if old["menu"] == "JEYUK" else
                                         process_status("NOT_MEASURED", old.get("process_thickness_status", "NOT_MEASURED"), "NOT_MEASURED"),
            "process_temperature_status": "NOT_MEASURED",
            "process_surface_status": "NOT_MEASURED", "process_rpm_status": "NOT_MEASURED",
            "auto_safety_status": "NOT_MEASURED", "mixture_hold_seconds": 0,
            "safety_passed_at_second": None,
            "discharge_check_version": 0, "discharge_check_status": "NOT_MEASURED",
            "process_thickness_status": old.get("process_thickness_status", "NOT_MEASURED"),
            "process_time_status": "NOT_MEASURED"}
    try:
        await _store_call(store, store.create_recook, parent_pk, body.expected_version,
                          _pk(session.subject, new_id), meta)
    except Conflict as exc:
        raise _conflict() from exc
    return meta
