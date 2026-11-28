"""Customer request path through the mutually authenticated AI Gateway."""

import os
import ssl
import json
import time
from uuid import UUID
from typing import Literal
from urllib.parse import urlparse

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .auth import _csrf, _session
from .recipe_candidate_jobs import CandidateJobRequest
from foodtech_contracts.recipe_execution_scope import validate_recipe_status_payload
from foodtech_contracts.recipe_result_view import recipe_proposal_digest, validate_recipe_result_payload
from foodtech_contracts.tonkatsu_result_view import parse_fryer_review_scope, validate_tonkatsu_result_view
from foodtech_contracts.tonkatsu_transaction_conditions import TonkatsuTransactionConditions


router = APIRouter(prefix="/api/customer/ai", tags=["ai"])


class GuidanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def ordered_turns(self) -> "ChatRequest":
        if len(self.messages) % 2 != 1 or any(
            message.role != ("user" if index % 2 == 0 else "assistant")
            or not message.content.strip()
            for index, message in enumerate(self.messages)
        ):
            raise ValueError("chat messages must alternate and end with a user question")
        return self


class CookingChatRequest(ChatRequest):
    job_id: UUID


class ExternalPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128)
    credential_id: str = Field(min_length=1, max_length=128)


class ExternalConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notice_version: str = Field(min_length=1, max_length=64)


class RecipeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    job_version: int = Field(strict=True, ge=1)
    comparison_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prior_job_id: UUID | None = None
    prior_job_version: int | None = Field(default=None, strict=True, ge=1)
    jeyuk_source_revision: int | None = Field(default=None, strict=True, ge=1, le=10**18)
    prior_source_revision: int | None = Field(default=None, strict=True, ge=1, le=10**18)

    @model_validator(mode="after")
    def validate_prior(self):
        if (self.prior_job_id is None) != (self.prior_job_version is None) or self.prior_job_id == self.job_id or \
                (self.prior_source_revision is not None and self.prior_job_id is None):
            raise ValueError("invalid prior job binding")
        return self


class TonkatsuJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    job_id: UUID
    job_version: int = Field(strict=True, ge=1, le=10**18)
    prior_job_id: UUID
    prior_job_version: int = Field(strict=True, ge=1, le=10**18)


class RecipeConfirmRequest(RecipeJobRequest):
    notice_version: str = Field(min_length=1, max_length=64)
    quality_warning_acknowledged: StrictBool


async def _recipe_job(request: Request, body: RecipeJobRequest):
    from .cooking import _read
    from .recommendation_evidence import read_evidence
    session, _, _, job, _ = await _read(request, str(body.job_id), write=True)
    evidence = await read_evidence(request, str(body.job_id), comparison_id=body.comparison_id,
                                   prior_job_id=str(body.prior_job_id) if body.prior_job_id else None)
    if job["version"] != body.job_version or evidence["job_version"] != body.job_version:
        raise HTTPException(409, "조리 기록이 변경되었습니다. 근거를 다시 조회해 주세요.")
    if body.prior_job_id and (not evidence.get("selected_prior_job") or
            evidence["selected_prior_job"].get("job_version") != body.prior_job_version):
        raise HTTPException(409, "이전 작업이 변경되었습니다. 근거를 다시 조회해 주세요.")
    current_source = evidence.get("jeyuk_collection") or {}
    prior = evidence.get("selected_prior_job") or {}
    prior_source = prior.get("collection") or {}
    if body.jeyuk_source_revision is not None and (
            current_source.get("status") != "MATCHED_AT_READ" or
            current_source.get("source_revision") != body.jeyuk_source_revision):
        raise HTTPException(409, "현재 작업의 원천 기록이 변경되었습니다. 근거를 다시 조회해 주세요.")
    if body.prior_source_revision is not None and (
            prior_source.get("status") != "MATCHED_AT_READ" or
            prior_source.get("source_revision") != body.prior_source_revision):
        raise HTTPException(409, "이전 작업의 원천 기록이 변경되었습니다. 근거를 다시 조회해 주세요.")
    if evidence["recorded"].get("provenance") != "SYNTHETIC" or \
            evidence["recorded"].get("menu") not in {"TONKATSU", "JEYUK"}:
        raise HTTPException(409, "합성 조리 기록만 요청할 수 있습니다.")
    if evidence["completeness"] == "INSUFFICIENT":
        raise HTTPException(409, "필수 조리 근거가 부족합니다.")
    if evidence.get("process_stale") or evidence.get("source_revision_status") in {"CHANGED", "INCONSISTENT"}:
        raise HTTPException(409, "원천 자료가 변경되었거나 연결이 맞지 않습니다. 공정 근거를 다시 확인해 주세요.")
    return session


