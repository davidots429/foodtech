"""Customer session BFF: forwards ciphertext, never an API key value."""

import os
import ssl
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import _csrf, _session


router = APIRouter(prefix="/api/customer/credentials", tags=["credentials"])


class EncryptedCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=32)
    key_version: str = Field(min_length=1, max_length=32)
    ciphertext_base64: str = Field(min_length=1, max_length=1024)


def _settings() -> tuple[str, str, str, str]:
    url = os.getenv("FOODTECH_REGISTRATION_URL", "")
    ca = os.getenv("FOODTECH_REGISTRATION_CA", "")
    cert = os.getenv("FOODTECH_BUSINESS_CLIENT_CERT", "")
    key = os.getenv("FOODTECH_BUSINESS_CLIENT_KEY", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"127.0.0.1", "localhost"} or \
            parsed.path not in {"", "/"} or parsed.username or parsed.password or \
            parsed.query or parsed.fragment or \
            not all(os.path.isfile(path) for path in (ca, cert, key)):
        raise HTTPException(503, "자격증명 등록 서비스가 설정되지 않았습니다.")
    return url.rstrip("/"), ca, cert, key


def _client(ca: str, cert: str, key: str) -> httpx.AsyncClient:
    context = ssl.create_default_context(cafile=ca)
    context.load_cert_chain(certfile=cert, keyfile=key)
    return httpx.AsyncClient(verify=context, timeout=30, trust_env=False)


@router.get("/transfer-key")
async def transfer_key(request: Request, response: Response) -> dict:
    await _session(request, "customer")
    url, ca, cert, key = _settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.get(f"{url}/internal/credentials/transfer-key")
            result.raise_for_status()
            response.headers["Cache-Control"] = "no-store"
            return result.json()
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "전송 공개키를 읽지 못했습니다.") from None


@router.post("", status_code=201)
async def register(request: Request, body: EncryptedCredential, response: Response) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    url, ca, cert, key = _settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.post(f"{url}/internal/credentials",
                                       json={**body.model_dump(), "tenant_id": session.subject})
            result.raise_for_status()
            response.headers["Cache-Control"] = "no-store"
            return result.json()
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "자격증명 등록을 완료하지 못했습니다.") from None
