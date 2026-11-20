"""Synthetic rice and mackerel meal batches with separate linked work records."""

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from foodtech_contracts.combi_cooking import cooking_durations, cooking_time_status, oven_load, rice_water
from foodtech_contracts.combi_transition import TransitionSample, assess_transition

from .cooking import _context, _now, _store_call
from .cooking_store import Conflict

router = APIRouter(prefix="/api/customer/combi-meals", tags=["combi-meals"])


class CreateMeal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    equipment_size: Literal["MEDIUM_FIXED_RACK_10", "LARGE_ROLL_IN_RACK_20"]
    rack_count: int = Field(ge=1, le=2)
    pan_count: int = Field(ge=1, le=20)
    dry_rice_kg: Decimal = Field(gt=0, max_digits=7, decimal_places=2)
    drained_rice_kg: Decimal = Field(gt=0, max_digits=7, decimal_places=2)
    added_water_kg: Decimal = Field(ge=0, max_digits=7, decimal_places=2)
    pan_tare_kg: Decimal = Field(ge=0, max_digits=7, decimal_places=2)
    rack_tare_kg: Decimal = Field(ge=0, max_digits=7, decimal_places=2)


class MealEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    event: Literal["RICE_STARTED", "RICE_PROGRAM_COMPLETED", "RICE_UNLOADED", "MODE_TRANSITION_STARTED"]
    observed_at: datetime


class TransitionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    second: int = Field(ge=1, le=300, strict=True)
    chamber_celsius: Decimal
    climate_setpoint_percent: Decimal
    door_closed: StrictBool
    critical_alarm: StrictBool
    humidity_percent: Decimal | None = None
    vent_event: Literal["OPENED", "CLOSED"] | None = None


class TransitionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    humidity_supported: StrictBool
    vent_supported: StrictBool
    samples: list[TransitionObservation] = Field(min_length=1, max_length=300)


def _pk(subject: str, meal_id: str) -> str:
    return f"SCOPE#USER#{subject}#COMBI_MEAL#{meal_id}"


@router.post("", status_code=201)
async def create_meal(request: Request, body: CreateMeal) -> dict:
    session, store = await _context(request, write=True)
    try:
        water = rice_water(body.dry_rice_kg, body.drained_rice_kg, body.added_water_kg)
        load = oven_load(body.equipment_size, body.rack_count, body.pan_count,
                         body.drained_rice_kg + body.added_water_kg,
                         body.pan_tare_kg, body.rack_tare_kg)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if load.violations:
        raise HTTPException(422, {"code": "PROFILE_REJECTED", "violations": load.violations})
    meal_id, rice_id, transition_id, fish_id = (str(uuid4()) for _ in range(4))
    now = _now()
    records = [
        {"sk": "META", "meal_batch_id": meal_id, "rice_cooking_job_id": rice_id,
         "mode_transition_id": transition_id, "mackerel_cooking_job_id": fish_id,
         "provenance": "SYNTHETIC", "equipment_size": body.equipment_size,
         "status": "PREPARING_RICE", "version": 1, "created_at": now},
        {"sk": f"JOB#RICE#{rice_id}", "job_id": rice_id, "meal_batch_id": meal_id,
         "menu": "RICE", "status": "PENDING", "program_mode": "STEAM",
         "program_celsius": "100", "climate_setpoint_percent": "100", "fan_level": 4,
         "target_program_seconds": 1800, "process_status": "NOT_MEASURED",
         "safety_status": "NOT_MEASURED", "quality_status": "PENDING",
         "dry_rice_kg": str(body.dry_rice_kg), "drained_rice_kg": str(body.drained_rice_kg),
         "added_water_kg": str(body.added_water_kg),
         "absorbed_and_residual_water_kg": str(water.absorbed_and_residual_water_kg),
         "effective_water_kg": str(water.actual_effective_water_kg),
         "effective_water_ratio": str(water.effective_water_ratio), "water_ratio_status": water.status,
         "target_added_water_kg": str(water.target_added_water_kg) if water.target_added_water_kg is not None else None,
         "rack_count": body.rack_count, "pan_count": body.pan_count,
         "ingredient_kg": str(load.ingredient_kg), "pan_tare_kg": str(load.pan_tare_kg),
         "rack_tare_kg": str(load.rack_tare_kg), "payload_kg": str(load.payload_kg),
         "gross_kg": str(load.gross_kg), "load_basis": load.load_basis,
         "load_ratio": str(load.load_ratio)},
        {"sk": f"TRANSITION#{transition_id}", "transition_id": transition_id,
         "meal_batch_id": meal_id, "status": "PENDING", "mackerel_load_allowed": False},
        {"sk": f"JOB#MACKEREL#{fish_id}", "job_id": fish_id, "meal_batch_id": meal_id,
         "menu": "MACKEREL", "status": "PENDING", "program_mode": "DRY_HEAT",
         "program_celsius": "220", "climate_setpoint_percent": "10", "fan_level": 3,
         "target_program_seconds": 720, "process_status": "NOT_MEASURED",
         "safety_status": "NOT_MEASURED", "quality_status": "PENDING"},
    ]
    try:
        await _store_call(store, store.create_meal, _pk(session.subject, meal_id), records)
    except Conflict as exc:
        raise HTTPException(409, "식사 배치 생성이 충돌했습니다. 다시 시도해 주세요.") from exc
    return {"meal": records[0], "rice": records[1], "transition": records[2], "mackerel": records[3]}


