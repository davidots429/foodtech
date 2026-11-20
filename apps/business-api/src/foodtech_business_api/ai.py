"""Customer request path through the mutually authenticated AI Gateway."""

import os
import ssl
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


async def _invoke_local(tenant_id: str, prompt: str | None = None, *, messages: list[dict] | None = None) -> dict:
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            created = await client.post(f"{url}/internal/ai/executions",
                                        json={"tenant_id": tenant_id})
            created.raise_for_status()
            execution_id = created.json()["execution_id"]
            content = {"messages": messages} if messages is not None else {"prompt": prompt}
            invoked = await client.post(f"{url}/internal/ai/executions/{execution_id}/invoke",
                                        json={"tenant_id": tenant_id, **content})
            if invoked.status_code == 403:
                try:
                    detail = invoked.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if detail == "LIMIT_EXCEEDED":
                    raise HTTPException(429, "AI_USAGE_LIMIT_UNAVAILABLE")
                if isinstance(detail, str) and detail in {"LOCAL_MODEL_REQUIRED", "LOCAL_MODEL_UNAVAILABLE", "MODEL_NOT_ACTIVE",
                              "MODEL_CONFIGURATION_INVALID"}:
                    raise HTTPException(503, "AI_MODEL_UNAVAILABLE")
                if detail == "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED":
                    raise HTTPException(503, "AI_TEMPORARILY_UNAVAILABLE")
                raise HTTPException(502, "AI_GATEWAY_ERROR")
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
    if sum(len(message.content) for message in body.messages) > 3400:
        raise HTTPException(422, "대화가 너무 깁니다. 이전 대화를 지우고 다시 질문해 주세요.")
    return await _invoke_local(session.subject, messages=[message.model_dump() for message in body.messages])


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
