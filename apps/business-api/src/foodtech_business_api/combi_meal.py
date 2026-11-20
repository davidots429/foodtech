"""Synthetic rice and mackerel meal batches with separate linked work records."""

from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from foodtech_contracts.combi_cooking import cooking_durations, cooking_time_status, oven_load, rice_water
from foodtech_contracts.combi_transition import TransitionSample, assess_transition
from foodtech_contracts.combi_mackerel_process import Sample, assess as assess_mackerel_process
from foodtech_contracts.combi_rice_process import Sample as RiceSample, assess as assess_rice_process
from foodtech_contracts.combi_stream import CombiStreamValidator
from foodtech_contracts.combi_sampling import selection_issues

from .cooking import SCORE_ITEMS, _context, _now, _store_call
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
    position: Literal["LEFT", "CENTER", "RIGHT"] | None = None
    mass_kg: Decimal = Field(ge=Decimal("0.12"), le=Decimal("0.15"), max_digits=4, decimal_places=3)
    thickness_cm: Decimal = Field(ge=Decimal("1.5"), le=Decimal("2.0"), max_digits=3, decimal_places=1)


class MealEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    event: Literal["RICE_STARTED", "RICE_PROGRAM_COMPLETED", "RICE_UNLOADED", "MODE_TRANSITION_STARTED",
                   "MACKEREL_STARTED", "MACKEREL_PROGRAM_COMPLETED", "MACKEREL_UNLOADED"]
    observed_at: datetime
    products: list[MackerelProduct] | None = Field(default=None, min_length=1, max_length=200)


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
    selection_note: str | None = Field(default=None, max_length=500)


class MackerelProcessSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    second: int = Field(ge=1, le=900, strict=True)
    chamber_celsius: Decimal
    door: Literal["OPEN", "CLOSED"]
    fan_level: int | None = Field(default=None, strict=True, ge=0, le=10)


class MackerelProcessEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    samples: list[MackerelProcessSample] = Field(min_length=1, max_length=900)


class RiceProcessSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    second: int = Field(ge=1, le=2000, strict=True)
    chamber_celsius: Decimal = Field(ge=Decimal("-50"), le=Decimal("350"), max_digits=6, decimal_places=2)
    humidity_percent: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    door: Literal["OPEN", "CLOSED"] | None = None
    fan_level: int | None = Field(default=None, strict=True, ge=0, le=10)


class RiceProcessEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    humidity_source: Literal["MEASURED", "CONTROLLER_ESTIMATE", "NOT_SUPPORTED"]
    samples: list[RiceProcessSample] = Field(min_length=1, max_length=2000)


class CombiSourceImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    device_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    records: list[str] = Field(min_length=3, max_length=5000)


class CombiQualityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    expected_current_version: int = Field(ge=0)
    kind: Literal["INITIAL", "REVISION", "REASSESSMENT"]
    reason: str | None = Field(default=None, max_length=500)
    scores: dict[str, StrictInt]
    observations: dict[str, str]
    quality_decision: Literal["QUALITY_PASSED", "QUALITY_FAILED"]
    quality_reason_codes: list[str] = Field(default_factory=list, max_length=8)
    quality_note: str | None = Field(default=None, max_length=500)
    overall_satisfaction: StrictInt | None = Field(default=None, ge=1, le=5)
    planned_portions: int = Field(gt=0, le=1000, strict=True)
    actual_portions: int = Field(gt=0, le=1000, strict=True)


def _pk(subject: str, meal_id: str) -> str:
    return f"SCOPE#USER#{subject}#COMBI_MEAL#{meal_id}"


