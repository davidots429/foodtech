"""Execution-ID-only gateway entry point for the authenticated internal caller."""

from dataclasses import dataclass
from decimal import Decimal

from .policy import Caller, Decision, GatewayPolicy
from .ollama import OllamaClient, OllamaFailure
from .registry import DynamoRegistry, RegistryConflict


@dataclass(frozen=True)
class ExecutionOutcome:
    execution_id: str
    state: str
    response_text: str | None = None
    error_code: str | None = None


class GatewayService:
    def __init__(self, registry: DynamoRegistry):
        self.registry = registry
        self.policy = GatewayPolicy(registry)

    def prepare(self, caller: Caller, execution_id: str) -> Decision:
        # The caller supplies no model, credential, consent or cost fields.
        self.registry.begin_request()
        decision = self.policy.authorize(caller, execution_id)
        if not decision.allowed:
            return decision
        if decision.fallback_used:
            return Decision(False, "FALLBACK_REQUIRES_NEW_EXECUTION",
                            decision.model_id, requires_new_execution=True)
        authorized_versions = self.registry.snapshot_versions()
        execution = self.registry.execution(execution_id)
        model = self.registry.model(decision.model_id) if decision.model_id else None
        if not execution or not model or execution.tenant_id != caller.tenant_id:
            return Decision(False, "EXECUTION_NOT_FOUND")
        if self.registry.snapshot_versions() != authorized_versions:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        try:
            self.registry.reserve(execution, model.provider)
        except RegistryConflict:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        return decision

    async def execute_local(self, caller: Caller, execution_id: str, prompt: str,
                            ollama: OllamaClient) -> ExecutionOutcome:
        execution = self.registry.execution(execution_id)
        if not execution or execution.tenant_id != caller.tenant_id:
            return ExecutionOutcome(execution_id, "DENIED", error_code="EXECUTION_NOT_FOUND")
        model = self.registry.model(execution.model_id)
        if not model or not model.local:
            return ExecutionOutcome(execution_id, "DENIED", error_code="LOCAL_MODEL_REQUIRED")
        decision = self.prepare(caller, execution_id)
        if not decision.allowed:
            return ExecutionOutcome(execution_id, "DENIED", error_code=decision.code)
        try:
            result = await ollama.generate(decision.model_id, prompt)
        except OllamaFailure as error:
            self.registry.finish(execution_id, succeeded=False,
                                 actual_cost_usd=Decimal("0"), error_code=error.code)
            return ExecutionOutcome(execution_id, "FAILED", error_code=error.code)
        except Exception:
            self.registry.finish(execution_id, succeeded=False,
                                 actual_cost_usd=Decimal("0"),
                                 error_code="OLLAMA_UNEXPECTED_ERROR")
            return ExecutionOutcome(execution_id, "FAILED", error_code="OLLAMA_UNEXPECTED_ERROR")
        self.registry.finish(execution_id, succeeded=True,
                             actual_cost_usd=Decimal("0"), response_text=result.text)
        return ExecutionOutcome(execution_id, "SUCCEEDED", response_text=result.text)
