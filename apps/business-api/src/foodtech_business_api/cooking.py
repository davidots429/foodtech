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
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from foodtech_contracts import stream_binding_key
from foodtech_contracts.cooking_live_time import observe as observe_live_time
from foodtech_contracts.tonkatsu_process import (RULE_VERSION, TEMPERATURE_RULE_VERSION,
                                                 cooking_time_status, process_status, thickness_profile)
from foodtech_contracts.tonkatsu_batch import batch as approved_tonkatsu_batch, RATED_KG
from foodtech_contracts.jeyuk_process import (RULE_VERSION as JEYUK_RULE_VERSION,
                                              cooking_time_status as jeyuk_cooking_time_status,
                                              load_status as jeyuk_load_status, PROFILES as JEYUK_PROFILES)

from .auth import _csrf, _session
from .cooking_store import Conflict, MemoryCookingStore, get_cooking_store

router = APIRouter(prefix="/api/customer/cooking-jobs", tags=["cooking-jobs"])
logger = logging.getLogger(__name__)
SYNTHETIC_FRYER_BATCH_KG = Decimal("0.8")
JEYUK_EQUIPMENT = {"jeyuk-small": "SMALL", "jeyuk-medium": "MEDIUM", "jeyuk-large": "LARGE"}
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


def _jeyuk_feed_safety_current(job: dict) -> bool:
    return job.get("menu") != "JEYUK" or job.get("jeyuk_feed_safety_aligned") is True


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
    planned_portions: int = Field(gt=0, le=1000)
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


class TonkatsuBatchInput(StrictModel):
    expected_version: int = Field(ge=1)
    mode: Literal["REFERENCE", "PARTIAL"]
    product_weights_g: list[StrictInt] = Field(min_length=1, max_length=18)


class TonkatsuInitialInput(StrictModel):
    expected_version: int = Field(ge=1)
    ingredient_temperature_c: Decimal | None = Field(default=None, ge=Decimal("-273.15"),
                                                       le=Decimal("300"), max_digits=6, decimal_places=2)
    temperature_observed_at: datetime | None = None
    storage_state: Literal["CHILLED", "FROZEN", "OTHER", "UNKNOWN"] = "UNKNOWN"
    oil_capacity_l: Decimal | None = Field(default=None, gt=0, max_digits=7, decimal_places=2)
    oil_batches_since_change: StrictInt | None = Field(default=None, ge=0, le=1000000)
    oil_cooked_mass_kg: Decimal | None = Field(default=None, ge=0, max_digits=9, decimal_places=3)
    oil_changed_at: datetime | None = None


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


class JeyukFeed(StrictModel):
    expected_version: StrictInt = Field(ge=1)
    started_at: datetime
    completed_at: datetime
    mass_kg: Decimal = Field(gt=0, le=100, max_digits=6, decimal_places=3)
    initial_temperature_c: Decimal | None = Field(default=None, ge=Decimal("-273.15"), le=Decimal("300"), max_digits=6, decimal_places=2)
    temperature_observed_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=500)


class JeyukDischarge(StrictModel):
    expected_version: StrictInt = Field(ge=1)
    completed_at: datetime
    reason: str | None = Field(default=None, max_length=500)


class CoreSample(StrictModel):
    at_second: int = Field(ge=0, le=60)
    celsius: Decimal | None = Field(default=None, ge=Decimal("-273.15"), le=Decimal("300"))


class Probe(StrictModel):
    item_id: str = Field(min_length=1, max_length=80)
    calibration_status: Literal["OK", "EXPIRED"]
    samples: list[CoreSample] = Field(min_length=1, max_length=61)
    probe_id: str | None = Field(default=None, min_length=1, max_length=80)
    removal_sequence: StrictInt | None = Field(default=None, ge=1, le=18)
    measurement_start: datetime | None = None
    measurement_end: datetime | None = None

    @model_validator(mode="after")
    def complete_measurement_metadata(self):
        values = (self.probe_id, self.removal_sequence, self.measurement_start, self.measurement_end)
        if any(value is not None for value in values):
            if any(value is None for value in values):
                raise ValueError("probe measurement metadata must be complete")
            assert self.measurement_start is not None and self.measurement_end is not None
            if self.measurement_start.utcoffset() is None or self.measurement_end.utcoffset() is None or \
                    (self.measurement_end - self.measurement_start).total_seconds() != 60:
                raise ValueError("probe measurement requires a timezone and a 60-second interval")
        return self


class CoreMockEvidence(StrictModel):
    schema_version: Literal["tonkatsu-core-samples-v1"]
    provenance: Literal["SYNTHETIC"]
    generator_version: str = Field(min_length=1, max_length=40)
    fixture_version: str = Field(min_length=1, max_length=80)
    seed: StrictInt = Field(ge=0)
    scenario: Literal["NORMAL", "LOW_CORE", "MISSING_SAMPLE", "EXPIRED_CALIBRATION"]
    measurement_start_utc: datetime
    measurement_end_utc: datetime


