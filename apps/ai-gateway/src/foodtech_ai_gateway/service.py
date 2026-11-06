"""Execution-ID-only gateway entry point for the authenticated internal caller."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .policy import Caller, Decision, GatewayPolicy
from .ollama import OllamaClient, OllamaFailure
from .registry import DynamoRegistry, RegistryConflict
from .provider import LiteLLMProvider, ProviderFailure
from .vault import DynamoCredentialVault, VaultFailure


@dataclass(frozen=True)
class ExecutionOutcome:
    execution_id: str
    state: str
    response_text: str | None = None
    error_code: str | None = None


class GatewayService:
    def __init__(self, registry: DynamoRegistry, *, vault: DynamoCredentialVault | None = None,
                 provider: LiteLLMProvider | None = None):
        self.registry = registry
        self.policy = GatewayPolicy(registry)
        self.vault = vault
        self.provider = provider

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

    async def execute_external(self, caller: Caller, execution_id: str,
                               prompt: str) -> ExecutionOutcome:
        if not self.vault or not self.provider:
            return ExecutionOutcome(execution_id, "DENIED",
                                    error_code="EXTERNAL_GATEWAY_NOT_CONFIGURED")
        execution = self.registry.execution(execution_id)
        if not execution or execution.tenant_id != caller.tenant_id:
            return ExecutionOutcome(execution_id, "DENIED", error_code="EXECUTION_NOT_FOUND")
        model = self.registry.model(execution.model_id)
        if not model or model.local or not execution.credential_id:
            return ExecutionOutcome(execution_id, "DENIED", error_code="EXTERNAL_MODEL_REQUIRED")
        decision = self.prepare(caller, execution_id)
        if not decision.allowed:
            return ExecutionOutcome(execution_id, "DENIED", error_code=decision.code)
        secret: bytearray | None = None
        try:
            # Re-read after the reservation; a revoked or replaced credential
            # must not be used just because it passed the earlier transaction.
            authorized_version = self.registry.snapshot_versions().get(
                f"CREDENTIAL#{execution.credential_id}")
            credential = self.registry.credential(execution.credential_id)
            current_version = self.registry.snapshot_versions().get(
                f"CREDENTIAL#{execution.credential_id}")
            if authorized_version is None or current_version != authorized_version or \
                    not credential or credential.state != "ACTIVE" or \
                    credential.tenant_id != caller.tenant_id or \
                    credential.provider != model.provider or \
                    (credential.expires_at and credential.expires_at <= datetime.now(timezone.utc)):
                raise VaultFailure("CREDENTIAL_NOT_USABLE")
            record = self.vault.read(credential)
            secret = self.vault.decrypt(record)
            result = await self.provider.generate(model.provider, model.model_id, prompt, secret)
        except (VaultFailure, ProviderFailure) as error:
            # A provider can have charged for a failed response. Keep the
            # reservation until billing reconciliation rather than refunding it.
            self.registry.finish(execution_id, succeeded=False,
                                 actual_cost_usd=execution.estimated_cost_usd,
                                 error_code=error.code)
            return ExecutionOutcome(execution_id, "FAILED", error_code=error.code)
        except Exception:
            self.registry.finish(execution_id, succeeded=False,
                                 actual_cost_usd=execution.estimated_cost_usd,
                                 error_code="EXTERNAL_EXECUTION_FAILED")
            return ExecutionOutcome(execution_id, "FAILED",
                                    error_code="EXTERNAL_EXECUTION_FAILED")
        finally:
            if secret is not None:
                secret[:] = b"\x00" * len(secret)
        self.registry.finish(execution_id, succeeded=True,
                             actual_cost_usd=result.actual_cost_usd,
                             response_text=result.text)
        return ExecutionOutcome(execution_id, "SUCCEEDED", response_text=result.text)
