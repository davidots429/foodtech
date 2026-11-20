"""Synthetic rice and mackerel meal batches with separate linked work records."""

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from foodtech_contracts.combi_cooking import cooking_durations, cooking_time_status, oven_load, rice_water
from foodtech_contracts.combi_transition import TransitionSample, assess_transition
from foodtech_contracts.combi_mackerel_process import Sample, assess as assess_mackerel_process

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


class MackerelProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    rack_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    shelf: int = Field(ge=1, le=10, strict=True)
    pan_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    mass_kg: Decimal = Field(ge=Decimal("0.12"), le=Decimal("0.15"), max_digits=4, decimal_places=3)
    thickness_cm: Decimal = Field(ge=Decimal("1.5"), le=Decimal("2.0"), max_digits=3, decimal_places=1)


class MealEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    event: Literal["RICE_STARTED", "RICE_PROGRAM_COMPLETED", "RICE_UNLOADED", "MODE_TRANSITION_STARTED",
                   "MACKEREL_STARTED", "MACKEREL_PROGRAM_COMPLETED", "MACKEREL_UNLOADED"]
    observed_at: datetime
    products: list[MackerelProduct] | None = Field(default=None, min_length=3, max_length=3)


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


class CoreReading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    second: int = Field(ge=0, le=59, strict=True)
    celsius: Decimal | None = Field(default=None, ge=Decimal("-50"), le=Decimal("150"))


class MackerelProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probe_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    product_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    calibration_status: Literal["OK", "EXPIRED"]
    readings: list[CoreReading] = Field(min_length=1, max_length=60)


class MackerelSafetyCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    measured_at: datetime
    probes: list[MackerelProbe] = Field(min_length=1, max_length=3)


class MackerelProcessSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    second: int = Field(ge=1, le=900, strict=True)
    chamber_celsius: Decimal
    door: Literal["OPEN", "CLOSED"]


class MackerelProcessEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    samples: list[MackerelProcessSample] = Field(min_length=1, max_length=900)


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
        ("MACKEREL_READY", "MACKEREL_STARTED"): ("MACKEREL_COOKING", "MACKEREL"),
        ("MACKEREL_COOKING", "MACKEREL_PROGRAM_COMPLETED"): ("MACKEREL_AWAITING_UNLOAD", "MACKEREL"),
        ("MACKEREL_AWAITING_UNLOAD", "MACKEREL_UNLOADED"): ("MACKEREL_UNLOADED", "MACKEREL"),
    }
    route = routes.get((old["status"], body.event))
    if not route:
        raise HTTPException(409, "현재 단계에서는 이 사건을 기록할 수 없습니다.")
    next_status, target = route
    step_key = (f"JOB#RICE#{old['rice_cooking_job_id']}" if target == "RICE" else
                f"JOB#MACKEREL#{old['mackerel_cooking_job_id']}" if target == "MACKEREL" else
                f"TRANSITION#{old['mode_transition_id']}")
    step = rows.get(step_key)
    if not step:
        raise HTTPException(503, "연결된 작업을 찾을 수 없습니다.")
    at = observed.isoformat().replace("+00:00", "Z")
    updated = {**step}
    if body.event != "MACKEREL_STARTED" and body.products is not None:
        raise HTTPException(422, "고등어 투입 단계에서만 제품을 기록할 수 있습니다.")
    if body.event == "MACKEREL_STARTED":
        transition = rows.get(f"TRANSITION#{old['mode_transition_id']}")
        if not transition or transition.get("status") != "DRY_HEAT_READY" or not transition.get("mackerel_load_allowed"):
            raise HTTPException(409, "건열 전환 준비를 먼저 확인해야 합니다.")
        products = body.products
        if not products or len({item.product_id for item in products}) != 3 or \
                len({item.rack_id for item in products}) != 1 or \
                len({item.shelf for item in products}) != 3 or \
                len({item.pan_id for item in products}) != 3:
            raise HTTPException(422, "초기 합성 시연에는 한 랙의 서로 다른 세 선반에 제품 세 개가 필요합니다.")
        updated.update(status="RUNNING", active_cooking_started_at=at,
                       products=[item.model_dump(mode="json") for item in products])
    elif body.event == "RICE_STARTED":
        updated.update(status="RUNNING", active_cooking_started_at=at)
    elif body.event in {"RICE_PROGRAM_COMPLETED", "MACKEREL_PROGRAM_COMPLETED"}:
        start = datetime.fromisoformat(step["active_cooking_started_at"].replace("Z", "+00:00"))
        seconds = int((observed - start).total_seconds())
        updated.update(status="AWAITING_UNLOAD", program_completed_at=at,
                       program_cooking_duration=seconds,
                       time_status=cooking_time_status("RICE" if target == "RICE" else "MACKEREL", seconds))
    elif body.event in {"RICE_UNLOADED", "MACKEREL_UNLOADED"}:
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