class CoreCheck(StrictModel):
    expected_version: int = Field(ge=1)
    probes: list[Probe] = Field(min_length=1, max_length=18)
    mock_evidence: CoreMockEvidence | None = None

    @model_validator(mode="after")
    def matches_mock_measurements(self):
        if self.mock_evidence:
            source = self.mock_evidence
            if source.measurement_start_utc.utcoffset() is None or source.measurement_end_utc.utcoffset() is None or \
                    (source.measurement_end_utc - source.measurement_start_utc).total_seconds() != 60 or \
                    any(probe.measurement_start != source.measurement_start_utc or
                        probe.measurement_end != source.measurement_end_utc or not probe.probe_id
                        for probe in self.probes):
                raise ValueError("mock evidence does not match the probe measurement interval")
        return self


class TonkatsuReheat(StrictModel):
    expected_version: StrictInt = Field(ge=1)
    started_at: datetime
    lifted_at: datetime
    reason: str = Field(min_length=1, max_length=500)


class CustomerRestatementRequest(StrictModel):
    expected_version: StrictInt = Field(ge=1)
    request_id: uuid.UUID


def _probe_summary(probe: Probe) -> dict:
    values = [sample.celsius for sample in probe.samples if sample.celsius is not None]
    complete = (probe.calibration_status == "OK" and len(probe.samples) == 61 and
                [sample.at_second for sample in probe.samples] == list(range(61)) and len(values) == 61)
    below = probe.calibration_status == "OK" and any(value < 75 for value in values)
    longest = current = 0
    previous = None
    for sample in probe.samples:
        if sample.celsius is not None and sample.celsius >= 75:
            current = current + 1 if previous is not None and sample.at_second == previous + 1 else 1
            longest = max(longest, current)
        else:
            current = 0
        previous = sample.at_second
    return {"item_id": probe.item_id, "probe_id": probe.probe_id, "removal_sequence": probe.removal_sequence,
            "minimum_celsius": str(min(values)) if values else None,
            "hold_seconds": max(longest - 1, 0) if probe.calibration_status == "OK" else None,
            "status": "FAILED" if below else "PASSED" if complete else "INCOMPLETE"}


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
    actual_portions: int = Field(gt=0, le=1000)
    actual_mass_kg: Decimal = Field(gt=0, le=100)
    load_percent: Decimal = Field(gt=0, le=167)
    load_band: Literal["PARTIAL", "REFERENCE", "OVER_RECOMMENDED"]


