"""Explicit local Tier downgrade notice and single-use consent, without model invocation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from .policy import Caller, Consent, Execution, Feature, GatewayPolicy, Tier, UNCONFIGURED_TIER1, _active
from .registry import RegistryConflict

NOTICE_VERSION = "local-recipe-downgrade-v1"
MODEL_ID = "llama3.1:8b"
PURPOSE = "LOCAL_RECIPE_DOWNGRADE"
BLOCKED_CODES = {"MODEL_NOT_ACTIVE", "MODEL_CONFIGURATION_INVALID", "EXTERNAL_TRANSFER_CONSENT_REQUIRED",
                 "EXTERNAL_TRANSFER_BLOCKED", "CREDENTIAL_NOT_USABLE", "LIMIT_ACTION_REQUIRED"}


class DowngradeError(Exception):
    def __init__(self, code: str):
        self.code = code


class DowngradeFlow:
    def __init__(self, registry):
        self.registry = registry

    def _check(self, tenant: str, parent_id: str):
        parent = self.registry.execution(parent_id)
        first_versions = self.registry.snapshot_versions()
        if not parent or parent.tenant_id != tenant or parent.feature != Feature.RECIPE_CORRECTION or \
                parent.data_classification != "SYNTHETIC" or self.registry.execution_state(parent_id) != "PENDING":
            raise DowngradeError("PARENT_UNAVAILABLE")
        if (parent.job_id is None) != (parent.job_version is None) or \
                (parent.job_id is not None and (not parent.job_id or parent.job_version < 1)):
            raise DowngradeError("PARENT_CONFIGURATION_INVALID")
        if self.registry.snapshot_versions().get(f"EXEC#{parent_id}") != first_versions.get(f"EXEC#{parent_id}"):
            raise DowngradeError("PARENT_CHANGED")
        source = self.registry.model(parent.model_id)
        if (parent.model_id == UNCONFIGURED_TIER1 and source is not None) or \
                (parent.model_id != UNCONFIGURED_TIER1 and (not source or source.tier != Tier.TIER_1)):
            raise DowngradeError("PARENT_NOT_TIER_ONE")
        prior_versions = self.registry.snapshot_versions()
        decision = GatewayPolicy(self.registry).authorize(Caller("business-api", tenant), parent_id)
        current_versions = self.registry.snapshot_versions()
        if any(current_versions.get(key) != version for key, version in prior_versions.items()):
            raise DowngradeError("PARENT_CHANGED")
        if decision.allowed or decision.code not in BLOCKED_CODES:
            raise DowngradeError("PARENT_NOT_BLOCKED")
        model = self.registry.model(MODEL_ID)
        if not _active(model) or not model or not model.local or model.provider != "ollama" or \
                model.tier not in {Tier.TIER_2, Tier.TIER_3} or not self.registry.local_available(MODEL_ID):
            raise DowngradeError("LOCAL_MODEL_UNAVAILABLE")
        budget = self.registry.budget(tenant, "ollama")
        if not budget or budget.calls_remaining <= 0 or any(value < 0 for value in (
                budget.user_remaining_usd, budget.provider_remaining_usd, budget.project_remaining_usd)):
            raise DowngradeError("LOCAL_LIMIT_EXCEEDED")
        return parent

    def preview(self, tenant: str, parent_id: str) -> dict:
        self.registry.begin_request()
        parent = self._check(tenant, parent_id)
        now = datetime.now(timezone.utc)
        quote_id, execution_id = str(uuid4()), str(uuid4())
        expires = now + timedelta(minutes=5)
        self.registry.create_quote(quote_id, {"quote_id": quote_id, "execution_id": execution_id,
            "parent_execution_id": parent_id, "tenant_id": tenant, "purpose": PURPOSE,
            "source_model_id": parent.model_id,
            "job_id": parent.job_id, "job_version": parent.job_version,
            "model_id": MODEL_ID, "notice_version": NOTICE_VERSION, "estimated_cost_usd": Decimal("0"),
            "created_at": now.isoformat(), "expires": int(expires.timestamp())})
        return {"quote_id": quote_id, "parent_execution_id": parent_id, "execution_id": execution_id,
                "job_id": parent.job_id, "job_version": parent.job_version,
                "model_id": MODEL_ID, "notice_version": NOTICE_VERSION, "transmission_location": "LOCAL",
                "estimated_cost_usd": "0", "quality_warning": True,
                "quality_notice": "Tier 1보다 분석·권고 품질이 낮을 수 있습니다. 운영자 검토가 필요합니다.",
                "expires_at": expires.isoformat()}

    def confirm(self, tenant: str, quote_id: str, notice_version: str, acknowledged: bool) -> str:
        self.registry.begin_request()
        quote = self.registry.quote(quote_id)
        if acknowledged is not True or not quote or quote.get("tenant_id") != tenant or quote.get("state") != "OPEN" or \
                quote.get("purpose") != PURPOSE or quote.get("notice_version") != notice_version or \
                notice_version != NOTICE_VERSION or quote.get("model_id") != MODEL_ID or \
                quote.get("estimated_cost_usd") != Decimal("0") or \
                quote.get("expires", 0) <= int(datetime.now(timezone.utc).timestamp()):
            raise DowngradeError("NOTICE_UNAVAILABLE")
        parent = self._check(tenant, quote["parent_execution_id"])
        if (parent.job_id, parent.job_version) != (quote.get("job_id"), quote.get("job_version")) or \
                (quote.get("source_model_id") is not None and parent.model_id != quote["source_model_id"]):
            raise DowngradeError("PARENT_CHANGED")
        execution = Execution(quote["execution_id"], tenant, Feature.RECIPE_CORRECTION, MODEL_ID,
                              None, "SYNTHETIC", Decimal("0"), downgrade_consent_id=quote_id,
                              parent_execution_id=quote["parent_execution_id"],
                              job_id=parent.job_id, job_version=parent.job_version)
        consent = Consent(quote_id, execution.execution_id, tenant, MODEL_ID, "TIER_DOWNGRADE", NOTICE_VERSION,
                          datetime.now(timezone.utc), parent_execution_id=execution.parent_execution_id,
                          quality_warning_acknowledged=True, transmission_location="LOCAL", estimated_cost_usd=Decimal("0"))
        try:
            self.registry.confirm_local_downgrade(quote, consent, execution)
        except RegistryConflict:
            raise DowngradeError("NOTICE_CONFLICT") from None
        return execution.execution_id
