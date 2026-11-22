import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from typing import Literal

from .ollama import OllamaClient
from .policy import Caller, Execution, Feature, Tier, UNCONFIGURED_TIER1
from .registry import DynamoRegistry, RegistryConflict
from .service import GatewayService
from .provider import LiteLLMProvider
from .vault import DynamoCredentialVault
from .external_flow import ExternalFlow, ExternalFlowError, FIXTURE_PROMPT
from .downgrade_flow import DowngradeFlow, DowngradeError

app = FastAPI(title="Foodtech AI Gateway")


@app.exception_handler(RegistryConflict)
async def registry_conflict(_request: Request, _error: RegistryConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": "EXECUTION_CONFLICT"},
                        headers={"Cache-Control": "no-store"})


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewExecution(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)


class ChatMessage(StrictBody):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class InvokeExecution(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    messages: list[ChatMessage] | None = Field(default=None, min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_content(self):
        if (self.prompt is None) == (self.messages is None):
            raise ValueError("exactly one input form is required")
        if self.messages is not None and (len(self.messages) % 2 != 1 or
                sum(len(message.content) for message in self.messages) > 4000 or any(
                    message.role != ("user" if i % 2 == 0 else "assistant") or not message.content.strip()
                    for i, message in enumerate(self.messages))):
            raise ValueError("invalid chat history")
        return self


class ExternalPreview(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    credential_id: str = Field(min_length=1, max_length=128)


class ExternalConfirm(StrictBody):
    tenant_id: str = Field(min_length=1, max_length=128)
    notice_version: str = Field(min_length=1, max_length=64)


class PriorBinding(StrictBody):
    prior_job_id: UUID | None = None
    prior_job_version: int | None = Field(default=None, strict=True, ge=1)
    jeyuk_source_revision: int | None = Field(default=None, strict=True, ge=1, le=10**18)
    prior_source_revision: int | None = Field(default=None, strict=True, ge=1, le=10**18)

    @model_validator(mode="after")
    def validate_prior(self):
        if (self.prior_job_id is None) != (self.prior_job_version is None) or \
                (self.prior_job_id is not None and self.prior_job_id == getattr(self, "job_id", None)) or \
                (self.prior_source_revision is not None and self.prior_job_id is None) or \
                (self.jeyuk_source_revision is not None and getattr(self, "job_id", None) is None):
            raise ValueError("invalid prior job binding")
        return self


class DowngradePreview(PriorBinding):
    tenant_id: str = Field(min_length=1, max_length=128)
    parent_execution_id: UUID
    job_id: UUID | None = None
    job_version: int | None = Field(default=None, strict=True, ge=1)
    comparison_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RecipeRequest(PriorBinding):
    tenant_id: str = Field(min_length=1, max_length=128)
    job_id: UUID
    job_version: int = Field(strict=True, ge=1)
    comparison_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DowngradeConfirm(PriorBinding):
    tenant_id: str = Field(min_length=1, max_length=128)
    notice_version: str = Field(min_length=1, max_length=64)
    quality_warning_acknowledged: StrictBool
    job_id: UUID | None = None
    job_version: int | None = Field(default=None, strict=True, ge=1)
    comparison_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


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
    # Customer-scoped source credentials are not yet issued to this service.
    # Do not fall back to the Gateway's long-lived role for recipe source reads.
    return GatewayService(registry, vault=vault, provider=LiteLLMProvider() if vault else None)


def _trial_limits() -> tuple[Decimal, int] | None:
    if os.getenv("FOODTECH_FREE_TRIAL_AUTO_PROVISION") != "true":
        return None
    try:
        calls_text = os.environ["FOODTECH_FREE_TRIAL_MONTHLY_CALLS"]
        usd_text = os.environ["FOODTECH_FREE_TRIAL_MONTHLY_USD"]
        if not calls_text.isascii() or not calls_text.isdigit():
            raise ValueError("invalid call limit")
        calls = int(calls_text)
        usd = Decimal(usd_text)
        if not 1 <= calls <= 1_000_000 or not usd.is_finite() or not Decimal("0") <= usd <= Decimal("1000000"):
            raise ValueError("invalid free-trial limit")
        if len(usd.as_tuple().digits) > 38 or (usd and usd.adjusted() < -128):
            raise ValueError("free-trial limit cannot be stored exactly")
        return (Decimal("0") if not usd else usd), calls
    except (KeyError, ValueError, InvalidOperation):
        raise HTTPException(503, "FREE_TRIAL_LIMIT_CONFIGURATION_INVALID") from None


@app.post("/internal/ai/executions", status_code=201)
async def create_execution(request: Request, body: NewExecution) -> dict:
    _internal(request)
    service = _service(request)
    trial = _trial_limits()
    if trial is not None:
        service.registry.ensure_trial_tenant_limit(body.tenant_id, *trial)
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
            execution_id, body.prompt, ollama,
            messages=[message.model_dump() for message in body.messages] if body.messages is not None else None)
    else:
        raise HTTPException(403, "EXTERNAL_FIXTURE_CONFIRMATION_REQUIRED")
    if outcome.state == "DENIED":
        raise HTTPException(403, outcome.error_code)
    return {"execution_id": outcome.execution_id, "state": outcome.state,
            "response_text": outcome.response_text, "error_code": outcome.error_code}


@app.post("/internal/ai/recipe-downgrades/preview", status_code=201)
async def downgrade_preview(request: Request, body: DowngradePreview) -> dict:
    _internal(request)
    try:
        registry = _service(request).registry
        expected = (str(body.job_id) if body.job_id else None, body.job_version, body.comparison_id,
                    str(body.prior_job_id) if body.prior_job_id else None, body.prior_job_version,
                    body.jeyuk_source_revision, body.prior_source_revision)
        parent = registry.execution(str(body.parent_execution_id))
        if not parent or parent.tenant_id != body.tenant_id or (parent.job_id, parent.job_version, parent.comparison_id, parent.prior_job_id, parent.prior_job_version,
                parent.jeyuk_source_revision, parent.prior_source_revision) != expected:
            raise DowngradeError("JOB_BINDING_MISMATCH")
        result = DowngradeFlow(registry).preview(body.tenant_id, str(body.parent_execution_id))
        if (result["job_id"], result["job_version"], result["comparison_id"], result["prior_job_id"], result["prior_job_version"],
                result["jeyuk_source_revision"], result["prior_source_revision"]) != expected:
            raise DowngradeError("JOB_BINDING_MISMATCH")
        return result
    except DowngradeError as error:
        raise HTTPException(409, error.code) from None


@app.post("/internal/ai/recipe-requests", status_code=201)
async def recipe_request(request: Request, body: RecipeRequest) -> dict:
    _internal(request)
    service = _service(request)
    if service.registry.model(UNCONFIGURED_TIER1) is not None:
        raise HTTPException(503, "RESERVED_MODEL_MARKER_IN_USE")
    model_id = os.getenv("FOODTECH_RECIPE_TIER1_MODEL_ID", "").strip()
    if model_id:
        model = service.registry.model(model_id)
        if model_id == UNCONFIGURED_TIER1 or len(model_id) > 128 or not model or model.tier != Tier.TIER_1:
            raise HTTPException(503, "TIER1_MODEL_CONFIGURATION_INVALID")
    else:
        model_id = UNCONFIGURED_TIER1
    trial = _trial_limits()
    if trial is not None:
        service.registry.ensure_trial_tenant_limit(body.tenant_id, *trial)
    execution_id = str(uuid4())
    service.registry.create_execution(Execution(execution_id, body.tenant_id, Feature.RECIPE_CORRECTION,
        model_id, None, "SYNTHETIC", Decimal("0"),
        job_id=str(body.job_id), job_version=body.job_version, comparison_id=body.comparison_id,
        prior_job_id=str(body.prior_job_id) if body.prior_job_id else None, prior_job_version=body.prior_job_version,
        jeyuk_source_revision=body.jeyuk_source_revision, prior_source_revision=body.prior_source_revision))
    if model_id == UNCONFIGURED_TIER1:
        reason = "TIER1_MODEL_NOT_CONFIGURED"
    else:
        service.registry.begin_request()
        decision = service.policy.authorize(Caller("business-api", body.tenant_id), execution_id)
        reason = "RECOMMENDATION_EVIDENCE_UNVERIFIED" if decision.allowed else decision.code
    return {"execution_id": execution_id, "state": "BLOCKED", "reason": reason,
            "execution_available": False,
            "job_id": str(body.job_id), "job_version": body.job_version, "comparison_id": body.comparison_id,
            "prior_job_id": str(body.prior_job_id) if body.prior_job_id else None, "prior_job_version": body.prior_job_version,
            "jeyuk_source_revision": body.jeyuk_source_revision, "prior_source_revision": body.prior_source_revision}


@app.post("/internal/ai/recipe-downgrades/{quote_id}/confirm", status_code=201)
async def downgrade_confirm(request: Request, quote_id: UUID, body: DowngradeConfirm) -> dict:
    _internal(request)
    try:
        registry = _service(request).registry
        quote = registry.quote(str(quote_id))
        expected = (str(body.job_id) if body.job_id else None, body.job_version, body.comparison_id,
                    str(body.prior_job_id) if body.prior_job_id else None, body.prior_job_version,
                    body.jeyuk_source_revision, body.prior_source_revision)
        if not quote or quote.get("tenant_id") != body.tenant_id or \
                (quote.get("job_id"), quote.get("job_version"), quote.get("comparison_id"),
                 quote.get("prior_job_id"), quote.get("prior_job_version"), quote.get("jeyuk_source_revision"),
                 quote.get("prior_source_revision")) != expected:
            raise DowngradeError("JOB_BINDING_MISMATCH")
        execution_id = DowngradeFlow(registry).confirm(
            body.tenant_id, str(quote_id), body.notice_version, body.quality_warning_acknowledged)
        return {"execution_id": execution_id, "state": "PENDING"}
    except DowngradeError as error:
        raise HTTPException(409, error.code) from None


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
    try:
        snapshot = await asyncio.to_thread(service.registry.execution_status_snapshot, execution_id, tenant_id)
    except RegistryConflict:
        raise HTTPException(409, "EXECUTION_STATUS_CHANGED") from None
    if snapshot is None:
        raise HTTPException(404, "EXECUTION_NOT_FOUND")
    execution, result = snapshot
    return {"execution_id": execution_id, "state": execution.state,
            "response_text": result.get("response_text") if result else None,
            "error_code": result.get("error_code") if result else None,
            "actual_cost_usd": str(result["actual_cost_usd"]) if result else None,
            "settlement_status": result.get("settlement_status") if result else None}
