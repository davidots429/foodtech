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
from .provider import LiteLLMProvider
from .vault import DynamoCredentialVault
from .external_flow import ExternalFlow, ExternalFlowError, FIXTURE_PROMPT

app = FastAPI(title="Foodtech AI Gateway")


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewExecution(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)


class InvokeExecution(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=4000)


class ExternalPreview(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    credential_id: str = Field(min_length=1, max_length=128)


class ExternalConfirm(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    notice_version: str = Field(min_length=1, max_length=64)


def _external_enabled() -> None:
    if os.getenv("FOODTECH_EXTERNAL_EXECUTION_ENABLED") != "true":
        raise HTTPException(503, "EXTERNAL_EXECUTION_DISABLED")


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
    vault_table = os.getenv("FOODTECH_CREDENTIAL_VAULT_TABLE", "")
    key_arns = {value.strip() for value in os.getenv("FOODTECH_CREDENTIAL_KMS_KEY_ARNS", "").split(",")
                if value.strip()}
    vault = (DynamoCredentialVault(vault_table, os.getenv("FOODTECH_STAGE", "dev"), key_arns)
             if vault_table and key_arns else None)
    return GatewayService(registry, vault=vault, provider=LiteLLMProvider() if vault else None)


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
    execution = service.registry.execution(execution_id)
    if not execution or execution.tenant_id != body.tenant_id:
        raise HTTPException(403, "EXECUTION_NOT_FOUND")
    if execution.model_id == "llama3.1:8b":
        outcome = await service.execute_local(Caller("business-api", body.tenant_id),
                                              execution_id, body.prompt, ollama)
    else:
        raise HTTPException(403, "EXTERNAL_FIXTURE_CONFIRMATION_REQUIRED")
    if outcome.state == "DENIED":
        raise HTTPException(403, outcome.error_code)
    return {"execution_id": outcome.execution_id, "state": outcome.state,
            "response_text": outcome.response_text, "error_code": outcome.error_code}


@app.post("/internal/ai/external/preview", status_code=201)
async def external_preview(request: Request, body: ExternalPreview) -> dict:
    _internal(request)
    _external_enabled()
    try:
        return ExternalFlow(_service(request).registry).preview(
            body.tenant_id, body.model_id, body.credential_id)
    except ExternalFlowError as error:
        raise HTTPException(409, error.code) from None


@app.post("/internal/ai/external/quotes/{quote_id}/confirm")
async def external_confirm(request: Request, quote_id: str, body: ExternalConfirm) -> dict:
    _internal(request)
    _external_enabled()
    service = _service(request)
    try:
        execution_id = ExternalFlow(service.registry).confirm(
            body.tenant_id, quote_id, body.notice_version)
    except ExternalFlowError as error:
        raise HTTPException(409, error.code) from None
    outcome = await service.execute_external(
        Caller("business-api", body.tenant_id), execution_id, FIXTURE_PROMPT)
    if outcome.state == "DENIED":
        raise HTTPException(403, outcome.error_code)
    result = service.registry.execution_result(execution_id)
    return {"execution_id": outcome.execution_id, "state": outcome.state,
            "response_text": outcome.response_text, "error_code": outcome.error_code,
            "actual_cost_usd": str(result["actual_cost_usd"]) if result else None,
            "settlement_status": result.get("settlement_status") if result else None}


@app.get("/internal/ai/executions/{execution_id}/status")
async def execution_status(request: Request, execution_id: str, tenant_id: str) -> dict:
    _internal(request)
    service = _service(request)
    execution = service.registry.execution(execution_id)
    if not execution or execution.tenant_id != tenant_id:
        raise HTTPException(404, "EXECUTION_NOT_FOUND")
    state = service.registry.execution_state(execution_id)
    result = service.registry.execution_result(execution_id)
    return {"execution_id": execution_id, "state": state,
            "response_text": result.get("response_text") if result else None,
            "error_code": result.get("error_code") if result else None,
            "actual_cost_usd": str(result["actual_cost_usd"]) if result else None,
            "settlement_status": result.get("settlement_status") if result else None}
