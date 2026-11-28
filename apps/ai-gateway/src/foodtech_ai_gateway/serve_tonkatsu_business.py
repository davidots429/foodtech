"""Private Gateway listener for the business API's Tonkatsu request and record routes."""
import hmac
import os
import ssl

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from .combi_tls import client_leaf_pin, pinned_http_protocol

BUSINESS_URI = "urn:foodtech:service:business-api"
PEER_STATE = "tonkatsu_business_peer"
TLS_PREFIX = "FOODTECH_TONKATSU_BUSINESS_GATEWAY_"


def authenticate_business_peer(request: Request) -> None:
    expected = request.app.state.tonkatsu_business_pin
    peer = request.scope.get("state", {}).get(PEER_STATE)
    if request.url.scheme != "https" or not isinstance(peer, str) or not hmac.compare_digest(peer, expected):
        raise HTTPException(401, "internal mTLS authentication required", headers={"Cache-Control": "no-store"})


def business_server_config(*, server_cert, server_key, client_ca, client_certificate,
                           host="127.0.0.1", port=8447):
    from .main import (
        tonkatsu_recipe_request, tonkatsu_recipe_result, tonkatsu_recipe_status, tonkatsu_review_record,
    )

    pin = client_leaf_pin(client_certificate, client_uri=BUSINESS_URI)
    if type(port) is not int or not 1 <= port <= 65535 or not host:
        raise ValueError("business listener address required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.tonkatsu_business_pin = pin
    app.add_api_route("/internal/ai/tonkatsu-recipe-requests", tonkatsu_recipe_request, methods=["POST"], status_code=201)
    app.add_api_route("/internal/ai/tonkatsu-recipe-status/{execution_id}", tonkatsu_recipe_status, methods=["POST"])
    app.add_api_route("/internal/ai/tonkatsu-recipe-results/{execution_id}", tonkatsu_recipe_result, methods=["POST"])
    app.add_api_route("/internal/ai/tonkatsu-recipe-review-context/{execution_id}", tonkatsu_review_record, methods=["POST"])
    config = uvicorn.Config(
        app, host=host, port=port, http=pinned_http_protocol(pin, state_key=PEER_STATE),
        ws="none", ssl_version=ssl.PROTOCOL_TLS_SERVER, ssl_certfile=server_cert,
        ssl_keyfile=server_key, ssl_ca_certs=client_ca, ssl_cert_reqs=ssl.CERT_REQUIRED,
        proxy_headers=False, access_log=False, log_level="error",
    )
    config.load()
    if config.ssl is None or config.ssl.verify_mode != ssl.CERT_REQUIRED or config.ssl.keylog_filename is not None:
        raise ValueError("business mutual TLS context required")
    return config


def main() -> None:
    if os.getenv(TLS_PREFIX + "ENABLED") != "true":
        raise ValueError("business listener disabled")
    if not os.getenv("FOODTECH_AI_REGISTRY_TABLE") or not os.getenv("FOODTECH_AUTH_TABLE"):
        raise ValueError("fryer registry and authentication tables required")
    config = business_server_config(
        server_cert=os.environ[TLS_PREFIX + "SERVER_CERT"],
        server_key=os.environ[TLS_PREFIX + "SERVER_KEY"],
        client_ca=os.environ[TLS_PREFIX + "CLIENT_CA"],
        client_certificate=os.environ[TLS_PREFIX + "CLIENT_CERT"],
        host=os.getenv(TLS_PREFIX + "BIND_HOST", "127.0.0.1"),
        port=int(os.getenv(TLS_PREFIX + "PORT", "8447")),
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
