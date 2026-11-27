"""Customer combi request/notice records; invocation remains unavailable."""
import asyncio
from datetime import datetime, timezone
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from foodtech_contracts.combi_execution_binding import CombiExecutionBinding, combi_binding_payload
from .ai import _recipe_gateway
from .auth import _session
from .auth_store import get_store
from .combi_owned_observations import CombiObservationUnavailable, read_owned_combi_observation
from .cooking import _context, _store_call
from .recipe_scope import _require_principal_record, RecipeScopeDenied

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