class Recook(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


@router.post("", status_code=201)
async def create_job(request: Request, body: NewJob) -> dict:
    session, store = await _context(request, write=True)
    if body.equipment_profile in {"synthetic-fryer", "synthetic-fryer-approved-v2"}:
        if body.thickness_cm is None or body.actual_load_kg is not None or body.planned_portions > 18:
            raise HTTPException(422, "합성 돈까스 작업에는 두께만 입력해야 합니다.")
        if body.equipment_profile == "synthetic-fryer" and body.planned_portions > 4:
            raise HTTPException(422, "이전 합성 fixture는 최대 4인분입니다.")
        thickness_status, thickness_band = thickness_profile(body.thickness_cm)
        if thickness_status == "ABOVE_RANGE":
            raise HTTPException(422, "PROFILE_REJECTED: 두께가 합성 돈까스 프로파일 상한 2.0cm를 넘습니다.")
        menu, load_status = "TONKATSU", None
    elif body.equipment_profile in JEYUK_EQUIPMENT:
        jeyuk_size = JEYUK_EQUIPMENT[body.equipment_profile]
        if body.actual_load_kg is None or body.thickness_cm is not None or \
                (jeyuk_size == "SMALL" and body.planned_portions > 4):
            raise HTTPException(422, "합성 제육볶음 작업에는 실제 투입량만 입력해야 합니다.")
        load_status = jeyuk_load_status(jeyuk_size, body.actual_load_kg)
        if load_status == "RATED_LOAD_EXCEEDED":
            raise HTTPException(422, "PROFILE_REJECTED: 합성 볶음·교반기의 정격 하중을 넘습니다.")
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
        "jeyuk_size_profile": JEYUK_EQUIPMENT[body.equipment_profile] if menu == "JEYUK" else None,
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


@router.post("/{job_id}/tonkatsu-batch")
async def record_tonkatsu_batch(job_id: str, request: Request, body: TonkatsuBatchInput) -> dict:
    session, store, pk, old, _ = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old.get("menu") != "TONKATSU" or old.get("provenance") != "SYNTHETIC" or \
            old.get("equipment_profile") != "synthetic-fryer-approved-v2":
        raise HTTPException(422, "합성 돈까스 작업에만 배치를 기록할 수 있습니다.")
    if old["status"] not in {"PENDING", "READY"}:
        raise HTTPException(409, "조리 시작 전에만 배치를 기록할 수 있습니다.")
    try:
        value = approved_tonkatsu_batch(old.get("thickness_profile"), body.product_weights_g, body.mode)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    version = int(old.get("tonkatsu_batch_version", 0)) + 1
    now = _now()
    meta = {**old, "tonkatsu_batch": value, "tonkatsu_batch_version": version,
            "version": int(old["version"]) + 1, "updated_at": now}
    event = {"sk": f"EVENT#TONKATSU_BATCH#{version:012d}", "domain": "TONKATSU_BATCH",
             "trigger": "TONKATSU_BATCH_RECORDED", "batch": value,
             "actor_subject": session.subject, "occurred_at": now}
    await _commit(store, pk, old, meta, event=event)
    return meta


@router.post("/{job_id}/tonkatsu-initial-context")
async def record_tonkatsu_initial_context(job_id: str, request: Request, body: TonkatsuInitialInput) -> dict:
    session, store, pk, old, _ = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old.get("equipment_profile") != "synthetic-fryer-approved-v2" or old.get("provenance") != "SYNTHETIC":
        raise HTTPException(422, "승인된 합성 돈까스 프로파일에서만 기록할 수 있습니다.")
    if old["status"] not in {"PENDING", "READY"} or not old.get("tonkatsu_batch"):
        raise HTTPException(409, "조리 시작 전 배치를 먼저 기록해 주세요.")
    if (body.ingredient_temperature_c is None) != (body.temperature_observed_at is None):
        raise HTTPException(422, "초기 온도와 관찰 시각은 함께 입력해야 합니다.")
    if body.storage_state == "UNKNOWN" and all(value is None for value in (
            body.ingredient_temperature_c, body.oil_capacity_l, body.oil_batches_since_change,
            body.oil_cooked_mass_kg, body.oil_changed_at)):
        raise HTTPException(422, "기록할 합성 값을 하나 이상 입력해 주세요.")
    now = datetime.now(timezone.utc)
    for value in (body.temperature_observed_at, body.oil_changed_at):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None or value.astimezone(timezone.utc) > now):
            raise HTTPException(422, "합성 관찰 시각에는 시간대가 필요하며 미래일 수 없습니다.")
    value = {"schema_version": "tonkatsu-initial-context-v1", "provenance": "SYNTHETIC",
             "status": "DECLARED_UNVERIFIED", "batch_version": int(old["tonkatsu_batch_version"]),
             "product_classification": "NON_MINCED_PORK_LOIN_RAW",
             "ingredient_temperature_c": str(body.ingredient_temperature_c) if body.ingredient_temperature_c is not None else None,
             "temperature_observed_at": body.temperature_observed_at.astimezone(timezone.utc).isoformat() if body.temperature_observed_at else None,
             "storage_state": body.storage_state,
             "oil_capacity_l": str(body.oil_capacity_l) if body.oil_capacity_l is not None else None,
             "oil_batches_since_change": body.oil_batches_since_change,
             "oil_cooked_mass_kg": str(body.oil_cooked_mass_kg) if body.oil_cooked_mass_kg is not None else None,
             "oil_changed_at": body.oil_changed_at.astimezone(timezone.utc).isoformat() if body.oil_changed_at else None}
    version = int(old.get("tonkatsu_initial_context_version", 0)) + 1
    timestamp = now.isoformat().replace("+00:00", "Z")
    meta = {**old, "tonkatsu_initial_context": value, "tonkatsu_initial_context_version": version,
            "version": int(old["version"]) + 1, "updated_at": timestamp}
    event = {"sk": f"EVENT#TONKATSU_INITIAL_CONTEXT#{version:012d}", "domain": "TONKATSU_INITIAL_CONTEXT",
             "trigger": "TONKATSU_INITIAL_CONTEXT_RECORDED", "context": value,
             "actor_subject": session.subject, "occurred_at": timestamp}
    await _commit(store, pk, old, meta, event=event)
    return meta


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


@router.get("/{job_id}/collection-evidence")
async def jeyuk_collection_evidence(job_id: str, request: Request) -> dict:
    """Report an observed synthetic collection receipt, never authorize a model."""
    _, store, pk, job, _ = await _read(request, job_id)
    if job.get("menu") != "JEYUK":
        raise HTTPException(422, "제육볶음 작업에만 수집 증거를 조회할 수 있습니다.")
    binding = job.get("source_binding")
    table = os.getenv("FOODTECH_DATA_TABLE", "")
    if not isinstance(binding, dict) or not binding.get("batch_id"):
        return {"status": "NOT_BOUND", "raw_count": None}
    if not table:
        raise HTTPException(503, "원천 수집 저장소가 설정되지 않았습니다.")
    from .jeyuk_source_evidence import read_collection_completion
    try:
        result = await asyncio.to_thread(read_collection_completion, table, binding["batch_id"])
    except Exception as exc:
        raise HTTPException(503, "원천 수집 증거를 조회하지 못했습니다.") from exc
    latest, _ = await _store_call(store, store.read, pk)
    if not latest or latest.get("version") != job.get("version"):
        raise HTTPException(409, "조리작업이 조회 중 변경되었습니다. 다시 확인해 주세요.")
    return {**result, "job_id": job_id, "job_version": job["version"]}