@router.get("/{meal_id}")
async def get_meal(meal_id: UUID, request: Request) -> dict:
    session, store = await _context(request)
    records = await _store_call(store, store.read_meal, _pk(session.subject, str(meal_id)))
    rows = {row["sk"]: {k: v for k, v in row.items() if k != "pk"} for row in records}
    meta = rows.get("META")
    if not meta:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    keys = (f"JOB#RICE#{meta['rice_cooking_job_id']}", f"TRANSITION#{meta['mode_transition_id']}",
            f"JOB#MACKEREL#{meta['mackerel_cooking_job_id']}")
    if any(key not in rows for key in keys):
        raise HTTPException(503, "식사 배치의 연결된 작업을 불러올 수 없습니다.")
    events = [rows[key] for key in sorted(rows) if key.startswith("EVENT#")]
    result = {"meal": meta, "rice": rows[keys[0]], "transition": rows[keys[1]], "mackerel": rows[keys[2]]}
    if events:
        result["events"] = events
    return result


@router.post("/{meal_id}/events")
async def advance_meal(meal_id: UUID, request: Request, body: MealEvent) -> dict:
    session, store = await _context(request, write=True)
    pk = _pk(session.subject, str(meal_id))
    rows = {row["sk"]: {k: v for k, v in row.items() if k != "pk"}
            for row in await _store_call(store, store.read_meal, pk)}
    old = rows.get("META")
    if not old:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    if int(old["version"]) != body.expected_version:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.")
    if body.observed_at.tzinfo is None or body.observed_at.utcoffset() is None:
        raise HTTPException(422, "관찰시각에 시간대가 필요합니다.")
    observed = body.observed_at.astimezone(timezone.utc)
    if observed > datetime.now(timezone.utc):
        raise HTTPException(422, "관찰시각은 현재보다 미래일 수 없습니다.")
    previous = old.get("last_observed_at")
    if previous and observed < datetime.fromisoformat(previous.replace("Z", "+00:00")):
        raise HTTPException(422, "관찰시각은 이전 단계 이후여야 합니다.")
    routes = {
        ("PREPARING_RICE", "RICE_STARTED"): ("RICE_COOKING", "RICE"),
        ("RICE_COOKING", "RICE_PROGRAM_COMPLETED"): ("RICE_AWAITING_UNLOAD", "RICE"),
        ("RICE_AWAITING_UNLOAD", "RICE_UNLOADED"): ("RICE_UNLOADED", "RICE"),
        ("RICE_UNLOADED", "MODE_TRANSITION_STARTED"): ("MODE_TRANSITION", "TRANSITION"),
    }
    route = routes.get((old["status"], body.event))
    if not route:
        raise HTTPException(409, "현재 단계에서는 이 사건을 기록할 수 없습니다.")
    next_status, target = route
    step_key = (f"JOB#RICE#{old['rice_cooking_job_id']}" if target == "RICE" else
                f"TRANSITION#{old['mode_transition_id']}")
    step = rows.get(step_key)
    if not step:
        raise HTTPException(503, "연결된 작업을 찾을 수 없습니다.")
    at = observed.isoformat().replace("+00:00", "Z")
    updated = {**step}
    if body.event == "RICE_STARTED":
        updated.update(status="RUNNING", active_cooking_started_at=at)
    elif body.event == "RICE_PROGRAM_COMPLETED":
        start = datetime.fromisoformat(step["active_cooking_started_at"].replace("Z", "+00:00"))
        seconds = int((observed - start).total_seconds())
        updated.update(status="AWAITING_UNLOAD", program_completed_at=at,
                       program_cooking_duration=seconds, time_status=cooking_time_status("RICE", seconds))
    elif body.event == "RICE_UNLOADED":
        start = datetime.fromisoformat(step["active_cooking_started_at"].replace("Z", "+00:00"))
        completed = datetime.fromisoformat(step["program_completed_at"].replace("Z", "+00:00"))
        _, exposure = cooking_durations(start, completed, observed)
        updated.update(status="UNLOADED", food_unloaded_at=at, thermal_exposure_duration=exposure)
    else:
        updated.update(status="IN_PROGRESS", started_at=at, mackerel_load_allowed=False)
    version = int(old["version"]) + 1
    meta = {**old, "status": next_status, "version": version, "last_observed_at": at, "updated_at": _now()}
    event = {"sk": f"EVENT#{version:012d}", "event": body.event, "from_status": old["status"],
             "to_status": next_status, "observed_at": at, "recorded_at": _now(), "actor": session.subject}
    try:
        await _store_call(store, store.advance_meal, pk, body.expected_version, meta, updated, event)
    except Conflict as exc:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.") from exc
    return {"meal": meta, "step": updated, "event": event}


