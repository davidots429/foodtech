"""Customer request path through the mutually authenticated AI Gateway."""

import os
import ssl
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import _csrf, _session


router = APIRouter(prefix="/api/customer/ai", tags=["ai"])


class GuidanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)


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


@router.post("/guidance")
async def local_guidance(request: Request, body: GuidanceRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            created = await client.post(f"{url}/internal/ai/executions",
                                        json={"tenant_id": session.subject})
            created.raise_for_status()
            execution_id = created.json()["execution_id"]
            invoked = await client.post(f"{url}/internal/ai/executions/{execution_id}/invoke",
                                        json={"tenant_id": session.subject,
                                              "prompt": body.prompt})
            invoked.raise_for_status()
            return invoked.json()
    except (httpx.HTTPError, KeyError, ValueError, OSError) as error:
        raise HTTPException(502, "AI Gateway 실행을 완료하지 못했습니다.") from error