@router.get("/{job_id}/validation-evidence")
async def jeyuk_validation_evidence(job_id: str, request: Request) -> dict:
    _, store, pk, job, _ = await _read(request, job_id)
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
                     "source_relative_duration_covers_cooking": False,
                     "source_process_span_covers_cooking": False}
    source_table = os.getenv("FOODTECH_DATA_TABLE", "")
    if process_linked and context_matches_job and source_table and second <= max(
            profile.time_bands[-1] for profile in JEYUK_PROFILES.values()):
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
        "current_safety_passed": (job.get("safety_status") == "PASSED" and
                                  not job.get("evaluation_blocked") and _jeyuk_feed_safety_current(job)),
        **source_checks,
    }
    latest, _ = await _store_call(store, store.read, pk)
    if not latest or latest.get("version") != job.get("version"):
        raise HTTPException(409, "조리작업이 조회 중 변경되었습니다. 다시 확인해 주세요.")
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
                                   "PROCESS_CONDITION_PROFILE_IDENTITY"] +
                                  ([] if checks["source_process_span_covers_cooking"] else ["SOURCE_TIMELINE_ALIGNMENT"]),
    }


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request, response: Response,
                  process_view: Literal["LATEST_RESTATED", "AS_PROCESSED"] = "LATEST_RESTATED") -> dict:
    response.headers["Cache-Control"] = "no-store"
    _, store, pk, meta, pointer = await _read(request, job_id)
    if not _jeyuk_feed_safety_current(meta):
        meta = {**meta, "safety_status": "INCOMPLETE" if meta.get("safety_status") == "PASSED" else meta["safety_status"],
                "evaluation_blocked": True}
        if pointer and pointer.get("usable"):
            pointer = {**pointer, "usable": False, "blocked_reason": "JEYUK_FEED_SAFETY_UNVERIFIED"}
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


@router.get("/{job_id}/prior-job-candidate")
async def prior_job_candidate(job_id: str, request: Request, response: Response) -> dict:
    from .prior_job_discovery import discover
    response.headers["Cache-Control"] = "no-store"
    return await discover(request, job_id)


@router.get("/{job_id}/recommendation-evidence")
async def recommendation_evidence(job_id: str, request: Request, response: Response,
                                  comparison_id: str | None = None, prior_job_id: str | None = None) -> dict:
    from .recommendation_evidence import read_evidence
    response.headers["Cache-Control"] = "no-store"
    return await read_evidence(request, job_id, comparison_id, prior_job_id=prior_job_id)


@router.post("/{job_id}/process-restatements", status_code=202)
async def request_process_restatement(job_id: str, request: Request, body: CustomerRestatementRequest) -> dict:
    from .restatement_queue import send, source_revision
    _, store, pk, job, _ = await _read(request, job_id, write=True)
    request_id = str(body.request_id)
    existing = await _store_call(store, store.customer_restatement, pk, request_id)
    if existing:
        if existing.get("reason") != "CUSTOMER_REFRESH" or \
                existing.get("job_version_at_detection") != body.expected_version:
            raise HTTPException(409, "재계산 요청 ID가 다른 요청에 사용되었습니다.")
        if existing["status"] == "COMPLETED":
            return {"request_id": request_id, "status": "COMPLETED"}
    else:
        if int(job["version"]) != body.expected_version:
            raise _conflict()
        if job.get("provenance") != "SYNTHETIC" or job.get("menu") != "TONKATSU" or \
                job.get("status") not in {"COMPLETED", "FAILED"} or not job.get("source_binding"):
            raise HTTPException(409, "원천이 연결된 완료 돈까스 작업만 재계산할 수 있습니다.")
    queue_url = os.getenv("FOODTECH_ORDERED_QUEUE_URL", "")
    table_name = os.getenv("FOODTECH_DATA_TABLE", "")
    cooking_table = os.getenv("FOODTECH_COOKING_TABLE", "")
    if not queue_url or not table_name or not cooking_table:
        raise HTTPException(503, "공정 재계산 저장소 또는 대기열이 설정되지 않았습니다.")
    if not existing:
        stream = stream_binding_key(**job["source_binding"])
        try:
            revision = await asyncio.to_thread(source_revision, table_name, stream)
        except Exception as exc:
            logger.exception("Unable to read restatement source revision")
            raise HTTPException(503, "원천 상태를 확인하지 못했습니다.") from exc
        if revision is None:
            raise HTTPException(409, "재계산할 원천 버전이 없습니다.")
        record = {"sk": f"RESTATEMENT#REQUEST#{request_id}", "request_id": request_id,
                  "status": "PENDING", "reason": "CUSTOMER_REFRESH", "source_stream_pk": stream,
                  "source_revision_at_request": revision, "job_id": job["job_id"],
                  "job_version_at_detection": body.expected_version, "provenance": "SYNTHETIC",
                  "requested_at": _now()}
        try:
            existing = await _store_call(store, store.create_customer_restatement, pk, body.expected_version, record)
        except Conflict as exc:
            raise _conflict() from exc
    if existing["status"] == "PENDING":
        try:
            await asyncio.to_thread(send, queue_url, pk, request_id)
        except Exception as exc:
            logger.exception("Unable to dispatch restatement request")
            raise HTTPException(503, "요청은 저장되었으나 전달되지 않았습니다. 같은 요청으로 다시 시도해 주세요.") from exc
    return {"request_id": request_id, "status": existing["status"]}


