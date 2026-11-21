"""Execution-ID-only gateway entry point for the authenticated internal caller."""

from dataclasses import dataclass
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json

from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import JeyukBaseline, TonkatsuBaseline

from .policy import Caller, Decision, Feature, GatewayPolicy
from .ollama import OllamaClient, OllamaFailure
from .registry import DynamoRegistry, RegistryConflict
from .recipe_source_guard import BoundRecipeSourceGuard, DynamoRecipeSourceGuard
from .provider import LiteLLMProvider, ProviderFailure
from .vault import DynamoCredentialVault, VaultFailure
from .recommendation_generation import generate_candidate


class _RecommendationDenied(Exception):
    def __init__(self, code: str):
        self.code = code


@dataclass(frozen=True)
class ExecutionOutcome:
    execution_id: str
    state: str
    response_text: str | None = None
    error_code: str | None = None


class GatewayService:
    def __init__(self, registry: DynamoRegistry, *, vault: DynamoCredentialVault | None = None,
                 provider: LiteLLMProvider | None = None, recipe_source_guard=None):
        self.registry = registry
        self.policy = GatewayPolicy(registry)
        self.vault = vault
        self.provider = provider
        self.recipe_source_guard = recipe_source_guard

    def prepare(self, caller: Caller, execution_id: str, *, local_recipe: bool = False,
                disallow_recipe: bool = False) -> Decision:
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
        if local_recipe and (execution.feature != Feature.RECIPE_CORRECTION or not model.local or
                             model.provider != "ollama" or execution.estimated_cost_usd != 0):
            return Decision(False, "LOCAL_RECIPE_REQUIRED")
        if disallow_recipe and execution.feature == Feature.RECIPE_CORRECTION:
            return Decision(False, "STRUCTURED_RECIPE_REQUIRED")
        if self.registry.snapshot_versions() != authorized_versions:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        try:
            self.registry.reserve(execution, model.provider)
        except RegistryConflict:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        return decision

    def prepare_local_recipe_retry(self, caller: Caller, execution_id: str, attempt: int) -> Decision:
        """Reauthorize and reserve a single additional local provider request."""
        self.registry.begin_request()
        decision = self.policy.authorize(caller, execution_id)
        if not decision.allowed:
            return decision
        versions = self.registry.snapshot_versions()
        execution = self.registry.execution(execution_id)
        model = self.registry.model(decision.model_id) if decision.model_id else None
        if not execution or execution.tenant_id != caller.tenant_id or \
                execution.feature != Feature.RECIPE_CORRECTION or not model or not model.local or \
                model.provider != "ollama" or execution.estimated_cost_usd != 0:
            return Decision(False, "LOCAL_RECIPE_RETRY_NOT_ALLOWED")
        if self.registry.snapshot_versions() != versions:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        try:
            self.registry.reserve_local_retry(execution, attempt)
        except RegistryConflict:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        return decision

    async def execute_local_recipe(self, caller: Caller, execution_id: str, *,
                                   evidence: Evidence, baseline: TonkatsuBaseline | JeyukBaseline,
                                   context: dict, ollama: OllamaClient) -> ExecutionOutcome:
        """Internal only: caller supplies verified, tenant-scoped server records.

        No HTTP route accepts these arguments. Each provider request is reserved
        independently and only a validated candidate is stored on success.
        """
        execution = self.registry.execution(execution_id)
        model = self.registry.model(execution.model_id) if execution else None
        if not execution or execution.tenant_id != caller.tenant_id:
            return ExecutionOutcome(execution_id, "DENIED", error_code="EXECUTION_NOT_FOUND")
        if execution.feature != Feature.RECIPE_CORRECTION or not model or not model.local or \
                model.provider != "ollama" or execution.estimated_cost_usd != 0:
            return ExecutionOutcome(execution_id, "DENIED", error_code="LOCAL_RECIPE_REQUIRED")
        if isinstance(baseline, JeyukBaseline) and self.recipe_source_guard is None:
            return ExecutionOutcome(execution_id, "DENIED", error_code="SOURCE_GUARD_UNAVAILABLE")
        if isinstance(baseline, JeyukBaseline):
            guard = self.recipe_source_guard
            if type(guard) is not BoundRecipeSourceGuard or type(guard.guard) is not DynamoRecipeSourceGuard or \
                    guard.execution != execution:
                return ExecutionOutcome(execution_id, "DENIED", error_code="SOURCE_SCOPE_UNAVAILABLE")
            try:
                checks = guard.settlement_checks()
                if any(check['ConditionCheck']['TableName'] == self.registry.table_name for check in checks):
                    raise ValueError('source table conflicts with execution registry')
            except (TypeError, ValueError, KeyError):
                return ExecutionOutcome(execution_id, "DENIED", error_code="SOURCE_SCOPE_UNAVAILABLE")
        attempts = 0
        reserved = False

        async def source_current():
            if not isinstance(baseline, JeyukBaseline):
                return True
            try:
                if await asyncio.to_thread(self.registry.execution, execution_id) != execution:
                    return False
                if await asyncio.to_thread(self.recipe_source_guard.verify, execution) is not True:
                    return False
                return await asyncio.to_thread(self.registry.execution, execution_id) == execution
            except Exception:
                return False

        def finish_recipe(*, succeeded: bool, response_text: str | None = None, error_code: str | None = None) -> bool:
            try:
                self.registry.finish(execution_id, succeeded=succeeded, actual_cost_usd=Decimal('0'),
                                     response_text=response_text, error_code=error_code,
                                     expected_execution=execution,
                                     recipe_source_guard=self.recipe_source_guard if succeeded and
                                         isinstance(baseline, JeyukBaseline) else None)
                return True
            except RegistryConflict:
                return False

        async def invoke(messages, schema):
            nonlocal attempts, reserved
            if not await source_current():
                raise _RecommendationDenied("RECIPE_SOURCE_CHANGED_OR_UNAVAILABLE")
            attempts += 1
            decision = (self.prepare(caller, execution_id, local_recipe=True) if attempts == 1 else
                        self.prepare_local_recipe_retry(caller, execution_id, attempts))
            if not decision.allowed:
                raise _RecommendationDenied(decision.code)
            reserved = True
            return (await ollama.structured_chat(decision.model_id, messages, schema)).text

        try:
            result = await generate_candidate(invoke, evidence=evidence, baseline=baseline, context=context)
            if not reserved:
                return ExecutionOutcome(execution_id, "INDETERMINATE", error_code="EVIDENCE_INSUFFICIENT")
        except _RecommendationDenied as error:
            if not reserved:
                return ExecutionOutcome(execution_id, "DENIED", error_code=error.code)
            code = error.code
        except OllamaFailure as error:
            code = error.code
        except (ValueError, TypeError):
            code = "RECOMMENDATION_INPUT_INVALID"
        except Exception:
            code = "RECOMMENDATION_EXECUTION_FAILED"
        else:
            if not await source_current():
                code = "RECIPE_SOURCE_CHANGED_OR_UNAVAILABLE"
                if reserved:
                    if not finish_recipe(succeeded=False, error_code=code):
                        return ExecutionOutcome(execution_id, "FAILED", error_code="SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED")
                    return ExecutionOutcome(execution_id, "FAILED", error_code=code)
                return ExecutionOutcome(execution_id, "DENIED", error_code=code)
            if result["status"] == "NEEDS_REVIEW":
                response = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if not finish_recipe(succeeded=True, response_text=response):
                    return ExecutionOutcome(execution_id, "FAILED", error_code="SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED")
                return ExecutionOutcome(execution_id, "SUCCEEDED", response_text=response)
            code = result["error_code"]
        if reserved:
            if not finish_recipe(succeeded=False, error_code=code):
                return ExecutionOutcome(execution_id, "FAILED", error_code="SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED")
            return ExecutionOutcome(execution_id, "FAILED", error_code=code)
        return ExecutionOutcome(execution_id, "DENIED", error_code=code)

    async def execute_local(self, caller: Caller, execution_id: str, prompt: str | None,
                            ollama: OllamaClient, *, messages: list[dict] | None = None) -> ExecutionOutcome:
        execution = self.registry.execution(execution_id)
        if not execution or execution.tenant_id != caller.tenant_id:
            return ExecutionOutcome(execution_id, "DENIED", error_code="EXECUTION_NOT_FOUND")
        if execution.feature == Feature.RECIPE_CORRECTION:
            return ExecutionOutcome(execution_id, "DENIED", error_code="STRUCTURED_RECIPE_REQUIRED")
        model = self.registry.model(execution.model_id)
        if not model or not model.local:
            return ExecutionOutcome(execution_id, "DENIED", error_code="LOCAL_MODEL_REQUIRED")
        decision = self.prepare(caller, execution_id, disallow_recipe=True)
        if not decision.allowed:
            return ExecutionOutcome(execution_id, "DENIED", error_code=decision.code)
        try:
            result = (await ollama.chat(decision.model_id, messages) if messages is not None
                      else await ollama.generate(decision.model_id, prompt))
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
        if execution.feature == Feature.RECIPE_CORRECTION:
            return ExecutionOutcome(execution_id, "DENIED", error_code="STRUCTURED_RECIPE_REQUIRED")
        model = self.registry.model(execution.model_id)
        if not model or model.local or not execution.credential_id:
            return ExecutionOutcome(execution_id, "DENIED", error_code="EXTERNAL_MODEL_REQUIRED")
        decision = self.prepare(caller, execution_id, disallow_recipe=True)
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