@router.post("/{meal_id}/source-observations", status_code=201)
async def import_combi_source(meal_id: UUID, request: Request, body: CombiSourceImport) -> dict:
    session, store = await _context(request, write=True)
    pk = _pk(session.subject, str(meal_id))
    rows = {row["sk"]: row for row in await _store_call(store, store.read_meal, pk)}
    old = rows.get("META")
    if not old:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    if int(old["version"]) != body.expected_version or old["status"] != "PREPARING_RICE" or old.get("source"):
        raise HTTPException(409, "원천 자료는 준비 중인 배치에 한 번만 연결할 수 있습니다.")
    encoded = [line.encode("utf-8") for line in body.records]
    if any(len(line) > 2048 for line in encoded) or sum(len(line) + 1 for line in encoded) > 2_500_000:
        raise HTTPException(413, "합성 원천 자료의 크기 제한을 초과했습니다.")
    jobs = {"RICE": old["rice_cooking_job_id"], "TRANSITION": old["mode_transition_id"],
            "MACKEREL": old["mackerel_cooking_job_id"]}
    validator = CombiStreamValidator(str(meal_id), body.device_id, jobs)
    counts = {key: 0 for key in jobs}
    first_observed_at = None
    try:
        for line in body.records:
            item = validator.accept(line)
            if first_observed_at is None:
                first_observed_at = item["occurredAt"]
            counts[item["phase"]] += 1
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, "원천 표본이 배치 계약과 일치하지 않습니다.") from exc
    if any(count == 0 for count in counts.values()):
        raise HTTPException(422, "밥·모드 전환·고등어 표본이 모두 필요합니다.")
    digest = hashlib.sha256(b"\n".join(encoded) + b"\n").hexdigest()
    chunks = [{"sk": f"RAW#{offset // 100:04d}", "first_sequence": offset + 1,
               "records": body.records[offset:offset + 100]} for offset in range(0, len(body.records), 100)]
    version = int(old["version"]) + 1
    now = _now()
    meta = {key: value for key, value in old.items() if key != "pk"}
    meta.update(version=version, updated_at=now,
                source={"device_id": body.device_id, "profile_id": "mockprofile-120", "provenance": "SYNTHETIC",
                        "record_count": len(body.records), "phase_counts": counts,
                        "first_observed_at": first_observed_at,
                        "raw_sha256": digest, "imported_at": now})
    event = {"sk": f"EVENT#{version:012d}", "event": "COMBI_SOURCE_IMPORTED",
             "from_status": old["status"], "to_status": old["status"],
             "observed_at": now, "recorded_at": now, "actor": session.subject,
             "source_sha256": digest, "record_count": len(body.records)}
    try:
        await _store_call(store, store.attach_meal_source, pk, body.expected_version, meta, event, chunks)
    except Conflict as exc:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.") from exc
    return {"meal": meta, "event": event}


@router.get("/{meal_id}/source-observations")
async def get_combi_source(meal_id: UUID, request: Request, cursor: int = Query(0, ge=0, le=49)) -> dict:
    session, store = await _context(request)
    pk = _pk(session.subject, str(meal_id))
    meta, _ = await _store_call(store, store.read, pk)
    if not meta or not meta.get("source"):
        raise HTTPException(404, "연결된 원천 자료가 없습니다.")
    count = int(meta["source"]["record_count"])
    if cursor * 100 >= count:
        raise HTTPException(404, "원천 자료 페이지가 없습니다.")
    row = await _store_call(store, store.read_meal_source_chunk, pk, cursor)
    if not row:
        raise HTTPException(503, "연결된 원천 자료를 읽을 수 없습니다.")
    return {"records": row["records"], "first_sequence": int(row["first_sequence"]),
            "next_cursor": cursor + 1 if (cursor + 1) * 100 < count else None,
            "raw_sha256": meta["source"]["raw_sha256"]}


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
        if not products or len({item.product_id for item in products}) != len(products):
            raise HTTPException(422, "서로 다른 고등어 제품이 필요합니다.")
        racks = {item.rack_id for item in products}
        pan_locations = {item.pan_id: (item.rack_id, item.shelf) for item in products}
        shelf_pans = {(item.rack_id, item.shelf): item.pan_id for item in products}
        if (len(racks) > (2 if old["equipment_size"] == "LARGE_ROLL_IN_RACK_20" else 1) or
                any(pan_locations[p.pan_id] != (p.rack_id, p.shelf) or
                    shelf_pans[(p.rack_id, p.shelf)] != p.pan_id for p in products) or
                any(sum(p.pan_id == pan for p in products) > 10 for pan in pan_locations)):
            raise HTTPException(422, "장비의 랙·선반·팬 위치 또는 팬당 제품 수를 확인해 주세요.")
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
    issues = selection_issues(step["products"], {p.product_id for p in body.probes}, body.selection_note)
    complete = (not issues and
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
             "products": step["products"], "selection_issues": issues, "selection_note": body.selection_note}
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


