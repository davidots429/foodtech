"""Execution-ID-only gateway entry point for the authenticated internal caller."""

from dataclasses import dataclass
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from botocore.exceptions import BotoCoreError, ClientError

from foodtech_contracts.combi_recommendation import CombiBaseline
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import JeyukBaseline, TonkatsuBaseline

from .policy import Caller, Decision, ExecutionSnapshot, Feature, GatewayPolicy
from .ollama import OllamaClient, OllamaFailure
from .registry import DynamoRegistry, RegistryConflict
from .combi_read_handoff import ReceivedCombiReads
from .tonkatsu_read_handoff import ReceivedTonkatsuReads
from .tonkatsu_recommendation_inputs import build_tonkatsu_inputs
from .combi_recommendation_inputs import build_combi_inputs
from .recipe_source_guard import BoundRecipeSourceGuard, DynamoRecipeSourceGuard
from .prior_history_conditions import DynamoPriorHistoryConditions
from .recipe_read_authorization import DynamoRecipeReadAuthorization
from .provider import LiteLLMProvider, ProviderFailure
from .vault import DynamoCredentialVault, VaultFailure
from .recommendation_generation import generate_candidate
from .jeyuk_recommendation_inputs import build_jeyuk_inputs


class _RecommendationDenied(Exception):
    def __init__(self, code: str):
        self.code = code


