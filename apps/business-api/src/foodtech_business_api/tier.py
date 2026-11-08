"""Demo tier application and review endpoints."""

import asyncio
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .auth import DEMO_NOTICE, _csrf, _session
from .tier_store import TierConflict, get_tier_store

router = APIRouter(prefix="/api", tags=["demo-tier"])


class Application(BaseModel):
    business_name: str
    business_registration_number: str
    notice_accepted: bool


class Review(BaseModel):
    decision: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@router.get("/customer/tier-requests/current")
async def current(request: Request, response: Response) -> dict:
    session = await _session(request, "customer")
    response.headers["Cache-Control"] = "no-store"
    return {"application": await asyncio.to_thread(get_tier_store().current, session.subject),
            "price": "00원", "notice": DEMO_NOTICE}


@router.post("/customer/tier-requests", status_code=201)
async def submit(request: Request, body: Application, response: Response) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    if not body.notice_accepted:
        raise HTTPException(400, "시연 기능 안내를 확인해야 합니다.")
    business_name = body.business_name.strip()
    if not 2 <= len(business_name) <= 80:
        raise HTTPException(400, "사업자명을 2~80자로 입력해야 합니다.")
    if not re.fullmatch(r"\d{3}-?\d{2}-?\d{5}", body.business_registration_number):
        raise HTTPException(400, "사업자등록번호는 숫자 10자리 형식이어야 합니다.")
    item = {"subject": session.subject, "application_id": str(uuid4()),
            "business_name": business_name,
            "business_number_last4": body.business_registration_number[-4:],
            "status": "REQUESTED", "tier": "FREE", "version": 1,
            "submitted_at": _now()}
    try:
        await asyncio.to_thread(get_tier_store().submit, session.subject, item)
    except TierConflict as exc:
        raise HTTPException(409, "이미 처리 중이거나 승인된 신청이 있습니다.") from exc
    response.headers["Cache-Control"] = "no-store"
    return {"application": item, "price": "00원", "notice": DEMO_NOTICE}


@router.get("/admin/tier-requests/pending")
async def pending(request: Request, response: Response) -> dict:
    await _session(request, "admin")
    response.headers["Cache-Control"] = "no-store"
    return {"applications": await asyncio.to_thread(get_tier_store().pending, 50),
            "price": "00원", "notice": DEMO_NOTICE}


@router.post("/admin/tier-requests/{subject}/{application_id}/decision")
async def decide(subject: str, application_id: str, request: Request,
                 body: Review, response: Response) -> dict:
    session = await _session(request, "admin")
    _csrf(request, session)
    if body.decision not in {"APPROVED_FOR_DEMO", "REJECTED"}:
        raise HTTPException(400, "승인 또는 거절만 선택할 수 있습니다.")
    try:
        UUID(subject)
        UUID(application_id)
    except ValueError as exc:
        raise HTTPException(400, "신청 식별자가 올바르지 않습니다.") from exc
    try:
        item = await asyncio.to_thread(get_tier_store().decide, subject, application_id,
                                       session.subject, body.decision, _now())
    except TierConflict as exc:
        raise HTTPException(409, "신청 상태가 이미 변경되었습니다.") from exc
    response.headers["Cache-Control"] = "no-store"
    return {"application": item, "price": "00원", "notice": DEMO_NOTICE}