@router.post("/{meal_id}/mackerel-safety")
async def record_mackerel_safety(meal_id: UUID, request: Request, body: MackerelSafetyCheck) -> dict:
    session, store = await _context(request, write=True)
    pk = _pk(session.subject, str(meal_id))
    rows = {row["sk"]: {k: v for k, v in row.items() if k != "pk"}
            for row in await _store_call(store, store.read_meal, pk)}
    old = rows.get("META")
    if not old:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    if int(old["version"]) != body.expected_version or old["status"] != "MACKEREL_UNLOADED":
        raise HTTPException(409, "인양된 작업의 안전 확인 단계가 아닙니다.")
    step = rows.get(f"JOB#MACKEREL#{old['mackerel_cooking_job_id']}")
    if not step or not step.get("food_unloaded_at") or not step.get("products"):
        raise HTTPException(409, "인양 및 제품 정보가 필요합니다.")
    if body.measured_at.tzinfo is None or body.measured_at.utcoffset() is None:
        raise HTTPException(422, "측정 시작시각에 시간대가 필요합니다.")
    start = body.measured_at.astimezone(timezone.utc)
    unloaded = datetime.fromisoformat(step["food_unloaded_at"].replace("Z", "+00:00"))
    if start != unloaded or start + timedelta(seconds=60) > datetime.now(timezone.utc):
        raise HTTPException(422, "측정은 인양 즉시 시작하고 60초가 경과해야 합니다.")
    products = {item["product_id"]: item for item in step["products"]}
    if (len({p.product_id for p in body.probes}) != len(body.probes) or
            len({p.probe_id for p in body.probes}) != len(body.probes) or
            any(p.product_id not in products for p in body.probes)):
        raise HTTPException(422, "서로 다른 적재 제품과 탐침을 선택해야 합니다.")
    low = any(p.calibration_status == "OK" and r.celsius is not None and r.celsius < 75
              for p in body.probes for r in p.readings)
    complete = (len(body.probes) == 3 and {p.product_id for p in body.probes} == set(products) and
                all(p.calibration_status == "OK" and len(p.readings) == 60 and
                    [r.second for r in p.readings] == list(range(60)) and
                    all(r.celsius is not None for r in p.readings) for p in body.probes))
    safety = "FAILED" if low else "PASSED" if complete else "ASSESSMENT_INCOMPLETE"
    minimum = (str(min(r.celsius for p in body.probes for r in p.readings)) if complete else None)
    version = int(old["version"]) + 1
    safety_version = int(step.get("safety_version", 0)) + 1
    updated = {**step, "safety_status": safety, "safety_version": safety_version,
               "batch_representative_core_temperature": minimum}
    next_status = "MACKEREL_SAFETY_PASSED" if safety == "PASSED" else \
                  "MACKEREL_SAFETY_FAILED" if safety == "FAILED" else "MACKEREL_UNLOADED"
    meta = {**old, "status": next_status, "version": version, "updated_at": _now()}
    event = {"sk": f"EVENT#{version:012d}", "event": "MACKEREL_CORE_CHECK", "from_status": old["status"],
             "to_status": next_status, "observed_at": start.isoformat().replace("+00:00", "Z"),
             "recorded_at": _now(), "actor": session.subject, "safety_status": safety,
             "probes": [probe.model_dump(mode="json") for probe in body.probes],
             "products": step["products"]}
    try:
        await _store_call(store, store.advance_meal, pk, body.expected_version, meta, updated, event)
    except Conflict as exc:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.") from exc
    return {"meal": meta, "step": updated, "event": event}


@router.post("/{meal_id}/mackerel-process")
async def record_mackerel_process(meal_id: UUID, request: Request, body: MackerelProcessEvidence) -> dict:
    session, store = await _context(request, write=True)
    pk = _pk(session.subject, str(meal_id))
    rows = {row["sk"]: {key: value for key, value in row.items() if key != "pk"}
            for row in await _store_call(store, store.read_meal, pk)}
    old = rows.get("META")
    if not old:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    if int(old["version"]) != body.expected_version or old["status"] not in {
            "MACKEREL_UNLOADED", "MACKEREL_SAFETY_PASSED", "MACKEREL_SAFETY_FAILED"}:
        raise HTTPException(409, "인양된 고등어 작업의 현재 상태를 확인해 주세요.")
    step = rows.get(f"JOB#MACKEREL#{old['mackerel_cooking_job_id']}")
    if not step or step.get("monitored_process_assessment"):
        raise HTTPException(409, "이미 공정 표본을 기록했거나 고등어 작업이 없습니다.")
    try:
        assessment = assess_mackerel_process([Sample(**item.model_dump()) for item in body.samples],
                                              int(step["program_cooking_duration"]), step["time_status"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    version = int(old["version"]) + 1
    updated = {**step, "monitored_process_assessment": assessment,
               "monitored_process_samples": [item.model_dump(mode="json") for item in body.samples]}
    meta = {**old, "version": version, "updated_at": _now()}
    event = {"sk": f"EVENT#{version:012d}", "event": "MACKEREL_PROCESS_ASSESSED",
             "from_status": old["status"], "to_status": old["status"],
             "observed_at": step["program_completed_at"], "recorded_at": _now(),
             "actor": session.subject, "assessment": assessment}
    try:
        await _store_call(store, store.advance_meal, pk, body.expected_version, meta, updated, event)
    except Conflict as exc:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.") from exc
    return {"meal": meta, "step": updated, "event": event}
