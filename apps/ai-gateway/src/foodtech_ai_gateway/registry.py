"""Durable AI execution records and conditional limit reservations.

The table contains metadata only. Credential secret material belongs to the
separate encrypted credential vault and is never accepted here.
"""

from dataclasses import asdict
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import boto3
from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer

from .policy import Budget, Consent, Credential, Execution, Feature, Model, Tier


def _key(kind: str, identifier: str) -> dict[str, str]:
    if not identifier or len(identifier) > 256:
        raise ValueError("record identifier must contain 1-256 characters")
    return {"pk": f"{kind}#{identifier}", "sk": "META"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class RegistryConflict(Exception):
    """A record changed or a hard limit was exhausted during reservation."""


class DynamoRegistry:
    def __init__(self, table_name: str, period: str, *, resource: Any = None,
                 client: Any = None, local_models: set[str] | None = None):
        if not table_name or len(period) != 7 or period[4] != "-" or not period[:4].isdigit() or \
                not period[5:].isdigit() or not 1 <= int(period[5:]) <= 12:
            raise ValueError("table name and YYYY-MM budget period are required")
        self.table_name = table_name
        self.period = period
        self.resource = resource or boto3.resource("dynamodb")
        self.table = self.resource.Table(table_name)
        # Transactions use the low-level client, so every AttributeValue is
        # serialized exactly once below. The resource client auto-marshals.
        self.client = client or boto3.client("dynamodb")
        self.serializer = TypeSerializer()
        self.local_models = local_models or set()
        self._request_versions: ContextVar[dict[str, int] | None] = ContextVar(
            "ai_registry_read_versions", default=None)

    def begin_request(self) -> None:
        self._request_versions.set({})

    @property
    def _read_versions(self) -> dict[str, int]:
        versions = self._request_versions.get()
        if versions is None:
            versions = {}
            self._request_versions.set(versions)
        return versions

    def _get(self, kind: str, identifier: str) -> dict[str, Any] | None:
        key = _key(kind, identifier)
        item = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        if item:
            self._read_versions[key["pk"]] = int(item["version"])
        return item

    def snapshot_versions(self) -> dict[str, int]:
        return dict(self._read_versions)

    def _put_new(self, kind: str, identifier: str, fields: dict[str, Any]) -> None:
        item = {**_key(kind, identifier), **fields, "version": 1}
        self.table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())

    def create_execution(self, value: Execution) -> None:
        if value.estimated_cost_usd < 0:
            raise ValueError("estimated cost must be nonnegative")
        fields = asdict(value)
        fields["feature"] = value.feature.value
        fields["state"] = "PENDING"
        self._put_new("EXEC", value.execution_id, fields)

    def create_model(self, value: Model) -> None:
        fields = asdict(value)
        fields["tier"] = value.tier.value
        for field in ("approved_at", "activated_at"):
            fields[field] = _iso(fields[field])
        self._put_new("MODEL", value.model_id, fields)

    def create_credential(self, value: Credential, *, vault_ref: str) -> None:
        if not vault_ref or not vault_ref.startswith("vault:"):
            raise ValueError("an encrypted vault reference is required")
        fields = asdict(value)
        fields["expires_at"] = _iso(value.expires_at)
        fields["vault_ref"] = vault_ref
        self._put_new("CREDENTIAL", value.credential_id, fields)

    def create_consent(self, value: Consent) -> None:
        if not value.consented_at.tzinfo:
            raise ValueError("consent time must have a timezone")
        fields = asdict(value)
        fields["consented_at"] = _iso(value.consented_at)
        self._put_new("CONSENT", value.consent_id, fields)

    def create_quote(self, quote_id: str, fields: dict[str, Any]) -> None:
        self._put_new("QUOTE", quote_id, {**fields, "state": "OPEN"})

    def quote(self, quote_id: str) -> dict[str, Any] | None:
        return self._get("QUOTE", quote_id)

    def confirm_quote(self, quote: dict[str, Any], consent: Consent,
                      execution: Execution) -> None:
        """Consume a preview and create its consent and execution atomically."""
        if quote["quote_id"] != consent.consent_id or quote["execution_id"] != execution.execution_id or \
                quote["tenant_id"] != execution.tenant_id or consent.tenant_id != execution.tenant_id or \
                consent.execution_id != execution.execution_id or \
                quote["model_id"] != execution.model_id or quote["credential_id"] != execution.credential_id or \
                quote["estimated_cost_usd"] != execution.estimated_cost_usd:
            raise ValueError("quote and execution do not match")
        quote_key = _key("QUOTE", quote["quote_id"])
        quote_version = self._read_versions.get(quote_key["pk"])
        if quote_version is None:
            raise RegistryConflict("quote was not read consistently")
        consent_fields = asdict(consent)
        consent_fields["consented_at"] = _iso(consent.consented_at)
        execution_fields = asdict(execution)
        execution_fields["feature"] = execution.feature.value
        execution_fields["state"] = "PENDING"
        now = int(datetime.now(timezone.utc).timestamp())
        items = [{"Update": {"TableName": self.table_name, "Key": self._av(quote_key),
                  "UpdateExpression": "SET #s = :used, #v = #v + :one",
                  "ConditionExpression": "#s = :open AND #v = :v AND expires > :now AND tenant_id = :tenant",
                  "ExpressionAttributeNames": {"#s": "state", "#v": "version"},
                  "ExpressionAttributeValues": self._av({":used": "USED", ":open": "OPEN",
                                                         ":one": 1, ":v": quote_version,
                                                         ":now": now, ":tenant": execution.tenant_id})}},
                 {"Put": {"TableName": self.table_name,
                          "Item": self._av({**_key("CONSENT", consent.consent_id),
                                            **consent_fields, "version": 1}),
                          "ConditionExpression": "attribute_not_exists(pk)"}},
                 {"Put": {"TableName": self.table_name,
                          "Item": self._av({**_key("EXEC", execution.execution_id),
                                            **execution_fields, "version": 1}),
                          "ConditionExpression": "attribute_not_exists(pk)"}}]
        for kind, identifier in (("MODEL", execution.model_id),
                                 ("CREDENTIAL", execution.credential_id)):
            items.append(self._condition(kind, identifier))
        try:
            token = str(uuid5(NAMESPACE_URL, f"{self.table_name}:confirm:{quote['quote_id']}"))
            self.client.transact_write_items(TransactItems=items, ClientRequestToken=token)
        except self.client.exceptions.TransactionCanceledException as error:
            raise RegistryConflict("quote expired, changed or already used") from error

    def create_limit(self, scope: str, identifier: str, remaining_usd: Decimal,
                     *, calls_remaining: int | None = None) -> None:
        if scope not in {"TENANT", "PROVIDER", "PROJECT"} or remaining_usd < 0 or \
                (calls_remaining is not None and calls_remaining < 0):
            raise ValueError("invalid limit")
        fields: dict[str, Any] = {"remaining_usd": remaining_usd, "period": self.period}
        if calls_remaining is not None:
            fields["calls_remaining"] = calls_remaining
        self._put_new("LIMIT", f"{self.period}#{scope}#{identifier}", fields)

    def execution(self, execution_id: str) -> Execution | None:
        item = self._get("EXEC", execution_id)
        if not item:
            return None
        fields = {name: item[name] for name in Execution.__dataclass_fields__}
        fields["feature"] = Feature(fields["feature"])
        return Execution(**fields)

    def execution_state(self, execution_id: str) -> str | None:
        item = self._get("EXEC", execution_id)
        return item["state"] if item else None

    def execution_result(self, execution_id: str) -> dict[str, Any] | None:
        item = self.table.get_item(
            Key={"pk": _key("EXEC", execution_id)["pk"], "sk": "RESULT"},
            ConsistentRead=True,
        ).get("Item")
        if item and item.get("expires", 0) <= int(datetime.now(timezone.utc).timestamp()):
            return None
        return item

    def model(self, model_id: str) -> Model | None:
        item = self._get("MODEL", model_id)
        if not item:
            return None
        fields = {name: item.get(name) for name in Model.__dataclass_fields__}
        fields["tier"] = Tier(fields["tier"])
        fields["approved_at"] = _time(fields["approved_at"])
        fields["activated_at"] = _time(fields["activated_at"])
        return Model(**fields)

    def credential(self, credential_id: str) -> Credential | None:
        item = self._get("CREDENTIAL", credential_id)
        if not item:
            return None
        fields = {name: item.get(name) for name in Credential.__dataclass_fields__}
        fields["expires_at"] = _time(fields["expires_at"])
        return Credential(**fields)

    def consent(self, consent_id: str) -> Consent | None:
        item = self._get("CONSENT", consent_id)
        if not item:
            return None
        fields = {name: item[name] for name in Consent.__dataclass_fields__}
        fields["consented_at"] = _time(fields["consented_at"])
        return Consent(**fields)

    def _limit_id(self, scope: str, identifier: str) -> str:
        return f"{self.period}#{scope}#{identifier}"

    def _limit(self, scope: str, identifier: str) -> dict[str, Any] | None:
        return self._get("LIMIT", self._limit_id(scope, identifier))

    def budget(self, tenant_id: str, provider: str) -> Budget | None:
        tenant = self._limit("TENANT", tenant_id)
        provider_limit = self._limit("PROVIDER", provider)
        project = self._limit("PROJECT", "GLOBAL")
        if not tenant or not provider_limit or not project:
            return None
        return Budget(tenant["remaining_usd"], provider_limit["remaining_usd"],
                      project["remaining_usd"], int(tenant["calls_remaining"]))

    def local_available(self, model_id: str) -> bool:
        return model_id in self.local_models

    def _av(self, values: dict[str, Any]) -> dict[str, Any]:
        return {name: self.serializer.serialize(value) for name, value in values.items()}

    def _condition(self, kind: str, identifier: str) -> dict[str, Any]:
        key = _key(kind, identifier)
        version = self._read_versions.get(key["pk"])
        if version is None:
            raise RegistryConflict("record was not read consistently before reservation")
        return {"ConditionCheck": {"TableName": self.table_name, "Key": self._av(key),
                "ConditionExpression": "#v = :v", "ExpressionAttributeNames": {"#v": "version"},
                "ExpressionAttributeValues": self._av({":v": version})}}

    def reserve(self, execution: Execution, provider: str) -> None:
        """Claim one execution and all three limits in one DynamoDB transaction."""
        cost = execution.estimated_cost_usd
        key = _key("EXEC", execution.execution_id)
        version = self._read_versions.get(key["pk"])
        if version is None:
            raise RegistryConflict("execution was not read consistently")
        items = [{"Update": {"TableName": self.table_name, "Key": self._av(key),
                  "UpdateExpression": "SET #s = :reserved, #v = #v + :one, reserved_provider = :provider",
                  "ConditionExpression": "#s = :pending AND #v = :v AND tenant_id = :tenant",
                  "ExpressionAttributeNames": {"#s": "state", "#v": "version"},
                  "ExpressionAttributeValues": self._av({":reserved": "RESERVED", ":pending": "PENDING",
                                                         ":one": 1, ":v": version,
                                                         ":tenant": execution.tenant_id,
                                                         ":provider": provider})}}]
        for scope, identifier in (("TENANT", execution.tenant_id),
                                  ("PROVIDER", provider), ("PROJECT", "GLOBAL")):
            limit_id = self._limit_id(scope, identifier)
            limit_key = _key("LIMIT", limit_id)
            limit_version = self._read_versions.get(limit_key["pk"])
            if limit_version is None:
                raise RegistryConflict("limits were not read consistently")
            condition = "#v = :v AND remaining_usd >= :cost"
            update = "SET remaining_usd = remaining_usd - :cost, #v = #v + :one"
            values: dict[str, Any] = {":v": limit_version, ":cost": cost, ":one": 1}
            if scope == "TENANT":
                condition += " AND calls_remaining >= :one"
                update += ", calls_remaining = calls_remaining - :one"
            items.append({"Update": {"TableName": self.table_name, "Key": self._av(limit_key),
                         "UpdateExpression": update, "ConditionExpression": condition,
                         "ExpressionAttributeNames": {"#v": "version"},
                         "ExpressionAttributeValues": self._av(values)}})
        checked: set[str] = set()
        for kind, identifier in (("MODEL", execution.model_id),
                                 ("CREDENTIAL", execution.credential_id),
                                 ("CONSENT", execution.external_transfer_consent_id),
                                 ("CONSENT", execution.downgrade_consent_id)):
            key = _key(kind, identifier)["pk"] if identifier else ""
            if key in self._read_versions and key not in checked:
                items.append(self._condition(kind, identifier))
                checked.add(key)
        try:
            token = str(uuid5(NAMESPACE_URL, f"{self.table_name}:reserve:{execution.execution_id}"))
            self.client.transact_write_items(TransactItems=items, ClientRequestToken=token)
        except self.client.exceptions.TransactionCanceledException as error:
            raise RegistryConflict("execution or authorization record changed, or limit exhausted") from error

    def finish(self, execution_id: str, *, succeeded: bool, actual_cost_usd: Decimal,
               response_text: str | None = None, error_code: str | None = None) -> None:
        """Persist result, settle cost and terminal state in one transaction."""
        if succeeded != (response_text is not None) or (not succeeded and not error_code):
            raise ValueError("success requires a response; failure requires an error code")
        if response_text is not None and len(response_text.encode("utf-8")) > 16384:
            raise ValueError("model response exceeds the first-flow limit")
        item = self._get("EXEC", execution_id)
        if not item or item["state"] != "RESERVED" or actual_cost_usd < 0:
            raise RegistryConflict("execution is not reserved or actual cost is invalid")
        provider = item["reserved_provider"]
        tenant_id = item["tenant_id"]
        if self.budget(tenant_id, provider) is None:
            raise RegistryConflict("limit record is missing")
        delta = actual_cost_usd - item["estimated_cost_usd"]
        overage = max(delta, Decimal("0"))
        settlement_status = "REVIEW_REQUIRED" if overage else "SETTLED"
        execution_key = _key("EXEC", execution_id)
        changes = [{"Update": {"TableName": self.table_name, "Key": self._av(execution_key),
                    "UpdateExpression": "SET #s = :final, #v = #v + :one, actual_cost_usd = :actual, settlement_status = :settlement",
                    "ConditionExpression": "#s = :reserved AND #v = :v",
                    "ExpressionAttributeNames": {"#s": "state", "#v": "version"},
                    "ExpressionAttributeValues": self._av({":final": "SUCCEEDED" if succeeded else "FAILED",
                                                           ":reserved": "RESERVED", ":one": 1,
                                                           ":actual": actual_cost_usd,
                                                           ":settlement": settlement_status,
                                                           ":v": self._read_versions[execution_key["pk"]]})}}]
        completed_at = datetime.now(timezone.utc)
        result = {"pk": execution_key["pk"], "sk": "RESULT",
                  "state": "SUCCEEDED" if succeeded else "FAILED",
                  "model_id": item["model_id"], "provider": provider,
                  "actual_cost_usd": actual_cost_usd,
                  "settlement_status": settlement_status,
                  "unreconciled_usd": overage,
                  "completed_at": completed_at.isoformat(),
                  "expires": int((completed_at + timedelta(days=90)).timestamp()),
                  "response_text": response_text, "error_code": error_code}
        changes.append({"Put": {"TableName": self.table_name, "Item": self._av(result),
                                "ConditionExpression": "attribute_not_exists(pk)"}})
        for scope, identifier in (("TENANT", tenant_id), ("PROVIDER", provider),
                                  ("PROJECT", "GLOBAL")):
            key = _key("LIMIT", self._limit_id(scope, identifier))
            version = self._read_versions[key["pk"]]
            condition = "#v = :v"
            values: dict[str, Any] = {":one": 1, ":v": version}
            if overage:
                values[":zero"] = Decimal("0")
                update = "SET remaining_usd = :zero, #v = #v + :one"
                if scope == "TENANT":
                    update += ", calls_remaining = :zero"
            elif delta < 0:
                values[":refund"] = -delta
                update = "SET remaining_usd = remaining_usd + :refund, #v = #v + :one"
            else:
                update = "SET #v = #v + :one"
            changes.append({"Update": {"TableName": self.table_name, "Key": self._av(key),
                            "UpdateExpression": update,
                            "ConditionExpression": condition,
                            "ExpressionAttributeNames": {"#v": "version"},
                            "ExpressionAttributeValues": self._av(values)}})
        try:
            token = str(uuid5(NAMESPACE_URL, f"{self.table_name}:finish:{execution_id}"))
            self.client.transact_write_items(TransactItems=changes, ClientRequestToken=token)
        except self.client.exceptions.TransactionCanceledException as error:
            raise RegistryConflict("execution or limit changed during settlement") from error