@router.post("/{meal_id}/rice-process")
async def record_rice_process(meal_id: UUID, request: Request, body: RiceProcessEvidence) -> dict:
    session, store = await _context(request, write=True)
    pk = _pk(session.subject, str(meal_id))
    rows = {row["sk"]: {key: value for key, value in row.items() if key != "pk"}
            for row in await _store_call(store, store.read_meal, pk)}
    old = rows.get("META")
    if not old:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    if int(old["version"]) != body.expected_version:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.")
    step = rows.get(f"JOB#RICE#{old['rice_cooking_job_id']}")
    if not step or step.get("status") != "UNLOADED" or step.get("monitored_process_assessment"):
        raise HTTPException(409, "인양된 밥 작업의 아직 기록되지 않은 공정 표본이 필요합니다.")
    try:
        assessment = assess_rice_process([RiceSample(**item.model_dump()) for item in body.samples],
                                         int(step["program_cooking_duration"]), step["time_status"],
                                         body.humidity_source)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    version = int(old["version"]) + 1
    updated = {**step, "monitored_process_assessment": assessment,
               "monitored_process_samples": [item.model_dump(mode="json") for item in body.samples]}
    meta = {**old, "version": version, "updated_at": _now()}
    event = {"sk": f"EVENT#{version:012d}", "event": "RICE_PROCESS_ASSESSED",
             "from_status": old["status"], "to_status": old["status"],
             "observed_at": step["program_completed_at"], "recorded_at": _now(),
             "actor": session.subject, "assessment": assessment}
    try:
        await _store_call(store, store.advance_meal, pk, body.expected_version, meta, updated, event)
    except Conflict as exc:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.") from exc
    return {"meal": meta, "step": updated, "event": event}


class SourceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


