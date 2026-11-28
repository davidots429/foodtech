"""Serve the one private inference call that may reach host-local Ollama."""
import asyncio
import hmac
import json
import os
import ssl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

from .combi_tls import client_leaf_pin, pinned_http_protocol
from .ollama import OllamaClient, OllamaFailure, TONKATSU_INFERENCE_MODEL
from .recommendation_generation import tonkatsu_model_request

CLIENT_URI = "urn:foodtech:service:tonkatsu-gateway-inference"
PEER_STATE = "tonkatsu_inference_peer"
PREFIX = "FOODTECH_TONKATSU_INFERENCE_"
NO_CACHE = {"Cache-Control": "no-store"}


def inference_server_config(*, server_cert, server_key, client_ca, client_certificate,
                            host="127.0.0.1", port=8448, ollama=None):
    pin = client_leaf_pin(client_certificate, client_uri=CLIENT_URI)
    if not host or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid inference listener address")
    local = ollama if ollama is not None else OllamaClient(timeout=170)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.post("/api/chat")
    async def chat(request: Request):
        peer = request.scope.get("state", {}).get(PEER_STATE)
        if request.url.scheme != "https" or not isinstance(peer, str) or not hmac.compare_digest(peer, pin):
            raise HTTPException(401, "inference peer unavailable", headers=NO_CACHE)
        if request.scope.get("query_string") or request.headers.get("Content-Encoding", "identity") != "identity":
            raise HTTPException(400, "inference request unavailable", headers=NO_CACHE)
        raw = bytearray()
        try:
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 65536:
                        raise HTTPException(413, "inference request too large", headers=NO_CACHE)
                    raw.extend(chunk)
            body = json.loads(raw)
            if (type(body) is not dict or set(body) != {"model", "messages", "format", "stream", "options"}
                    or body["model"] != TONKATSU_INFERENCE_MODEL or body["stream"] is not False
                    or body["options"] != {"num_ctx": 4096, "num_predict": 128}
                    or not tonkatsu_model_request(body["messages"], body["format"])):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "inference request unavailable", headers=NO_CACHE) from None
        except TimeoutError:
            raise HTTPException(408, "inference request timed out", headers=NO_CACHE) from None
        try:
            async with asyncio.timeout(175):
                result = await local.structured_chat(TONKATSU_INFERENCE_MODEL, body["messages"], body["format"])
        except TimeoutError:
            raise HTTPException(504, "local inference timed out", headers=NO_CACHE) from None
        except OllamaFailure:
            raise HTTPException(502, "local inference unavailable", headers=NO_CACHE) from None
        return JSONResponse({"done": True, "message": {"role": "assistant", "content": result.text},
                             "prompt_eval_count": result.prompt_tokens, "eval_count": result.output_tokens},
                            headers=NO_CACHE)

    config = uvicorn.Config(
        app, host=host, port=port, http=pinned_http_protocol(pin, state_key=PEER_STATE),
        ws="none", ssl_version=ssl.PROTOCOL_TLS_SERVER, ssl_certfile=server_cert,
        ssl_keyfile=server_key, ssl_ca_certs=client_ca, ssl_cert_reqs=ssl.CERT_REQUIRED,
        proxy_headers=False, access_log=False, log_level="error",
    )
    config.load()
    if config.ssl is None or config.ssl.verify_mode != ssl.CERT_REQUIRED or config.ssl.keylog_filename is not None:
        raise ValueError("inference listener requires native mutual TLS")
    return config


def main() -> None:
    if os.getenv(PREFIX + "ENABLED") != "true":
        raise ValueError("private inference listener disabled")
    if os.getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") or os.getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        raise ValueError("inference listener requires a host without an ECS task role")
    config = inference_server_config(
        server_cert=os.environ[PREFIX + "SERVER_CERT"],
        server_key=os.environ[PREFIX + "SERVER_KEY"],
        client_ca=os.environ[PREFIX + "CLIENT_CA"],
        client_certificate=os.environ[PREFIX + "CLIENT_CERT"],
        host=os.getenv(PREFIX + "BIND_HOST", "127.0.0.1"),
        port=int(os.getenv(PREFIX + "PORT", "8448")),
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
