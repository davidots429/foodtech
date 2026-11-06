"""Server-side authorization for the first AI execution boundary.

The gateway reads authoritative execution, model, credential, consent and limit
records. A caller may supply an execution ID, never a model or API key.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class Feature(StrEnum):
    RECIPE_CORRECTION = "RECIPE_CORRECTION"
    GENERAL_GUIDE = "GENERAL_GUIDE"
    DATA_SUMMARY = "DATA_SUMMARY"
    SENSITIVE_LOCAL = "SENSITIVE_LOCAL"


class Tier(StrEnum):
    TIER_1 = "TIER_1"
    TIER_2 = "TIER_2"
    TIER_3 = "TIER_3"


LOCAL_FALLBACK_MODEL = "llama3.1:8b"
ALLOWED_TIERS = {
    Feature.RECIPE_CORRECTION: {Tier.TIER_1},
    Feature.GENERAL_GUIDE: {Tier.TIER_1, Tier.TIER_2, Tier.TIER_3},
    Feature.DATA_SUMMARY: {Tier.TIER_2, Tier.TIER_3},
    Feature.SENSITIVE_LOCAL: {Tier.TIER_2, Tier.TIER_3},
}


@dataclass(frozen=True)
class Caller:
    service: str
    tenant_id: str


@dataclass(frozen=True)
class Execution:
    execution_id: str
    tenant_id: str
    feature: Feature
    model_id: str
    credential_id: str | None
    data_classification: str
    estimated_cost_usd: Decimal
    external_transfer_approved: bool = False
    external_transfer_consent_id: str | None = None
    downgrade_consent_id: str | None = None
    parent_execution_id: str | None = None


@dataclass(frozen=True)
class Model:
    model_id: str
    provider: str
    tier: Tier
    state: str
    local: bool
    regression_passed: bool
    approved_at: datetime | None
    activated_at: datetime | None
    custom: bool = False
    approved_by: str | None = None


@dataclass(frozen=True)
class Credential:
    credential_id: str
    tenant_id: str
    provider: str
    state: str
    expires_at: datetime | None
    tenant_pseudonym: str | None = None
    vault_ref: str | None = None


@dataclass(frozen=True)
class Consent:
    consent_id: str
    execution_id: str
    tenant_id: str
    model_id: str
    kind: str
    notice_version: str
    consented_at: datetime
    parent_execution_id: str | None = None
    quality_warning_acknowledged: bool = False
    transmission_location: str | None = None
    estimated_cost_usd: Decimal | None = None


@dataclass(frozen=True)
class Budget:
    user_remaining_usd: Decimal
    provider_remaining_usd: Decimal
    project_remaining_usd: Decimal
    calls_remaining: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str
    model_id: str | None = None
    fallback_used: bool = False
    requires_new_execution: bool = False


class Registry(Protocol):
    def execution(self, execution_id: str) -> Execution | None: ...
    def model(self, model_id: str) -> Model | None: ...
    def credential(self, credential_id: str) -> Credential | None: ...
    def consent(self, consent_id: str) -> Consent | None: ...
    def budget(self, tenant_id: str, provider: str) -> Budget | None: ...
    def local_available(self, model_id: str) -> bool: ...


def _active(model: Model | None) -> bool:
    return bool(model and model.state == "ACTIVE" and model.regression_passed and
                model.approved_at and model.activated_at and
                (not model.custom or model.approved_by))


def _valid_consent(consent: Consent | None, execution: Execution, kind: str) -> bool:
    expected_id = (execution.downgrade_consent_id if kind == "TIER_DOWNGRADE"
                   else execution.external_transfer_consent_id)
    return bool(consent and consent.consent_id == expected_id and consent.kind == kind and
                consent.execution_id == execution.execution_id and
                consent.tenant_id == execution.tenant_id and consent.model_id == execution.model_id and
                consent.notice_version and consent.consented_at.tzinfo and
                consent.consented_at <= datetime.now(timezone.utc) and
                (kind != "TIER_DOWNGRADE" or (
                    execution.parent_execution_id and consent.parent_execution_id == execution.parent_execution_id and
                    execution.parent_execution_id != execution.execution_id)))


class GatewayPolicy:
    def __init__(self, registry: Registry):
        self.registry = registry

    def authorize(self, caller: Caller, execution_id: str) -> Decision:
        if caller.service != "business-api" or not caller.tenant_id:
            return Decision(False, "CALLER_NOT_AUTHORIZED")
        execution = self.registry.execution(execution_id)
        if not execution or execution.tenant_id != caller.tenant_id or execution.execution_id != execution_id:
            return Decision(False, "EXECUTION_NOT_FOUND")
        if execution.feature not in ALLOWED_TIERS or execution.estimated_cost_usd < 0:
            return Decision(False, "EXECUTION_CONFIGURATION_INVALID")
        model = self.registry.model(execution.model_id)
        if not _active(model):
            return Decision(False, "MODEL_NOT_ACTIVE")
        assert model is not None
        if model.model_id != execution.model_id:
            return Decision(False, "MODEL_CONFIGURATION_INVALID")
        if model.provider not in {"ollama", "openai", "google", "anthropic"}:
            return Decision(False, "MODEL_CONFIGURATION_INVALID")
        if model.local != (model.provider == "ollama"):
            return Decision(False, "MODEL_CONFIGURATION_INVALID")
        if execution.feature == Feature.SENSITIVE_LOCAL and not model.local:
            return Decision(False, "EXTERNAL_TRANSFER_BLOCKED")
        if model.tier not in ALLOWED_TIERS[execution.feature]:
            downgrade = (self.registry.consent(execution.downgrade_consent_id)
                         if execution.downgrade_consent_id else None)
            if execution.feature != Feature.RECIPE_CORRECTION or model.tier not in {Tier.TIER_2, Tier.TIER_3} or \
                    not _valid_consent(
                downgrade, execution, "TIER_DOWNGRADE",
            ) or not downgrade or not downgrade.quality_warning_acknowledged or \
                    downgrade.transmission_location != ("LOCAL" if model.local else "EXTERNAL") or \
                    downgrade.estimated_cost_usd != execution.estimated_cost_usd:
                return Decision(False, "TIER_NOT_ALLOWED", requires_new_execution=True)
        if model.local:
            if execution.credential_id or not self.registry.local_available(model.model_id):
                return Decision(False, "LOCAL_MODEL_UNAVAILABLE")
        else:
            consent = (self.registry.consent(execution.external_transfer_consent_id)
                       if execution.external_transfer_consent_id else None)
            if not _valid_consent(consent, execution, "EXTERNAL_TRANSFER") or not consent or \
                    consent.transmission_location != "EXTERNAL" or \
                    consent.estimated_cost_usd != execution.estimated_cost_usd:
                return Decision(False, "EXTERNAL_TRANSFER_CONSENT_REQUIRED")
            if execution.data_classification not in {"PUBLIC", "SYNTHETIC"}:
                if not execution.external_transfer_approved:
                    return Decision(False, "EXTERNAL_TRANSFER_BLOCKED")
            credential = (self.registry.credential(execution.credential_id)
                          if execution.credential_id else None)
            if not credential or credential.tenant_id != execution.tenant_id or \
                    credential.credential_id != execution.credential_id or \
                    credential.provider != model.provider or credential.state != "ACTIVE" or \
                    (credential.expires_at and (credential.expires_at.tzinfo is None or
                                                credential.expires_at <= datetime.now(timezone.utc))):
                return Decision(False, "CREDENTIAL_NOT_USABLE")
        budget = self.registry.budget(execution.tenant_id, model.provider)
        if not budget or budget.calls_remaining <= 0 or any(
            execution.estimated_cost_usd > remaining for remaining in (
                budget.user_remaining_usd, budget.provider_remaining_usd,
                budget.project_remaining_usd,
            )
        ):
            if execution.feature == Feature.RECIPE_CORRECTION:
                return Decision(False, "LIMIT_ACTION_REQUIRED", requires_new_execution=True)
            fallback = self.registry.model(LOCAL_FALLBACK_MODEL)
            local_budget = self.registry.budget(execution.tenant_id, "ollama")
            if not model.local and _active(fallback) and fallback and \
                    fallback.model_id == LOCAL_FALLBACK_MODEL and fallback.local and \
                    fallback.tier in ALLOWED_TIERS[execution.feature] and \
                    self.registry.local_available(fallback.model_id) and local_budget and \
                    local_budget.calls_remaining > 0:
                return Decision(True, "LOCAL_FALLBACK", fallback.model_id, fallback_used=True)
            return Decision(False, "LIMIT_EXCEEDED")
        return Decision(True, "AUTHORIZED", model.model_id)