@router.post("/{meal_id}/source-assessment/{phase}")
async def assess_bound_source(meal_id: UUID, phase: Literal["rice", "transition", "mackerel"],
                              request: Request, body: SourceAssessment) -> dict:
    session, store = await _context(request, write=True)
    pk = _pk(session.subject, str(meal_id))
    rows = {row["sk"]: {key: value for key, value in row.items() if key != "pk"}
            for row in await _store_call(store, store.read_meal, pk)}
    old = rows.get("META")
    if not old:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    if int(old["version"]) != body.expected_version:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.")
    source = old.get("source")
    if not source:
        raise HTTPException(409, "연결된 합성 원표본이 필요합니다.")
    kind = {"rice": "RICE", "transition": "TRANSITION", "mackerel": "MACKEREL"}[phase]
    job_id = {"rice": old["rice_cooking_job_id"], "transition": old["mode_transition_id"],
                        "mackerel": old["mackerel_cooking_job_id"]}[phase]
    step = rows.get(f"TRANSITION#{job_id}" if phase == "transition" else f"JOB#{kind}#{job_id}")
    if phase == "transition":
        if not step or step.get("status") != "IN_PROGRESS" or step.get("samples") or old["status"] != "MODE_TRANSITION":
            raise HTTPException(409, "표본이 없는 진행 중인 전환에서만 원천 판정을 적용할 수 있습니다.")
    elif not step or step.get("status") != "UNLOADED" or step.get("monitored_process_assessment"):
        raise HTTPException(409, "인양 후 아직 공정 판정이 없는 작업에만 적용할 수 있습니다.")
    jobs = {"RICE": old["rice_cooking_job_id"], "TRANSITION": old["mode_transition_id"],
            "MACKEREL": old["mackerel_cooking_job_id"]}
    validator = CombiStreamValidator(str(meal_id), source["device_id"], jobs)
    records, selected = [], []
    total = int(source["record_count"])
    try:
        for index in range((total + 99) // 100):
            chunk = await _store_call(store, store.read_meal_source_chunk, pk, index)
            if not chunk or int(chunk["first_sequence"]) != index * 100 + 1:
                raise ValueError("source chunk missing or out of order")
            for line in chunk["records"]:
                item = validator.accept(line)
                records.append(line)
                if item["phase"] == kind:
                    selected.append(item)
        digest = hashlib.sha256(b"\n".join(line.encode("utf-8") for line in records) + b"\n").hexdigest()
        if len(records) != total or digest != source["raw_sha256"] or not selected:
            raise ValueError("source integrity check failed")
        started = datetime.fromisoformat(step["started_at" if phase == "transition" else
                                           "active_cooking_started_at"].replace("Z", "+00:00"))
        first = datetime.fromisoformat(selected[0]["occurredAt"].replace("Z", "+00:00"))
        if started != first or (phase != "transition" and int(step["program_cooking_duration"]) != len(selected)):
            raise ValueError("source time span differs from the recorded cooking interval")
        if phase == "transition" and started + timedelta(seconds=len(selected)) > datetime.now(timezone.utc):
            raise ValueError("transition samples have not yet elapsed")
        if phase == "rice":
            sources = {item["humidityValueSource"] for item in selected}
            if len(sources) != 1:
                raise ValueError("humidity source changes during the cooking interval")
            assessment = assess_rice_process([
                RiceSample(item["phaseSecond"], Decimal(item["chamberCelsius"]),
                           Decimal(item["measuredHumidityPercent"]) if item["measuredHumidityPercent"] is not None else None,
                           item["doorState"], item["fanLevel"])
                for item in selected], len(selected), step["time_status"], sources.pop())
        elif phase == "mackerel":
            assessment = assess_mackerel_process([
                Sample(item["phaseSecond"], Decimal(item["chamberCelsius"]), item["doorState"], item["fanLevel"])
                for item in selected], len(selected), step["time_status"])
        else:
            if any(item["schemaVersion"] != "synthetic-combi-v2" for item in selected):
                raise ValueError("alarm and vent observations are required")
            humidity_sources = {item["humidityValueSource"] for item in selected}
            vent_modes = {item["ventSupported"] for item in selected}
            if len(humidity_sources) != 1 or len(vent_modes) != 1:
                raise ValueError("sensor capabilities changed during transition")
            humidity_source = humidity_sources.pop()
            if humidity_source not in {"MEASURED", "NOT_SUPPORTED"}:
                raise ValueError("transition humidity requires actual measurement or an unsupported flag")
            humidity_supported, vent_supported = humidity_source == "MEASURED", vent_modes.pop()
            transition_samples = [{"second": item["phaseSecond"],
                                   "chamber_celsius": str(item["chamberCelsius"]),
                                   "climate_setpoint_percent": str(item["climateSetpointPercent"]),
                                   "door_closed": item["doorState"] == "CLOSED",
                                   "critical_alarm": item["criticalAlarm"],
                                   "humidity_percent": str(item["measuredHumidityPercent"])
                                   if item["measuredHumidityPercent"] is not None else None,
                                   "vent_event": item["ventEvent"]} for item in selected]
            assessment = assess_transition([
                TransitionSample(second=item["second"], chamber_celsius=Decimal(item["chamber_celsius"]),
                                 climate_setpoint_percent=Decimal(item["climate_setpoint_percent"]),
                                 door_closed=item["door_closed"], critical_alarm=item["critical_alarm"],
                                 humidity_percent=Decimal(item["humidity_percent"])
                                 if item["humidity_percent"] is not None else None,
                                 vent_event=item["vent_event"]) for item in transition_samples],
                humidity_supported=humidity_supported, vent_supported=vent_supported)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, "원표본의 무결성이나 조리 시간 범위를 확인할 수 없습니다.") from exc
    version = int(old["version"]) + 1
    lineage = {"raw_sha256": digest, "phase": kind, "first_sequence": selected[0]["sequence"],
               "last_sequence": selected[-1]["sequence"]}
    next_status = old["status"]
    observed_at = step.get("program_completed_at")
    if phase == "transition":
        observed_at = (started + timedelta(seconds=len(selected))).isoformat().replace("+00:00", "Z")
        next_status = {"DRY_HEAT_READY": "MACKEREL_READY", "MODE_TRANSITION_TIMEOUT": "TRANSITION_TIMEOUT"}.get(
            assessment["status"], "MODE_TRANSITION")
        updated = {**step, **assessment, "humidity_supported": humidity_supported, "vent_supported": vent_supported,
                   "samples": transition_samples, "observed_through": observed_at, "source": lineage}
    else:
        updated = {**step, "monitored_process_assessment": assessment, "monitored_process_source": lineage}
    meta = {**old, "status": next_status, "version": version, "updated_at": _now()}
    if phase == "transition":
        meta["last_observed_at"] = observed_at
    event = {"sk": f"EVENT#{version:012d}", "event": f"{kind}_SOURCE_ASSESSED",
             "from_status": old["status"], "to_status": next_status,
             "observed_at": observed_at, "recorded_at": _now(),
             "actor": session.subject, "assessment": assessment, "source_sha256": digest}
    try:
        await _store_call(store, store.advance_meal, pk, body.expected_version, meta, updated, event)
    except Conflict as exc:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.") from exc
    return {"meal": meta, "step": updated, "event": event}


