"""Owned Combi request, notice and stored-result reads; no automatic execution."""
import asyncio
from datetime import datetime, timezone
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query, Path
from fastapi.responses import JSONResponse
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from foodtech_contracts.combi_execution_binding import CombiExecutionBinding, combi_binding_payload
from .ai import _recipe_gateway
from .auth import _session
from .auth_store import Session, get_store
from .combi_owned_observations import CombiObservationUnavailable, read_owned_combi_observation
from .cooking import _context, _store_call
from .recipe_scope import _require_principal_record, RecipeScopeDenied
from .combi_candidate_meal import CandidateMealRequest

router = APIRouter(prefix='/api/customer/ai/combi', tags=['ai-combi'])


class CombiRecipeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    menu: Literal['RICE', 'MACKEREL']
    meal_id: UUID
    meal_version: int = Field(strict=True, ge=1, le=10**18)
    prior_meal_id: UUID
    prior_meal_version: int = Field(strict=True, ge=1, le=10**18)

    @model_validator(mode='after')
    def distinct(self):
        if self.meal_id == self.prior_meal_id:
            raise ValueError('distinct combi meals required')
        return self


class CombiRecipeConfirm(CombiRecipeRequest):
    notice_version: str = Field(min_length=1, max_length=64)
    quality_warning_acknowledged: StrictBool


async def _request_binding(request, body):
    session, store = await _context(request, write=True)
    # Keep only the authenticated metadata, independent of a mutable store
    # Session object; do not copy access/refresh tokens into this boundary.
    session = Session(session.surface, session.subject, None, session.csrf, session.expires, session.epoch)
    try:
        await _require_principal_record(session)
        auth_store = get_store()
        observed, references = [], []
        for meal_id, version in ((str(body.meal_id), body.meal_version),
                                 (str(body.prior_meal_id), body.prior_meal_version)):
            pk = f'SCOPE#USER#{session.subject}#COMBI_MEAL#{meal_id}'
            meta, _ = await _store_call(store, store.read, pk)
            source = meta.get('source') if isinstance(meta, dict) else None
            if not isinstance(source, dict):
                raise CombiObservationUnavailable('owned source unavailable')
            digest = source.get('raw_sha256')
            batch = await asyncio.to_thread(read_owned_combi_observation, session,
                meal_id=meal_id, expected_version=version, source_digest=digest, menu=body.menu,
                meal_reader=store.read_meal, source_reader=store.read_meal_source_chunk,
                principal_reader=auth_store.principal_record, clock=lambda: datetime.now(timezone.utc))
            if batch.unresolved or batch.baseline.safety_basis_valid is not True or \
                    batch.baseline.process_interrupted is not False:
                raise CombiObservationUnavailable('combi evidence unavailable')
            observed.append(batch)
            references.append((pk, version, digest))
        current, prior = observed
        if current.baseline.size != prior.baseline.size or current.job_id == prior.job_id or \
                current.source_digest == prior.source_digest:
            raise CombiObservationUnavailable('combi batches are not comparable')
        # Recheck the earlier meal after reading the other one. This is a
        # candidate binding, not a proof of immediate-prior history or a fence.
        for pk, version, digest in references:
            meta, _ = await _store_call(store, store.read, pk)
            if not isinstance(meta, dict) or type(meta.get('version')) is not int or meta['version'] != version or \
                    not isinstance(meta.get('source'), dict) or meta['source'].get('raw_sha256') != digest:
                raise CombiObservationUnavailable('combi batch changed')
        await _same_session(request, session)
        binding = CombiExecutionBinding(body.menu, current.meal_id, current.meal_version, current.job_id,
            current.source_digest, prior.meal_id, prior.meal_version, prior.job_id, prior.source_digest, int(session.epoch))
        return session, {'job_id': current.job_id, 'job_version': current.meal_version,
            'prior_job_id': prior.job_id, 'prior_job_version': prior.meal_version,
            'combi_binding': combi_binding_payload(binding)}
    except (CombiObservationUnavailable, RecipeScopeDenied, ValueError):
        raise HTTPException(409, '콤비오븐 작업·원본·평가 또는 권한이 변경되었습니다. 다시 조회해 주세요.',
                            headers={'Cache-Control': 'no-store'}) from None
    except (BotoCoreError, ClientError):
        raise HTTPException(503, '콤비오븐 저장 자료를 확인하지 못했습니다.',
                            headers={'Cache-Control': 'no-store'}) from None