def _recipe_binding(body: RecipeJobRequest) -> dict:
    result = {"job_id": str(body.job_id), "job_version": body.job_version}
    if body.comparison_id is not None:
        result["comparison_id"] = body.comparison_id
    if body.prior_job_id is not None:
        result["prior_job_id"] = str(body.prior_job_id)
        result["prior_job_version"] = body.prior_job_version
    if body.jeyuk_source_revision is not None:
        result["jeyuk_source_revision"] = body.jeyuk_source_revision
    if body.prior_source_revision is not None:
        result["prior_source_revision"] = body.prior_source_revision
    return result


async def _recipe_gateway(path: str, body: dict) -> dict:
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.post(f"{url}{path}", json=body)
            if result.status_code == 409:
                raise HTTPException(409, "요청 또는 전환 고지를 다시 확인해 주세요.")
            result.raise_for_status()
            return result.json()
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "AI 권고 요청을 처리하지 못했습니다.") from None


@router.post("/tonkatsu-recipe-requests", status_code=201)
async def tonkatsu_recipe_request(request: Request, body: TonkatsuJobRequest):
    from .cooking import _read

    if body.job_id == body.prior_job_id:
        raise HTTPException(422, "서로 다른 두 조리작업이 필요합니다.")
    session, _, _, current, _ = await _read(request, str(body.job_id), write=True)
    prior_session, _, _, prior, _ = await _read(request, str(body.prior_job_id), write=True)
    if (session.subject, session.epoch) != (prior_session.subject, prior_session.epoch) or any(
        job.get("version") != version or job.get("menu") != "TONKATSU" or job.get("provenance") != "SYNTHETIC"
        or job.get("equipment_profile") != "synthetic-fryer-approved-v2"
        or job.get("status") not in {"COMPLETED", "FAILED"}
        for job, version in ((current, body.job_version), (prior, body.prior_job_version))
    ):
        raise HTTPException(409, "조리 기록이 변경되었거나 권고에 사용할 수 없습니다.")
    response = await _recipe_gateway("/internal/ai/tonkatsu-recipe-requests", {
        **body.model_dump(mode="json"), "tenant_id": session.subject, "auth_epoch": session.epoch,
    })
    if type(response) is not dict or response.get("state") != "PENDING":
        raise HTTPException(502, "돈까스 권고 요청을 처리하지 못했습니다.")
    try:
        execution_id = str(UUID(response.get("execution_id")))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(502, "돈까스 권고 요청을 처리하지 못했습니다.") from None
    return {"execution_id": execution_id, "state": "PENDING"}


@router.post("/recipe-requests", status_code=201)
async def recipe_request(request: Request, body: RecipeJobRequest) -> dict:
    session = await _recipe_job(request, body)
    return await _recipe_gateway("/internal/ai/recipe-requests", {
        "tenant_id": session.subject, **_recipe_binding(body)})


@router.post("/recipe-requests/{parent_id}/local-preview", status_code=201)
async def recipe_local_preview(request: Request, parent_id: UUID, body: RecipeJobRequest) -> dict:
    session = await _recipe_job(request, body)
    return await _recipe_gateway("/internal/ai/recipe-downgrades/preview", {
        "tenant_id": session.subject, "parent_execution_id": str(parent_id),
        **_recipe_binding(body)})


@router.post("/recipe-quotes/{quote_id}/confirm", status_code=201)
async def recipe_local_confirm(request: Request, quote_id: UUID, body: RecipeConfirmRequest) -> dict:
    session = await _recipe_job(request, body)
    result = await _recipe_gateway(f"/internal/ai/recipe-downgrades/{quote_id}/confirm", {
        "tenant_id": session.subject, "notice_version": body.notice_version,
        "quality_warning_acknowledged": body.quality_warning_acknowledged,
        **_recipe_binding(body)})
    return {**result, "execution_available": False}