class _ReservationUncertain(Exception):
    pass


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
                disallow_recipe: bool = False, combi_source_guard: ReceivedCombiReads | None = None) -> Decision:
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
            self.registry.reserve(execution, model.provider, **(
                {"combi_source_guard": combi_source_guard} if combi_source_guard is not None else {}))
        except RegistryConflict:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        return decision

    def prepare_local_recipe_retry(self, caller: Caller, execution_id: str, attempt: int, *,
                                   combi_source_guard: ReceivedCombiReads | None = None) -> Decision:
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
            self.registry.reserve_local_retry(execution, attempt, **(
                {"combi_source_guard": combi_source_guard} if combi_source_guard is not None else {}))
        except RegistryConflict:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
        return decision

    async def execute_observed_local_recipe(self, caller: Caller, execution_id: str, *,
                                            ollama: OllamaClient) -> ExecutionOutcome:
        """Server-derived Jeyuk inputs for the opt-in internal invocation route.

        The bound guard must come from the trusted v2 handoff. It observes the
        sources and independently enforces live history/grant conditions.
        """
        execution = await asyncio.to_thread(self.registry.execution, execution_id)
        if not execution or execution.tenant_id != caller.tenant_id:
            return ExecutionOutcome(execution_id, 'DENIED', error_code='EXECUTION_NOT_FOUND')
        guard = self.recipe_source_guard
        if type(guard) is not BoundRecipeSourceGuard or guard.execution != execution or \
                type(guard.read_authorization) is not DynamoRecipeReadAuthorization or \
                type(guard.history_authorization) is not DynamoPriorHistoryConditions or \
                not guard.history_authorization.matches(execution, guard.read_authorization):
            return ExecutionOutcome(execution_id, 'DENIED', error_code='HISTORY_SCOPE_UNAVAILABLE')
        try:
            observed = await asyncio.to_thread(guard.observe_inputs)
            if type(observed) is not tuple or len(observed) != 2 or \
                    tuple(batch.job_id for batch in observed) != (execution.job_id, execution.prior_job_id):
                raise ValueError('observed execution mismatch')
            evidence, baseline, context = build_jeyuk_inputs(*observed)
            if await asyncio.to_thread(self.registry.execution, execution_id) != execution:
                raise ValueError('execution changed during observation')
        except (ValueError, TypeError, AttributeError, BotoCoreError, ClientError):
            return ExecutionOutcome(execution_id, 'DENIED', error_code='RECOMMENDATION_EVIDENCE_UNVERIFIED')
        return await self.execute_local_recipe(caller, execution_id, evidence=evidence,
                                               baseline=baseline, context=context, ollama=ollama)

    async def execute_observed_combi_recipe(self, caller: Caller, execution_id: str, *,
                                            combi_source_guard: ReceivedCombiReads,
                                            ollama: OllamaClient) -> ExecutionOutcome:
        """Derive every model input from one received, stored-execution-bound lease."""
        execution = await asyncio.to_thread(self.registry.execution, execution_id)
        if not execution or execution.tenant_id != caller.tenant_id:
            return ExecutionOutcome(execution_id, 'DENIED', error_code='EXECUTION_NOT_FOUND')
        if execution.combi_binding is None or type(combi_source_guard) is not ReceivedCombiReads:
            return ExecutionOutcome(execution_id, 'DENIED', error_code='COMBI_SOURCE_SCOPE_UNAVAILABLE')
        try:
            snapshot = await asyncio.to_thread(self.registry.execution_snapshot, execution_id)
            if type(snapshot) is not ExecutionSnapshot or snapshot.state != 'PENDING' or snapshot.execution != execution or \
                    not combi_source_guard.matches_transition(execution, version=snapshot.version, completed_calls=0) or \
                    not await asyncio.to_thread(combi_source_guard.verify, execution):
                raise ValueError('combi execution or history changed')
            observed = await asyncio.to_thread(combi_source_guard.observe_inputs)
            if type(observed) is not tuple or len(observed) != 2 or \
                    tuple(batch.meal_id for batch in observed) != (execution.combi_binding.current_meal_id,
                                                                execution.combi_binding.prior_meal_id):
                raise ValueError('combi observation mismatch')
            evidence, baseline, context = build_combi_inputs(*observed)
            if await asyncio.to_thread(self.registry.execution_snapshot, execution_id) != snapshot:
                raise ValueError('combi execution changed during observation')
            checks = await asyncio.to_thread(combi_source_guard.settlement_checks,
                self.registry._active_tenant_guard(execution.tenant_id, expected_epoch=execution.combi_binding.auth_epoch))
            if any(check['ConditionCheck']['TableName'] == self.registry.table_name for check in checks):
                raise ValueError('source table conflicts with registry')
        except (ValueError, TypeError, AttributeError, KeyError, BotoCoreError, ClientError):
            return ExecutionOutcome(execution_id, 'DENIED', error_code='RECOMMENDATION_EVIDENCE_UNVERIFIED')
        return await self._execute_prepared_recipe(caller, execution_id, evidence=evidence, baseline=baseline,
            context=context, ollama=ollama, combi_source_guard=combi_source_guard)

    def _reserve_fryer_attempt(self, caller, execution, guard, attempt):
        self.registry.begin_request()
        try:
            decision = self.policy.authorize_tonkatsu(caller, execution.execution_id, guard,
                                                      completed_calls=attempt - 1)
            if not decision.allowed:
                return decision
            if decision.fallback_used or decision.model_id != execution.model_id:
                return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")
            if attempt == 1:
                self.registry.reserve(execution, "ollama", tonkatsu_source_guard=guard)
            else:
                self.registry.reserve_local_retry(execution, attempt, tonkatsu_source_guard=guard)
            return decision
        except RegistryConflict:
            return Decision(False, "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED")

    async def execute_observed_tonkatsu_recipe(self, caller: Caller, execution_id: str, *,
                                              tonkatsu_source_guard: ReceivedTonkatsuReads,
                                              ollama: OllamaClient) -> ExecutionOutcome:
        """Run the internal synthetic observation path with per-call source checks."""
        if caller.service != "business-api" or not caller.tenant_id:
            return ExecutionOutcome(execution_id, "DENIED", error_code="CALLER_NOT_AUTHORIZED")
        if type(tonkatsu_source_guard) is not ReceivedTonkatsuReads:
            return ExecutionOutcome(execution_id, "DENIED", error_code="TONKATSU_SOURCE_SCOPE_UNAVAILABLE")
        try:
            execution = await asyncio.to_thread(self.registry.tonkatsu_execution, execution_id,
                tonkatsu_source_guard, completed_calls=0)
            if execution.tenant_id != caller.tenant_id:
                return ExecutionOutcome(execution_id, "DENIED", error_code="EXECUTION_NOT_FOUND")
            current, prior = await asyncio.to_thread(tonkatsu_source_guard.observe_inputs)
            if (current.job_id, prior.job_id) != (execution.job_id, execution.prior_job_id):
                raise ValueError("observed fryer execution mismatch")
            evidence, baseline, context, review_basis = build_tonkatsu_inputs(current, prior)
        except (RegistryConflict, ValueError, TypeError, AttributeError, KeyError, BotoCoreError, ClientError):
            return ExecutionOutcome(execution_id, "DENIED", error_code="RECOMMENDATION_EVIDENCE_UNVERIFIED")
        return await self._execute_prepared_recipe(caller, execution_id, evidence=evidence, baseline=baseline,
            context=context, ollama=ollama, tonkatsu_source_guard=tonkatsu_source_guard, fryer_execution=execution, fryer_review_basis=review_basis)

    async def execute_local_recipe(self, caller: Caller, execution_id: str, *,
                                   evidence: Evidence, baseline: TonkatsuBaseline | JeyukBaseline | CombiBaseline,
                                   context: dict, ollama: OllamaClient) -> ExecutionOutcome:
        """Internal only: caller supplies verified, tenant-scoped server records.

        No HTTP route accepts these arguments. Each provider request is reserved
        independently and only a validated candidate is stored on success.
        Grant-bound internal handoffs also require protected history conditions;
        bare source fixtures are not a registered customer invocation path.
        """
        execution = self.registry.execution(execution_id)
        if (execution and execution.combi_binding is not None) or isinstance(baseline, CombiBaseline):
            return ExecutionOutcome(execution_id, 'DENIED', error_code='COMBI_SOURCE_SCOPE_UNAVAILABLE')
        return await self._execute_prepared_recipe(caller, execution_id, evidence=evidence,
            baseline=baseline, context=context, ollama=ollama)

    async def _execute_prepared_recipe(self, caller, execution_id, *, evidence, baseline, context, ollama,
                                       combi_source_guard=None, tonkatsu_source_guard=None, fryer_execution=None, fryer_review_basis=None):
        execution = fryer_execution if tonkatsu_source_guard is not None else self.registry.execution(execution_id)
        model = self.registry.model(execution.model_id) if execution and tonkatsu_source_guard is None else None
        if not execution or execution.tenant_id != caller.tenant_id:
            return ExecutionOutcome(execution_id, "DENIED", error_code="EXECUTION_NOT_FOUND")
        if tonkatsu_source_guard is None and (execution.feature != Feature.RECIPE_CORRECTION or not model or
                not model.local or model.provider != "ollama" or execution.estimated_cost_usd != 0):
            return ExecutionOutcome(execution_id, "DENIED", error_code="LOCAL_RECIPE_REQUIRED")
        if execution.combi_binding is not None or isinstance(baseline, CombiBaseline):
            if execution.combi_binding is None or type(baseline) is not CombiBaseline or \
                    type(combi_source_guard) is not ReceivedCombiReads or \
                    not await asyncio.to_thread(combi_source_guard.verify, execution):
                return ExecutionOutcome(execution_id, 'DENIED', error_code='COMBI_SOURCE_SCOPE_UNAVAILABLE')
        elif combi_source_guard is not None:
            return ExecutionOutcome(execution_id, 'DENIED', error_code='COMBI_SOURCE_SCOPE_UNAVAILABLE')
        if isinstance(baseline, JeyukBaseline) and self.recipe_source_guard is None:
            return ExecutionOutcome(execution_id, "DENIED", error_code="SOURCE_GUARD_UNAVAILABLE")
        if isinstance(baseline, JeyukBaseline):
            guard = self.recipe_source_guard
            if type(guard) is not BoundRecipeSourceGuard or type(guard.guard) is not DynamoRecipeSourceGuard or \
                    guard.execution != execution:
                return ExecutionOutcome(execution_id, "DENIED", error_code="SOURCE_SCOPE_UNAVAILABLE")
            if guard.read_authorization is not None and \
                    (type(guard.read_authorization) is not DynamoRecipeReadAuthorization or
                     guard.read_authorization.observation.auth_table != getattr(self.registry, 'auth_table', None)):
                return ExecutionOutcome(execution_id, "DENIED", error_code="SOURCE_SCOPE_UNAVAILABLE")
            if guard.read_authorization is not None and (type(guard.history_authorization) is not DynamoPriorHistoryConditions or
                    guard.history_authorization.cooking_table != guard.guard.cooking.name or
                    not guard.history_authorization.matches(execution, guard.read_authorization)):
                return ExecutionOutcome(execution_id, "DENIED", error_code="HISTORY_SCOPE_UNAVAILABLE")
            try:
                checks = await asyncio.to_thread(guard.settlement_checks)
                if any(check['ConditionCheck']['TableName'] == self.registry.table_name for check in checks):
                    raise ValueError('source table conflicts with execution registry')
            except (TypeError, ValueError, KeyError, BotoCoreError, ClientError):
                return ExecutionOutcome(execution_id, "DENIED", error_code="SOURCE_SCOPE_UNAVAILABLE")
        attempts = 0
        reserved = False
        completed_reservations = 0

        async def source_current():
            if tonkatsu_source_guard is not None:
                try:
                    return await asyncio.to_thread(self.registry.verify_tonkatsu_execution, execution,
                        tonkatsu_source_guard, completed_calls=completed_reservations)
                except (RegistryConflict, ValueError, BotoCoreError, ClientError):
                    return False
            if not isinstance(baseline, JeyukBaseline) and combi_source_guard is None:
                return True
            try:
                if await asyncio.to_thread(self.registry.execution, execution_id) != execution:
                    return False
                guard = combi_source_guard if combi_source_guard is not None else self.recipe_source_guard
                if await asyncio.to_thread(guard.verify, execution) is not True:
                    return False
                return await asyncio.to_thread(self.registry.execution, execution_id) == execution
            except Exception:
                return False

        async def finish_recipe(*, succeeded: bool, response_text: str | None = None, error_code: str | None = None) -> str:
            try:
                await asyncio.to_thread(self.registry.finish, execution_id, succeeded=succeeded,
                                        actual_cost_usd=Decimal('0'), response_text=response_text,
                                        error_code=error_code, expected_execution=execution,
                                        recipe_source_guard=self.recipe_source_guard if succeeded and
                                            isinstance(baseline, JeyukBaseline) else None,
                                        **({'combi_source_guard': combi_source_guard} if succeeded and
                                            combi_source_guard is not None else {}),
                                        **({"tonkatsu_source_guard": tonkatsu_source_guard}
                                           if tonkatsu_source_guard is not None else {}))
                return 'settled'
            except RegistryConflict:
                return 'conflict'
            except (BotoCoreError, ClientError):
                # The transaction may have committed before the connection failed.
                return 'unknown'

        async def invoke(messages, schema):
            nonlocal attempts, reserved, completed_reservations
            if not await source_current():
                raise _RecommendationDenied("RECIPE_SOURCE_CHANGED_OR_UNAVAILABLE")
            attempts += 1
            guard_args = {'combi_source_guard': combi_source_guard} if combi_source_guard is not None else {}
            if tonkatsu_source_guard is not None:
                try:
                    decision = await asyncio.to_thread(self._reserve_fryer_attempt, caller, execution,
                                                       tonkatsu_source_guard, attempts)
                except (BotoCoreError, ClientError) as error:
                    raise _ReservationUncertain from error
            elif combi_source_guard is not None:
                decision = (await asyncio.to_thread(self.prepare, caller, execution_id, local_recipe=True, **guard_args)
                    if attempts == 1 else await asyncio.to_thread(self.prepare_local_recipe_retry,
                        caller, execution_id, attempts, **guard_args))
            else:
                decision = (self.prepare(caller, execution_id, local_recipe=True) if attempts == 1 else
                            self.prepare_local_recipe_retry(caller, execution_id, attempts))
            if not decision.allowed:
                raise _RecommendationDenied(decision.code)
            reserved = True
            completed_reservations = attempts
            if (combi_source_guard is not None or tonkatsu_source_guard is not None) and not await source_current():
                raise _RecommendationDenied('RECIPE_SOURCE_CHANGED_OR_UNAVAILABLE')
            return (await ollama.structured_chat(decision.model_id, messages, schema)).text

        try:
            result = await generate_candidate(invoke, evidence=evidence, baseline=baseline, context=context)
            if not reserved:
                return ExecutionOutcome(execution_id, "INDETERMINATE", error_code="EVIDENCE_INSUFFICIENT")
        except _ReservationUncertain:
            return ExecutionOutcome(execution_id, "INDETERMINATE", error_code="RESERVATION_RECONCILIATION_REQUIRED")
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
                    settlement = await finish_recipe(succeeded=False, error_code=code)
                    if settlement != 'settled':
                        return ExecutionOutcome(execution_id, "INDETERMINATE" if settlement == 'unknown' else "FAILED", error_code="SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED")
                    return ExecutionOutcome(execution_id, "FAILED", error_code=code)
                return ExecutionOutcome(execution_id, "DENIED", error_code=code)
            if result["status"] == "NEEDS_REVIEW":
                if fryer_review_basis is not None:
                    result["review_basis"] = fryer_review_basis
                response = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                settlement = await finish_recipe(succeeded=True, response_text=response)
                if settlement != 'settled':
                    # The candidate cannot be published after a settlement conflict.
                    # A second conditional transaction may close the same execution
                    # as failed; it never refunds the consumed model call.
                    conflict_code = "RECIPE_SETTLEMENT_CONFLICT"
                    if settlement == 'conflict':
                        settlement = await finish_recipe(succeeded=False, error_code=conflict_code)
                        if settlement == 'settled':
                            return ExecutionOutcome(execution_id, "FAILED", error_code=conflict_code)
                    return ExecutionOutcome(execution_id, "INDETERMINATE" if settlement == 'unknown' else "FAILED", error_code="SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED")
                return ExecutionOutcome(execution_id, "SUCCEEDED", response_text=response)
            code = result["error_code"]
        if reserved:
            settlement = await finish_recipe(succeeded=False, error_code=code)
            if settlement != 'settled':
                return ExecutionOutcome(execution_id, "INDETERMINATE" if settlement == 'unknown' else "FAILED", error_code="SETTLEMENT_CONFLICT_RECONCILIATION_REQUIRED")
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
