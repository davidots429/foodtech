"""Route one verified stored fryer execution to the local-only Gateway service."""
import asyncio
from datetime import datetime, timezone
import os
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from foodtech_contracts.dynamo_prior_history import DynamoPriorHistoryAuthority
from foodtech_contracts.dynamo_tonkatsu_scope import DynamoTonkatsuReadScope, TonkatsuScopeUnavailable

from foodtech_contracts.recipe_execution_scope import recipe_execution_status_payload
from foodtech_contracts.recipe_result_view import recipe_execution_result_payload
from foodtech_contracts.tonkatsu_result_view import validate_tonkatsu_result_view

from .combi_invocation import CombiRuntimeUnavailable, _close_runtime, configured_recipe_runtime
from .policy import Caller
from .registry import RegistryConflict
from .tonkatsu_transaction_conditions import TonkatsuTransactionConditions
from .service import GatewayService
from .tonkatsu_read_handoff import receive_internal_tonkatsu_handoff, require_tonkatsu_transport


_HEADERS = {"Cache-Control": "no-store"}


def _scope_resolver(role, client, registry):
    account = role.split(":")[4]
    stage = role.removeprefix(f"arn:aws:iam::{account}:role/foodtech-tonkatsu-source-read-")
    history = DynamoPriorHistoryAuthority(auth_table=registry.auth_table,
        cooking_table=f"foodtech-cooking-jobs-{stage}", account_id=account, client=client,
        clock=lambda: int(datetime.now(timezone.utc).timestamp()))
    resolver = DynamoTonkatsuReadScope(execution_table=registry.table_name,
        data_table=f"foodtech-data-flow-{stage}", history=history)
    return resolver, account, stage


def create_tonkatsu_request(*, request_id, owner, epoch, current_job_id, prior_job_id, job_versions):
    """Run the verified ECS read and atomic insert on one worker thread."""
    runtime = None
    try:
        runtime = configured_recipe_runtime("tonkatsu")
        role, _, client, registry = runtime
        resolver, account, stage = _scope_resolver(role, client, registry)
        model_id = os.getenv("FOODTECH_RECIPE_TIER1_MODEL_ID", "").strip()
        if not model_id:
            raise CombiRuntimeUnavailable("tier 1 model unavailable")
        execution_id = str(uuid5(NAMESPACE_URL, f"foodtech:tonkatsu:{stage}:{owner}:{epoch}:{request_id}"))
        scope = resolver.plan_execution(owner=owner, epoch=epoch, execution_id=execution_id,
            current_job_id=current_job_id, prior_job_id=prior_job_id, job_versions=job_versions)
        conditions = TonkatsuTransactionConditions(scope=scope, account=account, stage=stage,
            clock=lambda: int(datetime.now(timezone.utc).timestamp()))
        return registry.create_tonkatsu_execution(conditions, model_id=model_id)
    finally:
        _close_runtime(runtime)


def read_tonkatsu_request(*, execution_id, owner, epoch, job_id, result_view):
    runtime = None
    try:
        runtime = configured_recipe_runtime("tonkatsu")
        role, _, client, registry = runtime
        resolver, _, _ = _scope_resolver(role, client, registry)
        resolver.require_active_account(owner, epoch)
        lookup = {"owner": owner, "epoch": epoch, "job_id": job_id}
        before = registry.tonkatsu_status_snapshot(execution_id, **lookup)
        if before is None:
            return None
        snapshot, binding, result = before
        try:
            current = resolver.plan_execution(owner=owner, epoch=epoch, execution_id=execution_id,
                current_job_id=binding.current.job_id, prior_job_id=binding.prior.job_id,
                job_versions=(binding.current.job_version, binding.prior.job_version))
            source_current = current.binding == binding
        except TonkatsuScopeUnavailable:
            source_current = False
        if registry.tonkatsu_status_snapshot(execution_id, **lookup) != before:
            raise RegistryConflict("fryer status changed during source verification")
        resolver.require_active_account(owner, epoch)
        parameters = {"execution_id": execution_id, "tenant_id": owner, "job_id": job_id}
        if result_view:
            visible = result if source_current or result is None else {**result, "response_text": None}
            payload = recipe_execution_result_payload(snapshot, visible, **parameters)
            if payload["proposal"] is not None and payload["proposal"]["menu"] != "TONKATSU":
                payload.update(projection_status="UNAVAILABLE", proposal=None, error_code="RECIPE_RESULT_UNAVAILABLE")
        else:
            payload = recipe_execution_status_payload(snapshot, result, **parameters)
        return validate_tonkatsu_result_view({**payload, "source_current": source_current},
                                            execution_id=execution_id, job_id=job_id, result_view=result_view)
    finally:
        _close_runtime(runtime)


async def invoke_observed_tonkatsu(request, execution_id, *, ollama):
    require_tonkatsu_transport(request)
    if os.getenv("FOODTECH_TONKATSU_INVOCATION_ENABLED") != "true":
        raise HTTPException(503, "TONKATSU_INVOCATION_UNAVAILABLE", headers=_HEADERS)
    identifier = str(execution_id)
    runtime, guard = None, None
    try:
        pending = asyncio.create_task(asyncio.to_thread(configured_recipe_runtime, "tonkatsu"))
        try:
            runtime = await asyncio.shield(pending)
        except asyncio.CancelledError:
            def discard(task):
                if not task.cancelled() and task.exception() is None:
                    _close_runtime(task.result())

            pending.add_done_callback(discard)
            raise

        role, role_id, client, registry = runtime
        resolver, _, _ = _scope_resolver(role, client, registry)
        guard = await receive_internal_tonkatsu_handoff(request,
            resolve_scope=lambda: resolver.resolve_execution(identifier),
            expected_role=role, expected_role_id=role_id)
        service = GatewayService(registry)
        outcome = await service.execute_observed_tonkatsu_recipe(Caller("business-api", guard.owner),
            identifier, tonkatsu_source_guard=guard, ollama=ollama)
        return JSONResponse({"execution_id": outcome.execution_id, "state": outcome.state,
            "response_text": outcome.response_text, "error_code": outcome.error_code}, headers=_HEADERS)
    except (CombiRuntimeUnavailable, ValueError, BotoCoreError, ClientError):
        raise HTTPException(503, "TONKATSU_INVOCATION_UNAVAILABLE", headers=_HEADERS) from None
    finally:
        if guard is not None:
            guard.close()
        await asyncio.to_thread(_close_runtime, runtime)