@router.get('/recipe-executions/{execution_id}/status')
async def recipe_execution_status(request: Request, execution_id: UUID, job_id: UUID):
    return await _read_recipe_record(request, execution_id, job_id, result_view=False)


@router.get('/recipe-executions/{execution_id}/result')
async def recipe_execution_result(request: Request, execution_id: UUID, job_id: UUID):
    return await _read_recipe_record(request, execution_id, job_id, result_view=True)


@router.get("/tonkatsu-recipe-executions/{execution_id}/status")
async def tonkatsu_execution_status(request: Request, execution_id: UUID, job_id: UUID):
    payload, *_ = await _recipe_record_data(request, execution_id, job_id, result_view=False, tonkatsu=True)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/tonkatsu-recipe-executions/{execution_id}/result")
async def tonkatsu_execution_result(request: Request, execution_id: UUID, job_id: UUID):
    payload, *_ = await _recipe_record_data(request, execution_id, job_id, result_view=True, tonkatsu=True)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def _read_recipe_record(request: Request, execution_id: UUID, job_id: UUID, *, result_view: bool):
    payload, *_ = await _recipe_record_data(request, execution_id, job_id, result_view=result_view)
    return JSONResponse(payload, headers={'Cache-Control': 'no-store'})


async def _recipe_record_data(request: Request, execution_id: UUID, job_id: UUID, *, result_view: bool,
                              tonkatsu: bool = False, include_review_scope: bool = False):
    from .cooking import _read
    before, _, _, before_job, _ = await _read(request, str(job_id))
    identity = (before.subject, getattr(before, 'epoch', None), before.csrf)
    payload_request = {'tenant_id': identity[0], 'job_id': str(job_id)}
    resource = 'recipe-results' if result_view else 'recipe-status'
    if tonkatsu:
        before_job_version = before_job["version"]
        if (before_job.get("menu") != "TONKATSU" or before_job.get("provenance") != "SYNTHETIC"
                or before_job.get("equipment_profile") != "synthetic-fryer-approved-v2"):
            raise HTTPException(404, "돈까스 조리작업을 찾을 수 없습니다.", headers={"Cache-Control": "no-store"})
        payload_request["auth_epoch"] = identity[1]
        resource = "tonkatsu-recipe-review-context" if include_review_scope else "tonkatsu-" + resource
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.post(f'{url}/internal/ai/{resource}/{execution_id}', json=payload_request)
            if result.status_code == 404:
                raise HTTPException(404, '해당 조리작업의 권고 실행 기록을 찾을 수 없습니다.', headers={'Cache-Control': 'no-store'})
            if result.status_code == 409:
                raise HTTPException(409, '실행 상태가 변경되었습니다. 다시 조회해 주세요.', headers={'Cache-Control': 'no-store'})
            result.raise_for_status()
            if tonkatsu:
                wire = result.json()
                sidecar = wire.pop("review_scope", None) if include_review_scope and type(wire) is dict else None
                payload = validate_tonkatsu_result_view(wire, execution_id=str(execution_id),
                                                       job_id=str(job_id), result_view=result_view)
                if include_review_scope and payload["projection_status"] == "AVAILABLE":
                    scope = parse_fryer_review_scope(sidecar)
                    if (
                        scope.execution_id != str(execution_id) or scope.binding.current.job_id != str(job_id)
                        or scope.binding.current.job_version != payload["job_version"]
                        or scope.history.history.owner != identity[0] or scope.binding.auth_epoch != identity[1]
                    ):
                        raise ValueError("invalid stored fryer review binding")
                    payload["fryer_conditions"] = TonkatsuTransactionConditions(scope=scope,
                        account=sidecar["account"], stage=sidecar["stage"], clock=time.time)
            else:
                validate = validate_recipe_result_payload if result_view else validate_recipe_status_payload
                payload = validate(result.json(), execution_id=str(execution_id), job_id=str(job_id))
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OSError):
        raise HTTPException(502, '권고 실행 상태를 읽지 못했습니다.', headers={'Cache-Control': 'no-store'}) from None
    after, store, pk, job, _ = await _read(request, str(job_id))
    if identity != (after.subject, getattr(after, 'epoch', None), after.csrf):
        raise HTTPException(401, '로그인 상태가 변경되었습니다. 다시 로그인해 주세요.', headers={'Cache-Control': 'no-store'})
    if tonkatsu and before_job_version != job["version"]:
        raise HTTPException(409, "조리작업이 변경되었습니다. 다시 조회해 주세요.", headers={"Cache-Control": "no-store"})
    if tonkatsu and payload["source_current"] and payload["job_version"] != job["version"]:
        raise HTTPException(409, "권고 저장 범위가 변경되었습니다. 다시 조회해 주세요.", headers={"Cache-Control": "no-store"})
    if result_view and payload.get('proposal') is not None and payload['proposal']['menu'] != job.get('menu'):
        raise HTTPException(502, '권고 결과의 메뉴가 작업과 일치하지 않습니다.', headers={'Cache-Control': 'no-store'})
    # Configuration visibility is not execution authorization; POST rechecks all scopes.
    manual_enabled = job.get('menu') == 'JEYUK' and all(os.getenv(name) == 'true' for name in (
        'FOODTECH_RECIPE_INVOCATION_ENABLED', 'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED',
        'FOODTECH_RECIPE_SOURCE_BROKER_ENABLED'))
    return ({**payload, 'job_changed': payload['job_version'] != job['version'],
             'manual_invocation_enabled': manual_enabled}, after, store, pk, job)


class RecipeReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job_id: UUID
    expected_job_version: int = Field(strict=True, ge=1, le=10**18)
    expected_review_version: int = Field(strict=True, ge=0, lt=2**63 - 2)
    request_id: UUID
    proposal_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    decision: Literal['APPROVE', 'REJECT']
    reason: str = Field(min_length=1, max_length=1000)
    acknowledged: StrictBool

    @model_validator(mode='after')
    def explicit_review(self):
        if not self.acknowledged or not self.reason.strip() or self.reason != self.reason.strip():
            raise ValueError('explicit review acknowledgement and trimmed reason required')
        return self


async def _review_session(request, job_id, before, *, identity=None):
    from .cooking import _read
    expected = identity if identity is not None else (before.subject, before.epoch, before.csrf)
    after, _, _, job, _ = await _read(request, str(job_id))
    if expected != (after.subject, after.epoch, after.csrf):
        raise HTTPException(401, '로그인 상태가 변경되었습니다. 다시 로그인해 주세요.', headers={'Cache-Control': 'no-store'})
    return job


@router.get('/recipe-executions/{execution_id}/review')
async def recipe_review_context(request: Request, execution_id: UUID, job_id: UUID):
    return await _recipe_review_context(request, execution_id, job_id, tonkatsu=False)


@router.get("/tonkatsu-recipe-executions/{execution_id}/review")
async def tonkatsu_review_context(request: Request, execution_id: UUID, job_id: UUID):
    return await _recipe_review_context(request, execution_id, job_id, tonkatsu=True)


async def _recipe_review_context(request, execution_id, job_id, *, tonkatsu):
    from .cooking import _store_call
    from .recipe_review_store import RecipeReviewStore
    payload, session, store, pk, _ = await _recipe_record_data(
        request, execution_id, job_id, result_view=True, **({"tonkatsu": True} if tonkatsu else {}),
    )
    if payload['projection_status'] != 'AVAILABLE':
        raise HTTPException(409, '검토할 저장 권고가 없습니다.', headers={'Cache-Control': 'no-store'})
    try:
        reviews = RecipeReviewStore(store)
        current = await _store_call(store, reviews.current, pk)
    except ValueError:
        raise HTTPException(503, '검토 기록을 확인하지 못했습니다.', headers={'Cache-Control': 'no-store'}) from None
    job = await _review_session(request, job_id, session)
    digest = recipe_proposal_digest(payload['proposal'])
    return JSONResponse({'execution_id': str(execution_id), 'job_id': str(job_id),
        'job_version': payload['job_version'], 'proposal_digest': digest, 'review_version': current['version'],
        'proposal': payload['proposal'],
        'can_review': payload['job_version'] == job['version'],
        'latest_review': current['review']}, headers={'Cache-Control': 'no-store'})


@router.post('/recipe-executions/{execution_id}/review', status_code=201)
async def recipe_review(request: Request, execution_id: UUID, body: RecipeReviewRequest):
    return await _save_recipe_review(request, execution_id, body, tonkatsu=False)


