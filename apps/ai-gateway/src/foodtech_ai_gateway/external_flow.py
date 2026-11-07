"""Preview and confirm one synthetic-only external AI execution."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_UP
from uuid import uuid4

from .policy import Consent, Credential, Execution, Feature, Model, Tier, _active
from .registry import DynamoRegistry, RegistryConflict


NOTICE_VERSION = "synthetic-external-v1"
FIXTURE_ID = "synthetic-general-guide-v1"
FIXTURE_PROMPT = (
    "This is a synthetic cooking example with no real person, recipe, device or business data. "
    "Briefly explain why a kitchen operator should check a suggested cooking condition "
    "before applying it. Do not issue device commands."
)
MAX_OUTPUT_TOKENS = 128
QUOTE_LIFETIME = timedelta(minutes=5)
USD_QUANTUM = Decimal("0.000001")


class ExternalFlowError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _usable(model: Model | None, credential: Credential | None, tenant_id: str) -> bool:
    return bool(_active(model) and model and not model.local and
                model.provider in {"openai", "google", "anthropic"} and
                model.tier in {Tier.TIER_1, Tier.TIER_2, Tier.TIER_3} and
                credential and credential.tenant_id == tenant_id and
                credential.provider == model.provider and credential.state == "ACTIVE" and
                (credential.expires_at is None or
                 (credential.expires_at.tzinfo is not None and
                  credential.expires_at > datetime.now(timezone.utc))))


def _ceiling(model: Model) -> Decimal:
    input_rate = model.input_cost_ceiling_usd
    output_rate = model.output_cost_ceiling_usd
    if not model.price_version or input_rate is None or output_rate is None or \
            not input_rate.is_finite() or not output_rate.is_finite() or \
            input_rate < 0 or output_rate < 0 or input_rate + output_rate <= 0:
        raise ExternalFlowError("APPROVED_PRICE_CEILING_REQUIRED")
    # UTF-8 bytes are an upper bound on ordinary text tokens; the extra space
    # covers request framing. The rates are administrator-approved ceilings.
    input_tokens = len(FIXTURE_PROMPT.encode("utf-8")) * 2 + 64
    cost = (input_rate * input_tokens + output_rate * MAX_OUTPUT_TOKENS)
    return max(USD_QUANTUM, cost.quantize(USD_QUANTUM, rounding=ROUND_UP))


class ExternalFlow:
    def __init__(self, registry: DynamoRegistry):
        self.registry = registry

    def preview(self, tenant_id: str, model_id: str, credential_id: str) -> dict:
        self.registry.begin_request()
        model = self.registry.model(model_id)
        credential = self.registry.credential(credential_id)
        if not _usable(model, credential, tenant_id):
            raise ExternalFlowError("MODEL_OR_CREDENTIAL_UNAVAILABLE")
        assert model is not None
        estimate = _ceiling(model)
        budget = self.registry.budget(tenant_id, model.provider)
        if not budget or budget.calls_remaining <= 0 or any(
                estimate > remaining for remaining in (
                    budget.user_remaining_usd, budget.provider_remaining_usd,
                    budget.project_remaining_usd)):
            raise ExternalFlowError("LIMIT_EXCEEDED")
        now = datetime.now(timezone.utc)
        quote_id, execution_id = str(uuid4()), str(uuid4())
        expires_at = now + QUOTE_LIFETIME
        self.registry.create_quote(quote_id, {
            "quote_id": quote_id, "execution_id": execution_id,
            "tenant_id": tenant_id, "model_id": model_id,
            "credential_id": credential_id, "provider": model.provider,
            "fixture_id": FIXTURE_ID, "notice_version": NOTICE_VERSION,
            "price_version": model.price_version, "estimated_cost_usd": estimate,
            "created_at": now.isoformat(), "expires": int(expires_at.timestamp()),
        })
        return {"quote_id": quote_id, "execution_id": execution_id,
                "model_id": model_id, "provider": model.provider,
                "fixture_id": FIXTURE_ID, "notice_version": NOTICE_VERSION,
                "estimated_cost_usd": str(estimate),
                "expires_at": expires_at.isoformat(), "transmission_location": "EXTERNAL",
                "data_classification": "SYNTHETIC"}

    def confirm(self, tenant_id: str, quote_id: str, notice_version: str) -> str:
        self.registry.begin_request()
        quote = self.registry.quote(quote_id)
        if not quote or quote["tenant_id"] != tenant_id or quote["state"] != "OPEN" or \
                quote["notice_version"] != notice_version or \
                quote["fixture_id"] != FIXTURE_ID or \
                quote["expires"] <= int(datetime.now(timezone.utc).timestamp()):
            raise ExternalFlowError("QUOTE_UNAVAILABLE")
        model = self.registry.model(quote["model_id"])
        credential = self.registry.credential(quote["credential_id"])
        if not _usable(model, credential, tenant_id) or not model or \
                model.price_version != quote["price_version"] or \
                _ceiling(model) != quote["estimated_cost_usd"]:
            raise ExternalFlowError("QUOTE_CONFIGURATION_CHANGED")
        budget = self.registry.budget(tenant_id, model.provider)
        if not budget or budget.calls_remaining <= 0 or any(
                quote["estimated_cost_usd"] > remaining for remaining in (
                    budget.user_remaining_usd, budget.provider_remaining_usd,
                    budget.project_remaining_usd)):
            raise ExternalFlowError("LIMIT_EXCEEDED")
        execution = Execution(quote["execution_id"], tenant_id, Feature.GENERAL_GUIDE,
                              model.model_id, credential.credential_id, "SYNTHETIC",
                              quote["estimated_cost_usd"],
                              external_transfer_consent_id=quote_id)
        consent = Consent(quote_id, execution.execution_id, tenant_id, model.model_id,
                          "EXTERNAL_TRANSFER", notice_version, datetime.now(timezone.utc),
                          transmission_location="EXTERNAL",
                          estimated_cost_usd=quote["estimated_cost_usd"])
        try:
            self.registry.confirm_quote(quote, consent, execution)
        except RegistryConflict:
            raise ExternalFlowError("QUOTE_CONFLICT") from None
        return execution.execution_id