async def _same_session(request, expected):
    current = await _session(request, 'customer')
    await _require_principal_record(current)
    if (current.subject, current.epoch, current.csrf) != (expected.subject, expected.epoch, expected.csrf):
        raise RecipeScopeDenied('customer session changed')


async def _forward(request, session, path, payload):
    result = await _recipe_gateway(path, payload)
    try:
        await _same_session(request, session)
    except RecipeScopeDenied:
        raise HTTPException(401, '로그인 상태가 변경되었습니다. 다시 로그인해 주세요.',
                            headers={'Cache-Control': 'no-store'}) from None
    return JSONResponse({**result, 'execution_available': False}, status_code=201,
                        headers={'Cache-Control': 'no-store'})


@router.post('/recipe-requests', status_code=201)
async def create_request(request: Request, body: CombiRecipeRequest):
    session, binding = await _request_binding(request, body)
    return await _forward(request, session, '/internal/ai/recipe-requests', {'tenant_id': session.subject, **binding})


@router.post('/recipe-requests/{parent_id}/local-preview', status_code=201)
async def preview(request: Request, parent_id: UUID, body: CombiRecipeRequest):
    session, binding = await _request_binding(request, body)
    return await _forward(request, session, '/internal/ai/recipe-downgrades/preview',
                          {'tenant_id': session.subject, 'parent_execution_id': str(parent_id), **binding})


@router.post('/recipe-quotes/{quote_id}/confirm', status_code=201)
async def confirm(request: Request, quote_id: UUID, body: CombiRecipeConfirm):
    session, binding = await _request_binding(request, body)
    return await _forward(request, session, f'/internal/ai/recipe-downgrades/{quote_id}/confirm',
        {'tenant_id': session.subject, 'notice_version': body.notice_version,
         'quality_warning_acknowledged': body.quality_warning_acknowledged, **binding})


async def _owned_result_meta(store, subject, meal_id, menu):
    """Read owned batch references, never synthesize a standalone cooking job."""
    from copy import deepcopy
    pk = f'SCOPE#USER#{subject}#COMBI_MEAL#{meal_id}'
    meta, _ = await _store_call(store, store.read, pk)
    meta = deepcopy(meta)
    field = 'rice_cooking_job_id' if menu == 'RICE' else 'mackerel_cooking_job_id'
    try:
        if type(meta) is not dict or meta.get('pk') != pk or meta.get('sk') != 'META' or \
                meta.get('meal_batch_id') != meal_id or meta.get('provenance') != 'SYNTHETIC' or \
                type(meta.get('version')) is not int or not 1 <= meta['version'] <= 10**18 or \
                type(meta.get(field)) is not str or str(UUID(meta[field])) != meta[field]:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(404, '해당 식사배치의 조리작업을 찾을 수 없습니다.',
                            headers={'Cache-Control': 'no-store'}) from None
    return meta, meta[field]