@router.post("/tonkatsu-recipe-executions/{execution_id}/review", status_code=201)
async def tonkatsu_review(request: Request, execution_id: UUID, body: RecipeReviewRequest):
    return await _save_recipe_review(request, execution_id, body, tonkatsu=True)


async def _save_recipe_review(request, execution_id, body, *, tonkatsu):
    from .cooking import _now, _read, _store_call
    from .cooking_store import Conflict
    from .recipe_review_store import RecipeReviewStore
    before, _, _, _, _ = await _read(request, str(body.job_id), write=True)
    identity = (before.subject, before.epoch, before.csrf)
    lookup = {"tonkatsu": True, "include_review_scope": True} if tonkatsu else {}
    payload, session, store, pk, job = await _recipe_record_data(request, execution_id, body.job_id, result_view=True, **lookup)
    if identity != (session.subject, session.epoch, session.csrf):
        raise HTTPException(401, '로그인 상태가 변경되었습니다. 다시 로그인해 주세요.', headers={'Cache-Control': 'no-store'})
    if payload['projection_status'] != 'AVAILABLE' or payload['job_version'] != body.expected_job_version or \
            job['version'] != body.expected_job_version or recipe_proposal_digest(payload['proposal']) != body.proposal_digest:
        raise HTTPException(409, '권고 또는 작업이 변경되었습니다. 다시 조회해 주세요.', headers={'Cache-Control': 'no-store'})
    value = {'schema_version': 'recipe-review-v1', 'version': body.expected_review_version + 1,
        'request_id': str(body.request_id), 'execution_id': str(execution_id), 'job_id': str(body.job_id),
        'job_version': body.expected_job_version, 'proposal_digest': body.proposal_digest,
        'proposal': payload['proposal'], 'decision': body.decision, 'reason': body.reason, 'reviewed_at': _now()}
    try:
        reviews = RecipeReviewStore(store)
        if tonkatsu and "fryer_conditions" not in payload:
            raise Conflict
        saved = await _store_call(store, reviews.save, pk, body.expected_job_version,
                                  body.expected_review_version, value, session.epoch,
                                  **({"fryer_conditions": payload["fryer_conditions"]} if tonkatsu else {}))
    except Conflict:
        raise HTTPException(409, '검토 또는 작업 상태가 변경되었습니다. 검토 이력을 확인해 주세요.', headers={'Cache-Control': 'no-store'}) from None
    except (ValueError, BotoCoreError, ClientError):
        raise HTTPException(503, '검토 저장 여부를 확인하지 못했습니다. 검토 이력을 확인해 주세요.',
                            headers={'Cache-Control': 'no-store'}) from None
    current_job = await _review_session(request, body.job_id, session, identity=identity)
    return JSONResponse({'review': saved, 'job_changed': current_job['version'] != saved['job_version']},
                        status_code=201, headers={'Cache-Control': 'no-store'})


@router.get('/recipe-reviews')
async def recipe_review_history(request: Request, job_id: UUID, limit: int = Query(20, ge=1, le=50),
                                cursor: int | None = Query(None, ge=1, lt=2**63 - 1)):
    from .cooking import _read, _store_call
    from .recipe_review_store import RecipeReviewStore
    session, store, pk, job, _ = await _read(request, str(job_id))
    try:
        reviews = RecipeReviewStore(store)
        page = await _store_call(store, reviews.history, pk, limit, cursor)
    except ValueError:
        raise HTTPException(503, '검토 이력을 확인하지 못했습니다.', headers={'Cache-Control': 'no-store'}) from None
    current_job = await _review_session(request, job_id, session)
    return JSONResponse({**page, 'job_id': str(job_id), 'current_job_version': current_job['version']},
                        headers={'Cache-Control': 'no-store'})


@router.get('/recipe-candidates/{review_version}')
async def recipe_candidate(request: Request, review_version: int, job_id: UUID):
    from .cooking import _read, _store_call
    from .recipe_review_store import RecipeReviewStore
    if not 1 <= review_version < 2**63 - 1:
        raise HTTPException(422, '후보 버전이 올바르지 않습니다.', headers={'Cache-Control': 'no-store'})
    session, store, pk, _, _ = await _read(request, str(job_id))
    try:
        reviews = RecipeReviewStore(store)
        candidate = await _store_call(store, reviews.candidate, pk, review_version)
        current = await _store_call(store, reviews.current, pk)
    except ValueError:
        raise HTTPException(503, '후보 버전을 확인하지 못했습니다.', headers={'Cache-Control': 'no-store'}) from None
    job = await _review_session(request, job_id, session)
    if candidate is None:
        raise HTTPException(404, '이 검토에 저장된 후보 버전이 없습니다.', headers={'Cache-Control': 'no-store'})
    return JSONResponse({'candidate': candidate, 'current_job_version': job['version'],
        'current_review_version': current['version']}, headers={'Cache-Control': 'no-store'})