@router.get("/{job_id}/process-restatements/{request_id}")
async def process_restatement_status(job_id: str, request_id: uuid.UUID, request: Request, response: Response) -> dict:
    _, store, pk, _, _ = await _read(request, job_id)
    item = await _store_call(store, store.customer_restatement, pk, str(request_id))
    if not item or item.get("reason") != "CUSTOMER_REFRESH":
        raise HTTPException(404, "재계산 요청을 찾을 수 없습니다.")
    response.headers["Cache-Control"] = "no-store"
    return {"request_id": str(request_id), "status": item["status"]}


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
    if body.event == "COOKING_STARTED" and old.get("equipment_profile") == "synthetic-fryer-approved-v2" and \
            not old.get("tonkatsu_batch"):
        raise HTTPException(409, "조리 시작 전에 합성 돈까스 배치를 기록해 주세요.")
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
        meat_feed = (old.get("jeyuk_feeds") or {}).get("MEAT") if old["menu"] == "JEYUK" else None
        if meat_feed and datetime.fromisoformat(meat_feed["completed_at"].replace("Z", "+00:00")) != placed_at:
            raise HTTPException(422, "조리 시작은 기록된 고기 투입 완료 시각과 같아야 합니다.")
        initial = old.get("tonkatsu_initial_context") or {}
        if initial.get("batch_version") == old.get("tonkatsu_batch_version") and initial:
            for field in ("temperature_observed_at", "oil_changed_at"):
                if initial.get(field) and datetime.fromisoformat(initial[field].replace("Z", "+00:00")) > placed_at:
                    raise HTTPException(422, "초기 온도 관찰과 기름 교체 시각은 투입 이전이어야 합니다.")
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
        if old["menu"] == "JEYUK" and any(
                datetime.fromisoformat(feed["completed_at"].replace("Z", "+00:00")) > lifted_at
                for feed in (old.get("jeyuk_feeds") or {}).values()):
            raise HTTPException(422, "배출 시작 시각은 기록된 재료 투입 완료 이후여야 합니다.")
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