async def _combi_record_data(request, execution_id, meal_id, menu, *, result_view):
    from .ai import _gateway_settings, _client
    from .combi_owned_observations import _identical
    from foodtech_contracts.recipe_execution_scope import validate_recipe_status_payload
    from foodtech_contracts.recipe_result_view import validate_recipe_result_payload
    import httpx
    session, store = await _context(request)
    # An in-memory session can mutate during I/O; preserve authenticated facts.
    session = Session(session.surface, session.subject, None, session.csrf, session.expires, session.epoch)
    try:
        await _require_principal_record(session)
        meta, job_id = await _owned_result_meta(store, session.subject, str(meal_id), menu)
        url, ca, cert, key = _gateway_settings()
        resource = 'combi-recipe-results' if result_view else 'combi-recipe-status'
        async with _client(ca, cert, key) as client:
            response = await client.post(f'{url}/internal/ai/{resource}/{execution_id}',
                json={'tenant_id': session.subject, 'job_id': job_id, 'meal_id': str(meal_id), 'menu': menu})
            if response.status_code == 404:
                raise HTTPException(404, '해당 식사배치의 권고 실행 기록을 찾을 수 없습니다.',
                                    headers={'Cache-Control': 'no-store'})
            if response.status_code == 409:
                raise HTTPException(409, '권고 실행 상태가 변경되었습니다. 다시 조회해 주세요.',
                                    headers={'Cache-Control': 'no-store'})
            response.raise_for_status()
            validate = validate_recipe_result_payload if result_view else validate_recipe_status_payload
            payload = validate(response.json(), execution_id=str(execution_id), job_id=job_id)
        await _same_session(request, session)
        after, after_job = await _owned_result_meta(store, session.subject, str(meal_id), menu)
        await _same_session(request, session)
        if not _identical(meta, after) or after_job != job_id:
            raise HTTPException(409, '식사배치가 변경되었습니다. 다시 조회해 주세요.',
                                headers={'Cache-Control': 'no-store'})
        if result_view and payload['proposal'] is not None and payload['proposal']['menu'] != menu:
            raise ValueError('stored proposal menu mismatch')
        return ({**payload, 'meal_id': str(meal_id), 'menu': menu,
            'job_changed': payload['job_version'] != meta['version'], 'manual_invocation_enabled': False},
            session, store, meta, job_id)
    except RecipeScopeDenied:
        raise HTTPException(401, '로그인 상태가 변경되었습니다. 다시 로그인해 주세요.',
                            headers={'Cache-Control': 'no-store'}) from None
    except (ValueError, TypeError, KeyError, AttributeError, OSError, httpx.HTTPError):
        raise HTTPException(502, '콤비 권고 실행 기록을 확인하지 못했습니다.',
                            headers={'Cache-Control': 'no-store'}) from None
    except (BotoCoreError, ClientError):
        raise HTTPException(503, '콤비오븐 저장 자료를 확인하지 못했습니다.',
                            headers={'Cache-Control': 'no-store'}) from None


async def _combi_record(request, execution_id, meal_id, menu, *, result_view):
    payload, *_ = await _combi_record_data(request, execution_id, meal_id, menu, result_view=result_view)
    return JSONResponse(payload, headers={'Cache-Control': 'no-store'})


@router.get('/recipe-executions/{execution_id}/status')
async def combi_execution_status(request: Request, execution_id: UUID, meal_id: UUID,
                                 menu: Literal['RICE', 'MACKEREL']):
    return await _combi_record(request, execution_id, meal_id, menu, result_view=False)


@router.get('/recipe-executions/{execution_id}/result')
async def combi_execution_result(request: Request, execution_id: UUID, meal_id: UUID,
                                 menu: Literal['RICE', 'MACKEREL']):
    return await _combi_record(request, execution_id, meal_id, menu, result_view=True)


class CombiRecipeReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    meal_id: UUID
    menu: Literal['RICE', 'MACKEREL']
    expected_meal_version: int = Field(strict=True, ge=1, le=10**18)
    expected_review_version: int = Field(strict=True, ge=0, lt=2**63 - 2)
    request_id: UUID
    proposal_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['APPROVE', 'REJECT']
    reason: str = Field(min_length=1, max_length=1000)
    acknowledged: StrictBool

    @model_validator(mode='after')
    def explicit_review(self):
        if not self.acknowledged or not self.reason.strip() or self.reason != self.reason.strip():
            raise ValueError('explicit trimmed review required')
        return self