@router.post('/recipe-candidates/{review_version}/cooking-jobs', status_code=201)
async def candidate_cooking_job(request: Request, review_version: int, body: CandidateJobRequest, job_id: UUID):
    from .recipe_candidate_jobs import create_candidate_job
    return await create_candidate_job(request, job_id, review_version, body)


@router.post('/recipe-executions/{execution_id}/invoke')
async def invoke_recipe_execution(request: Request, execution_id: UUID):
    from .recipe_invocation import invoke_customer_recipe
    return await invoke_customer_recipe(request, execution_id)


def _gateway_settings() -> tuple[str, str, str, str]:
    url = os.getenv("FOODTECH_GATEWAY_URL", "")
    ca = os.getenv("FOODTECH_GATEWAY_CA", "")
    cert = os.getenv("FOODTECH_BUSINESS_CLIENT_CERT", "")
    key = os.getenv("FOODTECH_BUSINESS_CLIENT_KEY", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"127.0.0.1", "localhost"} or \
            parsed.path not in {"", "/"} or parsed.username or parsed.password or \
            parsed.query or parsed.fragment or \
            not all(os.path.isfile(path) for path in (ca, cert, key)):
        raise HTTPException(503, "AI Gateway mTLS is not configured")
    return url.rstrip("/"), ca, cert, key


def _client(ca: str, cert: str, key: str) -> httpx.AsyncClient:
    context = ssl.create_default_context(cafile=ca)
    context.load_cert_chain(certfile=cert, keyfile=key)
    return httpx.AsyncClient(verify=context, timeout=195, trust_env=False)


async def _invoke_local(tenant_id: str, prompt: str | None = None, *, messages: list[dict] | None = None) -> dict:
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            created = await client.post(f"{url}/internal/ai/executions",
                                        json={"tenant_id": tenant_id})
            created.raise_for_status()
            execution_id = created.json()["execution_id"]
            content = {"messages": messages} if messages is not None else {"prompt": prompt}
            invoked = await client.post(f"{url}/internal/ai/executions/{execution_id}/invoke",
                                        json={"tenant_id": tenant_id, **content})
            if invoked.status_code == 403:
                try:
                    detail = invoked.json().get("detail")
                except (ValueError, AttributeError):
                    detail = None
                if detail == "LIMIT_EXCEEDED":
                    raise HTTPException(429, "AI_USAGE_LIMIT_UNAVAILABLE")
                if isinstance(detail, str) and detail in {"LOCAL_MODEL_REQUIRED", "LOCAL_MODEL_UNAVAILABLE", "MODEL_NOT_ACTIVE",
                              "MODEL_CONFIGURATION_INVALID"}:
                    raise HTTPException(503, "AI_MODEL_UNAVAILABLE")
                if detail == "EXECUTION_CONFLICT_OR_LIMIT_EXCEEDED":
                    raise HTTPException(503, "AI_TEMPORARILY_UNAVAILABLE")
                raise HTTPException(502, "AI_GATEWAY_ERROR")
            invoked.raise_for_status()
            return invoked.json()
    except (httpx.HTTPError, KeyError, ValueError, OSError) as error:
        raise HTTPException(502, "AI Gateway 실행을 완료하지 못했습니다.") from error