@router.post("/{meal_id}/transition-evidence")
async def record_transition_evidence(meal_id: UUID, request: Request, body: TransitionEvidence) -> dict:
    session, store = await _context(request, write=True)
    pk = _pk(session.subject, str(meal_id))
    rows = {row["sk"]: {k: v for k, v in row.items() if k != "pk"}
            for row in await _store_call(store, store.read_meal, pk)}
    old = rows.get("META")
    if not old:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    if int(old["version"]) != body.expected_version or old["status"] != "MODE_TRANSITION":
        raise HTTPException(409, "배치 상태가 변경되었거나 전환을 진행할 수 없습니다.")
    step = rows.get(f"TRANSITION#{old['mode_transition_id']}")
    if not step or step.get("status") != "IN_PROGRESS":
        raise HTTPException(409, "진행 중인 전환이 없습니다.")
    samples = [item.model_dump(mode="json") for item in body.samples]
    previous = step.get("samples", [])
    if (samples[:len(previous)] != previous or len(samples) <= len(previous) or
            (previous and (step["humidity_supported"] != body.humidity_supported or
                           step["vent_supported"] != body.vent_supported))):
        raise HTTPException(409, "이미 기록한 표본이나 센서 지원 여부는 변경할 수 없습니다.")
    started = datetime.fromisoformat(step["started_at"].replace("Z", "+00:00"))
    observed = started + timedelta(seconds=len(samples))
    if observed > datetime.now(timezone.utc):
        raise HTTPException(422, "마지막 표본 시각은 현재보다 미래일 수 없습니다.")
    try:
        result = assess_transition([TransitionSample(**item.model_dump()) for item in body.samples],
                                   humidity_supported=body.humidity_supported,
                                   vent_supported=body.vent_supported)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    status = {"DRY_HEAT_READY": "MACKEREL_READY", "MODE_TRANSITION_TIMEOUT": "TRANSITION_TIMEOUT"}.get(
        result["status"], "MODE_TRANSITION")
    version = int(old["version"]) + 1
    observed_at = observed.isoformat().replace("+00:00", "Z")
    updated = {**step, **result, "humidity_supported": body.humidity_supported,
               "vent_supported": body.vent_supported, "samples": samples, "observed_through": observed_at}
    meta = {**old, "status": status, "version": version, "last_observed_at": observed_at, "updated_at": _now()}
    event = {"sk": f"EVENT#{version:012d}", "event": "MODE_TRANSITION_ASSESSED",
             "from_status": old["status"], "to_status": status, "observed_at": observed_at,
             "recorded_at": _now(), "actor": session.subject, "assessment": result}
    try:
        await _store_call(store, store.advance_meal, pk, body.expected_version, meta, updated, event)
    except Conflict as exc:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.") from exc
    return {"meal": meta, "step": updated, "event": event}