async def _review_recheck(request, expected, store, meta, meal_id, menu):
    from .combi_owned_observations import _identical
    await _same_session(request, expected)
    after, _ = await _owned_result_meta(store, expected.subject, meal_id, menu)
    await _same_session(request, expected)
    if not _identical(meta, after):
        raise HTTPException(409, '식사배치가 변경되었습니다. 다시 조회해 주세요.', headers={'Cache-Control': 'no-store'})


@router.get('/recipe-executions/{execution_id}/review')
async def combi_review_context(request: Request, execution_id: UUID, meal_id: UUID, menu: Literal['RICE', 'MACKEREL']):
    from foodtech_contracts.recipe_result_view import recipe_proposal_digest
    from .combi_review_store import CombiReviewStore
    payload, session, store, meta, job_id = await _combi_record_data(request, execution_id, meal_id, menu, result_view=True)
    if payload['projection_status'] != 'AVAILABLE':
        raise HTTPException(409, '검토할 저장 권고가 없습니다.', headers={'Cache-Control': 'no-store'})
    pk = f'SCOPE#USER#{session.subject}#COMBI_MEAL#{meal_id}'
    try:
        current = await _store_call(store, CombiReviewStore(store).current, pk, menu)
        await _review_recheck(request, session, store, meta, str(meal_id), menu)
    except RecipeScopeDenied:
        raise HTTPException(401, '로그인 상태가 변경되었습니다.', headers={'Cache-Control': 'no-store'}) from None
    except (ValueError, BotoCoreError, ClientError):
        raise HTTPException(503, '검토 기록을 확인하지 못했습니다.', headers={'Cache-Control': 'no-store'}) from None
    return JSONResponse({'execution_id': str(execution_id), 'meal_id': str(meal_id), 'menu': menu,
        'job_id': job_id, 'job_version': payload['job_version'], 'proposal': payload['proposal'],
        'proposal_digest': recipe_proposal_digest(payload['proposal']), 'review_version': current['version'],
        'latest_review': current['review'], 'can_review': not payload['job_changed']}, headers={'Cache-Control': 'no-store'})


@router.post('/recipe-executions/{execution_id}/review', status_code=201)
async def combi_save_review(request: Request, execution_id: UUID, body: CombiRecipeReview):
    from .cooking import _now
    from .cooking_store import Conflict
    from .combi_review_store import CombiReviewStore
    from foodtech_contracts.recipe_result_view import recipe_proposal_digest
    before, _ = await _context(request, write=True)
    before = Session(before.surface, before.subject, None, before.csrf, before.expires, before.epoch)
    payload, session, store, meta, job_id = await _combi_record_data(
        request, execution_id, body.meal_id, body.menu, result_view=True)
    if (before.subject, before.epoch, before.csrf) != (session.subject, session.epoch, session.csrf):
        raise HTTPException(401, '로그인 상태가 변경되었습니다.', headers={'Cache-Control': 'no-store'})
    if payload['projection_status'] != 'AVAILABLE' or payload['job_changed'] or \
            payload['job_version'] != body.expected_meal_version or \
            recipe_proposal_digest(payload['proposal']) != body.proposal_digest:
        raise HTTPException(409, '권고 또는 식사배치가 변경되었습니다. 다시 조회해 주세요.',
                            headers={'Cache-Control': 'no-store'})
    record = {'schema_version': 'combi-recipe-review-v1', 'version': body.expected_review_version + 1,
        'request_id': str(body.request_id), 'execution_id': str(execution_id), 'meal_id': str(body.meal_id),
        'menu': body.menu, 'job_id': job_id, 'job_version': body.expected_meal_version,
        'proposal_digest': body.proposal_digest, 'proposal': payload['proposal'], 'decision': body.decision,
        'reason': body.reason, 'reviewed_at': _now()}
    pk = f'SCOPE#USER#{session.subject}#COMBI_MEAL#{body.meal_id}'
    try:
        saved = await _store_call(store, CombiReviewStore(store).save, pk, body.menu,
            body.expected_meal_version, body.expected_review_version, record, session.epoch)
        await _review_recheck(request, session, store, meta, str(body.meal_id), body.menu)
    except Conflict:
        raise HTTPException(409, '검토 또는 식사배치가 변경되었습니다. 검토 이력을 확인해 주세요.',
                            headers={'Cache-Control': 'no-store'}) from None
    except RecipeScopeDenied:
        raise HTTPException(401, '로그인 상태가 변경되었습니다.', headers={'Cache-Control': 'no-store'}) from None
    except (ValueError, BotoCoreError, ClientError):
        raise HTTPException(503, '검토 저장 여부를 확인하지 못했습니다. 같은 요청으로 다시 확인해 주세요.',
                            headers={'Cache-Control': 'no-store'}) from None
    return JSONResponse({'review': saved, 'adoption_available': False}, status_code=201, headers={'Cache-Control': 'no-store'})


