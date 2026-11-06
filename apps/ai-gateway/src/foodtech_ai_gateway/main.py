import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .ollama import OllamaClient
from .policy import Caller, Execution, Feature
from .registry import DynamoRegistry
from .service import GatewayService

app = FastAPI(title="Foodtech AI Gateway")


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewExecution(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)


class InvokeExecution(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=4000)


def _internal(request: Request) -> None:
    # The dedicated runner enforces a business-client certificate at the TLS
    # handshake. Plain uvicorn and direct ASGI calls cannot enable this route.
    if request.scope.get("scheme") != "https" or not getattr(
        request.app.state, "mtls_listener", False,
    ):
        raise HTTPException(401, "internal mTLS authentication required")


def _service(request: Request) -> GatewayService:
    override = getattr(request.app.state, "gateway_service", None)
    if override:
        return override
    table = os.getenv("FOODTECH_AI_REGISTRY_TABLE", "")
    if not table:
        raise HTTPException(503, "AI registry is not configured")
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    registry = DynamoRegistry(table, period, local_models={"llama3.1:8b"})
    return GatewayService(registry)


@app.post("/internal/ai/executions", status_code=201)
async def create_execution(request: Request, body: NewExecution) -> dict:
    _internal(request)
    service = _service(request)
    execution_id = str(uuid4())
    execution = Execution(execution_id, body.tenant_id, Feature.GENERAL_GUIDE,
                          "llama3.1:8b", None, "CUSTOMER", Decimal("0"))
    service.registry.create_execution(execution)
    return {"execution_id": execution_id, "state": "PENDING"}


@app.post("/internal/ai/executions/{execution_id}/invoke")
async def invoke_execution(request: Request, execution_id: str, body: InvokeExecution) -> dict:
    _internal(request)
    service = _service(request)
    ollama = getattr(request.app.state, "ollama_client", None) or OllamaClient(
        os.getenv("FOODTECH_OLLAMA_URL", "http://127.0.0.1:11434"))
    outcome = await service.execute_local(Caller("business-api", body.tenant_id),
                                          execution_id, body.prompt, ollama)
    if outcome.state == "DENIED":
        raise HTTPException(403, outcome.error_code)
    return {"execution_id": outcome.execution_id, "state": outcome.state,
            "response_text": outcome.response_text, "error_code": outcome.error_code}