def _jeyuk_total_seconds(feeds: dict, completed: datetime) -> str | None:
    meat = feeds.get("MEAT")
    if not meat:
        return None
    started = datetime.fromisoformat(meat["started_at"].replace("Z", "+00:00"))
    elapsed = completed - started
    microseconds = (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    return str(Decimal(microseconds) / Decimal(1_000_000))


@router.post("/{job_id}/jeyuk-feeds/{ingredient}", status_code=201)
async def record_jeyuk_feed(job_id: str, ingredient: Literal["meat", "vegetable", "sauce"],
                            request: Request, body: JeyukFeed) -> dict:
    session, store, pk, old, pointer = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    if old["menu"] != "JEYUK" or old["status"] == "CANCELED":
        raise HTTPException(409, "취소되지 않은 제육볶음 작업에만 투입을 기록합니다.")
    if body.started_at.utcoffset() is None or body.completed_at.utcoffset() is None:
        raise HTTPException(422, "투입 시각에는 시간대가 필요합니다.")
    started, completed = body.started_at.astimezone(timezone.utc), body.completed_at.astimezone(timezone.utc)
    if started > completed or completed > datetime.now(timezone.utc):
        raise HTTPException(422, "투입 시작·완료 순서와 시각을 확인해 주세요.")
    previous_placed = old.get("actual_placed_at")
    if ingredient == "meat" and previous_placed and completed != datetime.fromisoformat(previous_placed.replace("Z", "+00:00")):
        raise HTTPException(422, "고기 투입 완료는 기록된 조리 시작 시각과 같아야 합니다.")
    previous_lifted = old.get("actual_lifted_at")
    if previous_lifted and completed > datetime.fromisoformat(previous_lifted.replace("Z", "+00:00")):
        raise HTTPException(422, "투입 완료는 배출 시작 이전이어야 합니다.")
    key = ingredient.upper()
    feeds = dict(old.get("jeyuk_feeds") or {})
    previous = feeds.get(key)
    if previous and not (body.reason and body.reason.strip()):
        raise HTTPException(422, "투입 기록 수정 사유가 필요합니다.")
    temperature_fields = {"initial_temperature_c", "temperature_observed_at"}
    supplied = temperature_fields & body.model_fields_set
    if supplied and supplied != temperature_fields:
        raise HTTPException(422, "초기 온도와 관찰 시각을 함께 입력해야 합니다.")
    if supplied:
        temperature, observed = body.initial_temperature_c, body.temperature_observed_at
    else:
        temperature = previous.get("initial_temperature_c") if previous else None
        observed = (datetime.fromisoformat(previous["temperature_observed_at"].replace("Z", "+00:00"))
                    if previous and previous.get("temperature_observed_at") else None)
    if (temperature is None) != (observed is None):
        raise HTTPException(422, "초기 온도와 관찰 시각을 함께 입력해야 합니다.")
    if observed is not None and (observed.utcoffset() is None or observed.astimezone(timezone.utc) > started):
        raise HTTPException(422, "초기 온도 관찰 시각은 시간대를 포함하며 투입 시작 이전이어야 합니다.")
    group_id = old.get("jeyuk_addition_group_id")
    if ingredient != "meat" and not group_id:
        group_id = str(uuid.uuid4())
    now = _now()
    current = {"ingredient": key, "started_at": started.isoformat().replace("+00:00", "Z"),
               "completed_at": completed.isoformat().replace("+00:00", "Z"), "mass_kg": str(body.mass_kg),
               "initial_temperature_c": str(temperature) if temperature is not None else None,
               "temperature_observed_at": observed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if observed else None,
               "addition_group_id": group_id if ingredient != "meat" else None,
               "provenance": "SYNTHETIC", "recorded_at": now}
    feeds[key] = current
    total_mass = sum((Decimal(feed["mass_kg"]) for feed in feeds.values()), Decimal(0))
    mass_status = ("INCOMPLETE" if len(feeds) != 3 else
                   "MATCHED" if total_mass == Decimal(str(old["actual_load_kg"])) else "MISMATCH")
    simultaneous = None
    if "VEGETABLE" in feeds and "SAUCE" in feeds:
        times = [datetime.fromisoformat(feeds[name]["started_at"].replace("Z", "+00:00"))
                 for name in ("VEGETABLE", "SAUCE")]
        simultaneous = abs((times[0] - times[1]).total_seconds()) <= 30
    version = int(old["version"]) + 1
    meta = {**old, "version": version, "updated_at": now, "jeyuk_feeds": feeds,
            "jeyuk_addition_group_id": group_id, "jeyuk_additions_simultaneous": simultaneous,
            "jeyuk_feed_total_kg": str(total_mass), "jeyuk_feed_mass_status": mass_status,
            "jeyuk_feed_safety_aligned": False}
    if old.get("safety_status") == "PASSED":
        meta.update(safety_status="INCOMPLETE", safety_version=int(old["safety_version"]) + 1,
                    evaluation_blocked=True)
    if old.get("jeyuk_discharge"):
        discharge = old["jeyuk_discharge"]
        discharge_completed = datetime.fromisoformat(discharge["completed_at"].replace("Z", "+00:00"))
        meta["jeyuk_discharge"] = {**discharge, "total_process_duration_seconds":
                                   _jeyuk_total_seconds(feeds, discharge_completed)}
    event = {"sk": f"EVENT#JEYUK_FEED#{version:012d}", "domain": "JEYUK_FEED",
             "trigger": "FEED_REVISED" if previous else "FEED_RECORDED", "ingredient": key,
             "previous": previous, "feed": current, "reason": body.reason.strip() if body.reason else None,
             "actor": session.subject, "occurred_at": now}
    await _commit(store, pk, old, meta,
                  pointer=_blocked_pointer(pointer, "JEYUK_FEED_CHANGED"),
                  event=event)
    return meta


@router.post("/{job_id}/jeyuk-discharge", status_code=201)
async def record_jeyuk_discharge(job_id: str, request: Request, body: JeyukDischarge) -> dict:
    session, store, pk, old, _ = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    started_text = old.get("actual_lifted_at")
    if old["menu"] != "JEYUK" or old["status"] not in {"COMPLETED", "FAILED"} or not started_text:
        raise HTTPException(409, "제육볶음 배출 시작을 기록한 후에만 완료를 기록합니다.")
    if body.completed_at.utcoffset() is None:
        raise HTTPException(422, "배출 완료 시각에는 시간대가 필요합니다.")
    started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
    completed = body.completed_at.astimezone(timezone.utc)
    if completed < started or completed > datetime.now(timezone.utc):
        raise HTTPException(422, "배출 완료는 시작 이후이면서 현재 이전이어야 합니다.")
    previous = old.get("jeyuk_discharge")
    if previous and not (body.reason and body.reason.strip()):
        raise HTTPException(422, "배출 기록 수정 사유가 필요합니다.")
    method = {"SMALL": "MANUAL_POUR", "MEDIUM": "SEMI_AUTOMATIC_TILT",
              "LARGE": "AUTOMATIC_BOTTOM_TILT_PUMP"}[old["jeyuk_size_profile"]]
    elapsed = completed - started
    microseconds = (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    now = _now()
    current = {"started_at": started_text, "completed_at": completed.isoformat().replace("+00:00", "Z"),
               "duration_seconds": str(Decimal(microseconds) / Decimal(1_000_000)),
               "total_process_duration_seconds": _jeyuk_total_seconds(old.get("jeyuk_feeds") or {}, completed),
               "method": method, "method_basis": "SYNTHETIC_ASSUMPTION", "recorded_at": now}
    version = int(old["version"]) + 1
    meta = {**old, "version": version, "updated_at": now, "jeyuk_discharge": current}
    event = {"sk": f"EVENT#JEYUK_DISCHARGE#{version:012d}", "domain": "JEYUK_DISCHARGE",
             "trigger": "DISCHARGE_REVISED" if previous else "DISCHARGE_COMPLETED", "previous": previous,
             "discharge": current, "reason": body.reason.strip() if body.reason else None,
             "actor": session.subject, "occurred_at": now}
    await _commit(store, pk, old, meta, event=event)
    return meta


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
    identified = [probe for probe in body.probes if probe.probe_id is not None]
    if identified and (len(identified) != len(body.probes) or
            len({probe.probe_id for probe in identified}) != len(identified) or
            sorted(probe.removal_sequence for probe in identified) != list(range(1, len(identified) + 1))):
        raise HTTPException(422, "제품별 탐침 ID와 분리 순번을 모두 고유하게 기록해야 합니다.")
    batch_context = old.get("tonkatsu_batch")
    if old.get("equipment_profile") == "synthetic-fryer-approved-v2" and not batch_context:
        raise HTTPException(409, "해당 작업의 합성 배치 기록이 없습니다.")
    required_probes = batch_context["product_count"] if batch_context else old["planned_portions"]
    if len(body.probes) != required_probes:
        raise HTTPException(422, "기록된 제품마다 독립 탐침 기록이 필요합니다.")
    reheat = old.get("pending_tonkatsu_reheat")
    if reheat:
        lifted = datetime.fromisoformat(reheat["lifted_at"].replace("Z", "+00:00"))
        previous_ids = set(reheat["item_ids"])
        if set(ids) != previous_ids or any(probe.measurement_start != lifted or
                probe.measurement_end is None or probe.measurement_end > datetime.now(timezone.utc)
                for probe in body.probes):
            raise HTTPException(422, "추가 가열한 같은 제품의 인양 직후 60초 표본을 기록해야 합니다.")
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
            "core_check_safety_version": int(old["safety_version"]) + 1,
            "core_check_summary": [_probe_summary(probe) for probe in body.probes],
            "core_check_mock_evidence": body.mock_evidence.model_dump(mode="json") if body.mock_evidence else None,
            "core_check_by": session.subject,
            "pending_tonkatsu_reheat": reheat if status == "INCOMPLETE" else None, "updated_at": _now()}
    event = {"sk": f"EVENT#SAFETY#{int(meta['safety_version']):012d}",
             "domain": "SAFETY_STATUS", "trigger": "CORE_CHECK",
             "from_status": old["safety_status"], "to_status": status,
             "probes": meta["core_check"], "mock_evidence": meta["core_check_mock_evidence"],
             "reheat_segment_id": reheat["segment_id"] if reheat else None,
             "actor": session.subject, "occurred_at": _now()}
    await _commit(store, pk, old, meta, pointer=_blocked_pointer(pointer, f"SAFETY_{status}"), event=event)
    return meta


@router.post("/{job_id}/safety/tonkatsu-reheat", status_code=201)
async def tonkatsu_reheat(job_id: str, request: Request, body: TonkatsuReheat) -> dict:
    session, store, pk, old, pointer = await _read(request, job_id, write=True)
    if int(old["version"]) != body.expected_version:
        raise _conflict()
    probes = old.get("core_check")
    if old.get("menu") != "TONKATSU" or old.get("status") != "COMPLETED" or \
            old.get("safety_status") != "FAILED" or old.get("core_check_safety_version") != old.get("safety_version") or \
            not isinstance(probes, list) or not probes:
        raise HTTPException(409, "심부온도 안전 실패가 기록된 완료 작업에서만 추가 가열을 기록합니다.")
    if not body.reason.strip() or any(value.utcoffset() is None for value in (body.started_at, body.lifted_at)):
        raise HTTPException(422, "추가 가열 사유와 시간대가 포함된 시각이 필요합니다.")
    try:
        starts = [datetime.fromisoformat(probe["measurement_start"].replace("Z", "+00:00")) for probe in probes]
        ends = [datetime.fromisoformat(probe["measurement_end"].replace("Z", "+00:00")) for probe in probes]
        original_lifted = datetime.fromisoformat(old["actual_lifted_at"].replace("Z", "+00:00"))
        if original_lifted.utcoffset() is None or any(value.utcoffset() is None for value in starts + ends) or \
                any(start < original_lifted or end > datetime.now(timezone.utc) for start, end in zip(starts, ends)):
            raise ValueError("timezone required")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(409, "기존 안전 측정의 종료 시각을 확인할 수 없습니다.")
    started, lifted = body.started_at.astimezone(timezone.utc), body.lifted_at.astimezone(timezone.utc)
    if started < max(ends) or lifted <= started or lifted > datetime.now(timezone.utc):
        raise HTTPException(422, "추가 가열은 이전 안전 측정 이후 시작하고 현재 이전에 인양해야 합니다.")
    segment = {"segment_id": str(uuid.uuid4()), "started_at": started.isoformat().replace("+00:00", "Z"),
               "lifted_at": lifted.isoformat().replace("+00:00", "Z"),
               "duration_seconds": str(Decimal(str((lifted - started).total_seconds()))),
               "reason": body.reason.strip(), "previous_safety_version": int(old["safety_version"]),
               "item_ids": [probe["item_id"] for probe in probes]}
    now = _now()
    meta = {**old, "version": int(old["version"]) + 1, "safety_version": int(old["safety_version"]) + 1,
            "safety_status": "INCOMPLETE", "evaluation_blocked": True, "pending_tonkatsu_reheat": segment,
            "latest_tonkatsu_reheat": segment,
            "core_check": None, "core_check_summary": None, "core_check_mock_evidence": None, "updated_at": now}
    event = {"sk": f"EVENT#SAFETY#{int(meta['safety_version']):012d}", "domain": "SAFETY_STATUS",
             "trigger": "TONKATSU_REHEATED", "from_status": "FAILED", "to_status": "INCOMPLETE",
             "segment": segment, "actor": session.subject, "occurred_at": now}
    await _commit(store, pk, old, meta, pointer=_blocked_pointer(pointer, "SAFETY_RECHECK_REQUIRED"), event=event)
    return meta


async def _jeyuk_feed_alignment(job: dict) -> bool:
    process = job.get("process_assessment") or {}
    source_table = os.getenv("FOODTECH_DATA_TABLE", "")
    binding = job.get("source_binding") or {}
    second = process.get("second")
    if not (source_table and binding.get("batch_id") and type(second) is int and
            1 <= second <= max(profile.time_bands[-1] for profile in JEYUK_PROFILES.values())):
        return False
    from .jeyuk_source_evidence import feed_safety_alignment
    try:
        assessments, raws = await _read_jeyuk_source_records(source_table, binding["batch_id"], second)
        return feed_safety_alignment(job, assessments, raws)
    except Exception as exc:
        logger.exception("Jeyuk feed safety evidence read failed")
        raise HTTPException(503, "재료 투입과 원천 안전 증거를 조회하지 못했습니다.") from exc


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
    feed_alignment = (await _jeyuk_feed_alignment(old)
                      if manual_status == "PASSED" and auto_status == "PASSED" else False)
    status = ("FAILED" if manual_status == "FAILED" else
              "PASSED" if manual_status == "PASSED" and auto_status == "PASSED" and feed_alignment else
              "INCOMPLETE")
    now = _now()
    version = int(old.get("discharge_check_version", 0)) + 1
    evidence = [sample.model_dump(mode="json") for sample in body.samples]
    meta = {**old, "safety_status": status, "safety_version": int(old["safety_version"]) + 1,
            "discharge_check_version": version, "discharge_check_status": manual_status,
            "discharge_check_kind": body.kind, "discharge_check": evidence,
            "jeyuk_feed_safety_aligned": feed_alignment,
            "discharge_check_by": session.subject, "version": int(old["version"]) + 1,
            "evaluation_blocked": status != "PASSED", "updated_at": now}
    event = {"sk": f"EVENT#SAFETY#{int(meta['safety_version']):012d}",
             "domain": "SAFETY_STATUS", "trigger": "JEYUK_DISCHARGE_CHECK",
             "from_status": old["safety_status"], "to_status": status,
             "manual_status": manual_status, "auto_safety_status": auto_status,
             "feed_safety_aligned": feed_alignment,
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
    if old["menu"] == "JEYUK" and old.get("jeyuk_feed_safety_aligned") is not True:
        raise HTTPException(409, "재료 투입과 원천 안전 표본의 일치 확인이 필요합니다.")
    if old.get("equipment_profile") == "synthetic-fryer-approved-v2" and not old.get("tonkatsu_batch"):
        raise HTTPException(409, "해당 작업의 합성 배치 기록이 없습니다.")
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
    elif (old.get("tonkatsu_batch") and body.actual_mass_kg != Decimal(old["tonkatsu_batch"]["total_mass_kg"])) or \
            (not old.get("tonkatsu_batch") and body.actual_mass_kg > SYNTHETIC_FRYER_BATCH_KG) or \
            body.actual_portions > old["planned_portions"] or body.quality_decision is not None or \
            body.quality_reason_codes or body.quality_note:
        raise HTTPException(422, "돈까스 첫 척도에는 합성 튀김기 적재량만 적용합니다.")
    reference_kg = (JEYUK_PROFILES[old["jeyuk_size_profile"]].recommended_load_kg if old["menu"] == "JEYUK" else
                    RATED_KG if old.get("tonkatsu_batch") else SYNTHETIC_FRYER_BATCH_KG)
    calculated_load = (body.actual_mass_kg / reference_kg * 100).quantize(Decimal("0.01"))
    expected_band = (old["tonkatsu_batch"]["mode"] if old.get("tonkatsu_batch") else
                     "PARTIAL" if calculated_load <= 50 else "REFERENCE" if calculated_load <= 100
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
        "tonkatsu_batch_rule_version": (old.get("tonkatsu_batch") or {}).get("rule_version"),
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