@router.get('/recipe-reviews')
async def combi_review_history(request: Request, meal_id: UUID, menu: Literal['RICE', 'MACKEREL'],
                                limit: int = Query(20, ge=1, le=50),
                                cursor: int | None = Query(None, ge=1, lt=2**63 - 1)):
    from .combi_review_store import CombiReviewStore
    session, store = await _context(request)
    session = Session(session.surface, session.subject, None, session.csrf, session.expires, session.epoch)
    try:
        await _require_principal_record(session)
        meta, _ = await _owned_result_meta(store, session.subject, str(meal_id), menu)
        pk = f'SCOPE#USER#{session.subject}#COMBI_MEAL#{meal_id}'
        result = await _store_call(store, CombiReviewStore(store).history, pk, menu, limit, cursor)
        await _review_recheck(request, session, store, meta, str(meal_id), menu)
    except RecipeScopeDenied:
        raise HTTPException(401, '로그인 상태가 변경되었습니다.', headers={'Cache-Control': 'no-store'}) from None
    except (ValueError, BotoCoreError, ClientError):
        raise HTTPException(503, '검토 이력을 확인하지 못했습니다.', headers={'Cache-Control': 'no-store'}) from None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


class CombiCandidatePreservation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    meal_id: UUID
    menu: Literal['RICE', 'MACKEREL']
    expected_meal_version: int = Field(strict=True, ge=1, le=10**18)
    proposal_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    acknowledged: StrictBool

    @model_validator(mode='after')
    def explicit_preservation(self):
        if not self.acknowledged:
            raise ValueError('explicit candidate preservation required')
        return self


