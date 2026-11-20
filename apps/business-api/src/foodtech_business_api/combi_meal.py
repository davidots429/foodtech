"""Synthetic rice and mackerel meal batches with separate linked work records."""

from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from foodtech_contracts.combi_cooking import oven_load, rice_water

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
    return {"meal": meta, "rice": rows[keys[0]], "transition": rows[keys[1]], "mackerel": rows[keys[2]]}