@router.post("/{meal_id}/quality/{menu}", status_code=201)
async def assess_combi_quality(meal_id: UUID, menu: Literal["rice", "mackerel"],
                               request: Request, body: CombiQualityAssessment) -> dict:
    session, store = await _context(request, write=True)
    pk = _pk(session.subject, str(meal_id))
    rows = {row["sk"]: {key: value for key, value in row.items() if key != "pk"}
            for row in await _store_call(store, store.read_meal, pk)}
    old = rows.get("META")
    if not old:
        raise HTTPException(404, "식사 배치를 찾을 수 없습니다.")
    if int(old["version"]) != body.expected_version:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.")
    kind = "RICE" if menu == "rice" else "MACKEREL"
    job_id = old["rice_cooking_job_id"] if menu == "rice" else old["mackerel_cooking_job_id"]
    step = rows.get(f"JOB#{kind}#{job_id}")
    if not step or step.get("status") != "UNLOADED" or step.get("safety_status") != "PASSED":
        raise HTTPException(409, "인양과 유효한 안전 확인 후에만 품질평가를 완료할 수 있습니다.")
    current = int(step.get("quality_version", 0))
    if current != body.expected_current_version:
        raise HTTPException(409, "현재 품질평가가 변경되었습니다. 다시 조회해 주세요.")
    if ((current == 0) != (body.kind == "INITIAL") or
            (current > 0 and (body.reason is None or not body.reason.strip()))):
        raise HTTPException(422, "최초 평가와 수정 유형 또는 사유를 확인해 주세요.")
    if (set(body.scores) != set(SCORE_ITEMS) or any(not 1 <= value <= 5 for value in body.scores.values()) or
            set(body.observations) != set(SCORE_ITEMS) or
            any(not value.strip() or len(value) > 500 for value in body.observations.values())):
        raise HTTPException(422, "7개 항목에 각각 1~5점과 관찰 근거가 필요합니다.")
    common_reasons = {"UNDERCOOKED", "EXCESS_MOISTURE", "OVERCOOKED", "UNEVEN_COOK", "BAD_SMELL"}
    menu_reasons = {"BURNT_BOTTOM", "RICE_CLUMPED"} if menu == "rice" else \
                   {"BURNT_SURFACE", "DRY_TEXTURE", "SKIN_NOT_CRISP"}
    reasons = body.quality_reason_codes
    if (len(reasons) != len(set(reasons)) or set(reasons) - (common_reasons | menu_reasons) or
            ((body.quality_decision == "QUALITY_FAILED") != bool(reasons))):
        raise HTTPException(422, "메뉴에 맞는 품질 실패 사유를 확인해 주세요.")
    if body.actual_portions > body.planned_portions or \
            (menu == "mackerel" and body.actual_portions > len(step.get("products", []))):
        raise HTTPException(422, "실제 인분이 계획 또는 합성 제품 수를 초과합니다.")
    total = sum(body.scores.values())
    score = (Decimal(total) / Decimal(35) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    score_passed = min(body.scores.values()) >= 3 and score >= 60
    if body.quality_decision == "QUALITY_PASSED" and not score_passed:
        raise HTTPException(422, "품질 합격 점수 기준에 미달했습니다.")
    passed = score_passed and body.quality_decision == "QUALITY_PASSED"
    event_version = int(old["version"]) + 1
    quality_version = current + 1
    event_sk = f"EVENT#{event_version:012d}"
    now = _now()
    assessment = {"version": quality_version, "previous_version": current or None,
                  "kind": body.kind, "reason": body.reason, "status": "COMPLETED",
                  "menu": kind, "scale_version": f"{menu}-common-v1", "formula_version": "quality-v1",
                  "scores": body.scores, "observations": body.observations, "score_total": total,
                  "score_percent": str(score), "quality_passed": passed,
                  "quality_target": passed and min(body.scores.values()) >= 4 and score >= 80,
                  "quality_status": "QUALITY_PASSED" if passed else "QUALITY_FAILED",
                  "quality_reason_codes": reasons, "quality_note": body.quality_note,
                  "overall_satisfaction": body.overall_satisfaction,
                  "planned_portions": body.planned_portions, "actual_portions": body.actual_portions,
                  "safety_status": step["safety_status"], "safety_version": int(step.get("safety_version", 0)),
                  "equipment_size": old["equipment_size"], "assessed_by": session.subject, "completed_at": now}
    if menu == "mackerel":
        mass = sum(Decimal(product["mass_kg"]) for product in step["products"])
        assessment.update(actual_mass_kg=str(mass), mass_per_portion_kg=str(
            (mass / body.actual_portions).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)))
    else:
        assessment.update(actual_mass_kg=step["ingredient_kg"], rack_count=step["rack_count"],
                          pan_count=step["pan_count"], load_ratio=step["load_ratio"])
    pointer = {"version": quality_version, "event_sk": event_sk, "quality_status": assessment["quality_status"],
               "score_percent": str(score), "quality_target": assessment["quality_target"],
               "safety_version": assessment["safety_version"]}
    updated = {**step, "quality_status": assessment["quality_status"], "quality_version": quality_version,
               "quality_current": pointer}
    meta = {**old, "version": event_version, "updated_at": now}
    event = {"sk": event_sk, "event": f"{kind}_QUALITY_ASSESSED", "from_status": old["status"],
             "to_status": old["status"], "observed_at": now, "recorded_at": now,
             "actor": session.subject, "assessment": assessment}
    try:
        await _store_call(store, store.advance_meal, pk, body.expected_version, meta, updated, event)
    except Conflict as exc:
        raise HTTPException(409, "배치 상태가 변경되었습니다. 다시 조회해 주세요.") from exc
    return {"meal": meta, "step": updated, "assessment": assessment}