async def _candidate_response(request, meal_id, menu, review_version, body=None):
    from .combi_recipe_candidate import CombiCandidateStore
    from .cooking_store import Conflict
    from .cooking import _now
    session, store = await _context(request, write=body is not None)
    session = Session(session.surface, session.subject, None, session.csrf, session.expires, session.epoch)
    try:
        await _require_principal_record(session)
        meta, _ = await _owned_result_meta(store, session.subject, str(meal_id), menu)
        pk = f'SCOPE#USER#{session.subject}#COMBI_MEAL#{meal_id}'
        candidates = CombiCandidateStore(store)
        if body is None:
            value = await _store_call(store, candidates.get, pk, menu, review_version)
        else:
            if meta['version'] != body.expected_meal_version:
                raise Conflict
            value = await _store_call(store, candidates.preserve, pk, menu, review_version,
                body.expected_meal_version, body.proposal_digest, _now(), session.epoch)
        pointer = await _store_call(store, candidates.reviews.current, pk, menu)
        await _review_recheck(request, session, store, meta, str(meal_id), menu)
        if value is None:
            raise HTTPException(404, '이 검토에 저장된 후보 버전이 없습니다.', headers={'Cache-Control':'no-store'})
        return JSONResponse({'candidate':value, 'current_meal_version':meta['version'],
            'current_review_version':pointer['version'], 'adoption_available':bool(
                meta['version'] == value['job_version'] and pointer['version'] == value['review_version'] and
                pointer['review']['decision'] == 'APPROVE')},
            status_code=200 if body is None else 201, headers={'Cache-Control':'no-store'})
    except Conflict:
        raise HTTPException(409, '현재 승인 검토 또는 식사배치가 변경되었습니다. 이력을 확인해 주세요.',
                            headers={'Cache-Control':'no-store'}) from None
    except RecipeScopeDenied:
        raise HTTPException(401, '로그인 상태가 변경되었습니다.', headers={'Cache-Control':'no-store'}) from None
    except (ValueError, BotoCoreError, ClientError):
        raise HTTPException(503, '후보 저장 여부를 확인하지 못했습니다. 같은 검토 버전의 후보를 조회해 주세요.',
                            headers={'Cache-Control':'no-store'}) from None


@router.get('/recipe-candidates/{review_version}')
async def combi_recipe_candidate(request: Request, meal_id: UUID, menu: Literal['RICE','MACKEREL'],
                                  review_version: int = Path(ge=1, le=2**53-1)):
    return await _candidate_response(request, meal_id, menu, review_version)


@router.post('/recipe-candidates/{review_version}', status_code=201)
async def combi_preserve_candidate(request: Request, body: CombiCandidatePreservation,
                                   review_version: int = Path(ge=1, le=2**53-1)):
    return await _candidate_response(request, body.meal_id, body.menu, review_version, body)


@router.post('/recipe-candidates/{review_version}/meals', status_code=201)
async def create_candidate_meal(request: Request, body: CandidateMealRequest,
                                review_version: int = Path(ge=1, le=2**53-1)):
    from .combi_candidate_meal_store import CombiCandidateMealStore
    from .combi_meal import read_meal_for_subject
    from .cooking_store import Conflict
    from .cooking import _now
    session, store = await _context(request, write=True)
    session = Session(session.surface, session.subject, None, session.csrf, session.expires, session.epoch)
    try:
        await _require_principal_record(session)
        adapter = CombiCandidateMealStore(store)
        created = await _store_call(store, adapter.create, session.subject, review_version, body, _now(), session.epoch)
        # Re-read the exact immutable request even when creation's response was
        # lost. Do not require a later changed source to still be current when
        # returning an already-created owned target, and never reset its state.
        await _store_call(store, adapter._reconcile, session.subject, review_version, body)
        meal = await read_meal_for_subject(store, session.subject, body.request_id)
        await _same_session(request, session)
        return JSONResponse({'schema_version':'combi-candidate-meal-receipt-v1',
            'request_id':str(body.request_id), 'meal_id':str(body.request_id),
            'request_hash':meal['meal']['recipe_candidate_selection']['request_hash'],
            'created':created, 'meal':meal}, status_code=201 if created else 200,
            headers={'Cache-Control':'no-store'})
    except Conflict:
        raise HTTPException(409, '승인·원천·계정 또는 같은 요청의 새 배치가 변경되었습니다. 다시 확인해 주세요.',
                            headers={'Cache-Control':'no-store'}) from None
    except RecipeScopeDenied:
        raise HTTPException(401, '로그인 상태가 변경되었습니다.', headers={'Cache-Control':'no-store'}) from None
    except (ValueError, KeyError, TypeError, BotoCoreError, ClientError):
        raise HTTPException(503, '새 배치 생성 여부를 확인하지 못했습니다. 같은 요청으로 확인·재시도해 주세요.',
                            headers={'Cache-Control':'no-store'}) from None