@router.post("/guidance")
async def local_guidance(request: Request, body: GuidanceRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    return await _invoke_local(session.subject, body.prompt)


@router.post("/chat")
async def local_chat(request: Request, body: ChatRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    if sum(len(message.content) for message in body.messages) > 3400:
        raise HTTPException(422, "대화가 너무 깁니다. 이전 대화를 지우고 다시 질문해 주세요.")
    return await _invoke_local(session.subject, messages=[message.model_dump() for message in body.messages])


@router.post("/cooking-chat")
async def cooking_chat(request: Request, body: CookingChatRequest) -> dict:
    from .cooking import _jeyuk_feed_safety_current, _read, _store_call
    session, store, pk, job, pointer = await _read(request, str(body.job_id), write=True)
    derived = await _store_call(store, store.latest_process_result, pk)
    process = derived["process_assessment_status"] if derived else job.get("process_assessment_status")
    feed_verified = _jeyuk_feed_safety_current(job)
    quality_usable = bool(feed_verified and pointer and pointer.get("usable") and not job.get("evaluation_blocked", True) and
                          job.get("safety_status") == "PASSED" and
                          pointer.get("safety_version") == job.get("safety_version"))
    # A fixed allowlist avoids sending ownership IDs, raw observations, or user-authored notes.
    context = {"provenance": "SYNTHETIC", "menu": job.get("menu"), "job_status": job.get("status"),
               "safety_status": ("INCOMPLETE" if not feed_verified and job.get("safety_status") == "PASSED"
                                 else job.get("safety_status")), "process_status": process,
               "process_view": "LATEST_RESTATED" if derived else "AS_PROCESSED",
               "derived_job_version_changed": bool(derived and int(job["version"]) != int(derived["job_version_at_derivation"])),
               "time_status": job.get("process_time_status"), "cooking_seconds": job.get("actual_cooking_seconds"),
               "quality_passed": pointer.get("quality_passed") if quality_usable else None,
               "job_version": int(job["version"])}
    messages = [message.model_dump() for message in body.messages]
    messages[-1]["content"] = json.dumps({"question": messages[-1]["content"], "synthetic_job_context": context},
                                          ensure_ascii=False, separators=(",", ":"))
    if len(messages[-1]["content"]) > 2000 or sum(len(message["content"]) for message in messages) > 4000:
        raise HTTPException(422, "대화가 너무 깁니다. 이전 대화를 지우고 다시 질문해 주세요.")
    result = await _invoke_local(session.subject, messages=messages)
    return {**result, "context_job_version": context["job_version"], "context_process_view": context["process_view"]}


@router.post("/external/preview", status_code=201)
async def external_preview(request: Request, body: ExternalPreviewRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.post(f"{url}/internal/ai/external/preview", json={
                "tenant_id": session.subject, "model_id": body.model_id,
                "credential_id": body.credential_id})
            result.raise_for_status()
            return result.json()
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "외부 실행 견적을 만들지 못했습니다.") from None


@router.post("/external/quotes/{quote_id}/confirm")
async def external_confirm(request: Request, quote_id: str, body: ExternalConfirmRequest) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    if len(quote_id) != 36:
        raise HTTPException(400, "견적 ID가 올바르지 않습니다.")
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.post(f"{url}/internal/ai/external/quotes/{quote_id}/confirm",
                                       json={"tenant_id": session.subject,
                                             "notice_version": body.notice_version})
            result.raise_for_status()
            return result.json()
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "외부 실행 결과를 확인하지 못했습니다. 실행 상태를 조회해 주세요.") from None


@router.get("/executions/{execution_id}/status")
async def execution_status(request: Request, execution_id: str) -> dict:
    session = await _session(request, "customer")
    if len(execution_id) != 36:
        raise HTTPException(400, "실행 ID가 올바르지 않습니다.")
    url, ca, cert, key = _gateway_settings()
    try:
        async with _client(ca, cert, key) as client:
            result = await client.get(f"{url}/internal/ai/executions/{execution_id}/status",
                                      params={"tenant_id": session.subject})
            result.raise_for_status()
            return JSONResponse(result.json(), headers={'Cache-Control': 'no-store'})
    except (httpx.HTTPError, ValueError, OSError):
        raise HTTPException(502, "실행 상태를 읽지 못했습니다.") from None


@router.post("/tonkatsu-recipe-executions/{execution_id}/invoke")
async def invoke_tonkatsu_recipe_execution(request: Request, execution_id: UUID):
    from .combi_customer_invocation import invoke_customer_tonkatsu

    return await invoke_customer_tonkatsu(request, execution_id)


@router.post('/combi-recipe-executions/{execution_id}/invoke')
async def invoke_combi_recipe_execution(request: Request, execution_id: UUID):
    from .combi_customer_invocation import invoke_customer_combi
    return await invoke_customer_combi(request,execution_id)
