"""Opt-in, pinned transport for the BFF's Tonkatsu Gateway records."""
import os

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from fastapi import HTTPException
import httpx

from .combi_read_delivery import CombiDeliverySettings, _context

ENV_PREFIX = "FOODTECH_TONKATSU_GATEWAY_RECORDS_"


def configured_tonkatsu_records_connection() -> tuple[str, httpx.AsyncClient] | None:
    mode = os.getenv(ENV_PREFIX + "ENABLED", "false")
    if mode == "false":
        return None
    try:
        if mode != "true":
            raise ValueError
        settings = CombiDeliverySettings(
            gateway_url=os.environ[ENV_PREFIX + "URL"],
            server_certificate=os.environ[ENV_PREFIX + "SERVER_CERT"],
            broker_certificate=os.environ[ENV_PREFIX + "CLIENT_CERT"],
            broker_key=os.environ[ENV_PREFIX + "CLIENT_KEY"],
        )
        context = _context(settings)
    except (ValueError, KeyError, OSError, x509.ExtensionNotFound, x509.DuplicateExtension, UnsupportedAlgorithm):
        raise HTTPException(503, "돈까스 권고 기록 연결 준비가 완료되지 않았습니다.",
                            headers={"Cache-Control": "no-store"}) from None
    return settings.gateway_url, httpx.AsyncClient(
        verify=context, timeout=195, trust_env=False, follow_redirects=False, http2=False,
    )
