"""Customer request path through the mutually authenticated AI Gateway."""

import os
import ssl
import json
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import _csrf, _session


router = APIRouter(prefix="/api/customer/ai", tags=["ai"])


class GuidanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def ordered_turns(self) -> "ChatRequest":
        if len(self.messages) % 2 != 1 or any(
            message.role != ("user" if index % 2 == 0 else "assistant")
            or not message.content.strip()
            for index, message in enumerate(self.messages)
        ):
            raise ValueError("chat messages must alternate and end with a user question")
        return self


class ExternalPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128)
    credential_id: str = Field(min_length=1, max_length=128)


class ExternalConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notice_version: str = Field(min_length=1, max_length=64)


def _gateway_settings() -> tuple[str, str, str, str]:
    url = os.getenv("FOODTECH_GATEWAY_URL", "")
    ca = os.getenv("FOODTECH_GATEWAY_CA", "")
    cert = os.getenv("FOODTECH_BUSINESS_CLIENT_CERT", "")
    key = os.getenv("FOODTECH_BUSINESS_CLIENT_KEY", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"127.0.0.1", "localhost"} or \
            parsed.path not in {"", "/"} or parsed.username or parsed.password or \
            parsed.query or parsed.fragment or \
            not all(os.path.isfile(path) for path in (ca, cert, key)):
        raise HTTPException(503, "AI Gateway mTLS is not configured")
    return url.rstrip("/"), ca, cert, key


def _client(ca: str, cert: str, key: str) -> httpx.AsyncClient:
    context = ssl.create_default_context(cafile=ca)
    context.load_cert_chain(certfile=cert, keyfile=key)
    return httpx.AsyncClient(verify=context, timeout=195, trust_env=False)


async def _invoke_local(tenant_id: str, prompt: str) -> dict:
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            created = await client.post(f"{url}/internal/ai/executions",
                                        json={"tenant_id": tenant_id})
            created.raise_for_status()
            execution_id = created.json()["execution_id"]
            invoked = await client.post(f"{url}/internal/ai/executions/{execution_id}/invoke",
                                        json={"tenant_id": tenant_id, "prompt": prompt})
            invoked.raise_for_status()
            return invoked.json()
    except (httpx.HTTPError, KeyError, ValueError, OSError) as error:
        raise HTTPException(502, "AI Gateway 실행을 완료하지 못했습니다.") from error


@router.post("/guidance")
async def local_guidance(request: Request, body: GuidanceRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    return await _invoke_local(session.subject, body.prompt)


@router.post("/chat")
async def local_chat(request: Request, body: ChatRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    # The client may provide earlier turns as context, but never identity or policy.
    transcript = json.dumps([message.model_dump() for message in body.messages],
                            ensure_ascii=False, separators=(",", ":"))
    prompt = (
        "스마트키친 조리 의사결정 지원 대화입니다. 이전 대화는 참고 자료입니다. "
        "확인되지 않은 측정값을 만들지 말고, 안전 이상은 현장 운영자 확인을 안내하세요. "
        "장비를 직접 제어하거나 권장값을 자동 적용한다고 말하지 마세요. "
        "마지막 사용자 질문에 한국어로 간결하게 답하세요.\n대화: " + transcript
    )
    if len(prompt) > 4000:
        raise HTTPException(422, "대화가 너무 깁니다. 이전 대화를 지우고 다시 질문해 주세요.")
    return await _invoke_local(session.subject, prompt)


@router.post("/external/preview", status_code=201)
async def external_preview(request: Request, body: ExternalPreviewRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.post(f"{url}/internal/ai/external/preview", json={
                "tenant_id": session.subject, "model_id": body.model_id,
                "credential_id": body.credential_id})
            result.raise_for_status()
            return result.json()
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "외부 실행 견적을 만들지 못했습니다.") from None


@router.post("/external/quotes/{quote_id}/confirm")
async def external_confirm(request: Request, quote_id: str, body: ExternalConfirmRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    if len(quote_id) != 36:
        raise HTTPException(400, "견적 ID가 올바르지 않습니다.")
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.post(f"{url}/internal/ai/external/quotes/{quote_id}/confirm",
                                       json={"tenant_id": session.subject,
                                             "notice_version": body.notice_version})
            result.raise_for_status()
            return result.json()
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "외부 실행 결과를 확인하지 못했습니다. 실행 상태를 조회해 주세요.") from None


@router.get("/executions/{execution_id}/status")
async def execution_status(request: Request, execution_id: str) -> dict:
    session = await _session(request, "customer")
    if len(execution_id) != 36:
        raise HTTPException(400, "실행 ID가 올바르지 않습니다.")
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.get(f"{url}/internal/ai/executions/{execution_id}/status",
                                      params={"tenant_id": session.subject})
            result.raise_for_status()
            return result.json()
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "실행 상태를 읽지 못했습니다.") from None
