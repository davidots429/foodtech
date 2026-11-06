"""Execution-ID-only gateway entry point for the authenticated internal caller."""

from .policy import Caller, Decision, GatewayPolicy
from .registry import DynamoRegistry, RegistryConflict


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
        execution = self.registry.execution(execution_id)
        model = self.registry.model(decision.model_id) if decision.model_id else None
        if not execution or not model or execution.tenant_id != caller.tenant_id:
            return Decision(False, "EXECUTION_NOT_FOUND")
        try:
            self.registry.reserve(execution, model.provider)
        except RegistryConflict:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        return decision
