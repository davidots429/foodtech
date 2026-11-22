"""Opt-in withdrawal request and fresh-login-only cancellation boundary."""
import asyncio
import os
import secrets
import time
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from botocore.exceptions import BotoCoreError, ClientError

from . import auth
from .account_withdrawal import WithdrawalConflict, _pending, _integer
from .auth_store import Session, get_store
from .principal_enrollment import PrincipalEnrollmentConflict

router = APIRouter(prefix='/api/customer/withdrawal')
RECOVERY_SECONDS = 300


def _enabled():
    if os.getenv('FOODTECH_ACCOUNT_WITHDRAWAL_ENABLED') != 'true' or \
            os.getenv('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') != 'true':
        raise HTTPException(404)


def _response(data, status_code=200):
    response = JSONResponse(data, status_code=status_code)
    response.headers['Cache-Control'] = 'no-store'
    return response


def _recoverable(principal, *, now):
    try:
        if not isinstance(principal, dict):
            raise WithdrawalConflict('missing account')
        request_id = principal.get('withdrawal_request_id')
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise WithdrawalConflict('invalid request')
        deadline = _pending(principal, request_id)
        if now >= deadline or _integer(principal.get('epoch')) != _integer(principal['withdrawal_epoch'], 1):
            raise WithdrawalConflict('cancellation unavailable')
        return request_id, deadline
    except (ValueError, TypeError, AttributeError, WithdrawalConflict):
        raise HTTPException(403, '탈퇴 취소가 가능한 계정 상태가 아닙니다.') from None


@router.get('/options')
async def options():
    return _response({'enabled': os.getenv('FOODTECH_ACCOUNT_WITHDRAWAL_ENABLED') == 'true' and
                      os.getenv('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') == 'true'})


@router.get('/login')
async def recovery_login():
    _enabled()
    return await auth._flow('customer', auth.Settings.from_env(), purpose='withdrawal')


async def recovery_callback(request, settings, flow, identity, access):
    _enabled()
    now = int(time.time())
    if flow.surface != 'customer' or type(flow.started_at) is not int or not 0 < flow.started_at <= now:
        raise HTTPException(401, '탈퇴 취소를 위해 새로 인증해야 합니다.')
    # A managed-login cookie is insufficient: signed auth_time must establish
    # authentication after this server-owned PKCE flow began.
    auth_time = identity.get('auth_time')
    if type(auth_time) is not int or not flow.started_at <= auth_time <= now + 5:
        raise HTTPException(401, '탈퇴 취소를 위해 새로 인증해야 합니다.')
    subject = identity['sub']
    if not await auth._checked_cognito_access(settings, subject, 'customer'):
        raise HTTPException(403, '현재 계정 권한을 확인할 수 없습니다.')
    store = get_store()
    principal = await asyncio.to_thread(store.principal, subject)
    _recoverable(principal, now=now)
    try:
        expires = min(now + RECOVERY_SECONDS, _integer(access.get('exp'), 1))
    except WithdrawalConflict:
        raise HTTPException(401, '인증 만료 시각을 확인할 수 없습니다.') from None
    if expires <= now:
        raise HTTPException(401, '인증이 만료되었습니다.')
    session_id = secrets.token_urlsafe(32)
    await asyncio.to_thread(store.save_session, session_id,
        Session('withdrawal', subject, None, secrets.token_urlsafe(32), expires, int(principal['epoch'])))
    old_session = request.cookies.get(auth.SESSION_COOKIE, '')
    if old_session:
        await asyncio.to_thread(store.delete_session, old_session)
    response = RedirectResponse(settings.customer_web, status_code=303)
    response.headers['Cache-Control'] = 'no-store'
    response.delete_cookie(auth.FLOW_COOKIE, path='/api')
    auth._cookie(response, auth.SESSION_COOKIE, session_id, expires - now, settings.secure_cookies)
    return response


async def _recovery_session(request):
    _enabled()
    store = get_store()
    session_id = request.cookies.get(auth.SESSION_COOKIE, '')
    session = await asyncio.to_thread(store.read_session, session_id) if session_id else None
    if not session or session.surface != 'withdrawal':
        raise HTTPException(401, '탈퇴 취소용 본인 인증이 필요합니다.')
    principal = await asyncio.to_thread(store.principal, session.subject)
    request_id, deadline = _recoverable(principal, now=int(time.time()))
    if session.epoch != principal['epoch']:
        raise HTTPException(401, '계정 상태가 변경되었습니다. 다시 인증해 주세요.')
    if not await auth._checked_cognito_access(auth.Settings.from_env(), session.subject, 'customer'):
        raise HTTPException(401, '현재 계정 권한을 확인할 수 없습니다.')
    return store, session, request_id, deadline


class WithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    confirmed: bool = Field(strict=True)


@router.post('/request')
async def request_withdrawal(request: Request, body: WithdrawalRequest):
    _enabled()
    session = await auth._session(request, 'customer')
    auth._csrf(request, session)
    if not body.confirmed:
        raise HTTPException(422, '탈퇴 안내를 확인해야 합니다.')
    store = get_store()
    try:
        await asyncio.to_thread(store.enrollment_fence)
    except (PrincipalEnrollmentConflict, WithdrawalConflict, BotoCoreError, ClientError):
        raise HTTPException(503, '계정 삭제 경합을 보호하는 저장소가 준비되지 않았습니다.') from None
    try:
        state = await asyncio.to_thread(store.withdrawal_transition, subject=session.subject,
            request_id=str(body.request_id), now=int(time.time()), action='request', expected_epoch=session.epoch)
    except WithdrawalConflict:
        raise HTTPException(409, '계정 상태가 변경되었거나 탈퇴 요청을 처리할 수 없습니다.') from None
    await asyncio.to_thread(store.delete_session, request.cookies.get(auth.SESSION_COOKIE, ''))
    response = _response({'status': 'PENDING', 'requested_at': int(state['withdrawal_requested_at']),
                          'cancel_before': int(state['withdrawal_deadline'])})
    response.delete_cookie(auth.SESSION_COOKIE, path='/api')
    return response


@router.get('/status')
async def withdrawal_status(request: Request):
    _, session, _, deadline = await _recovery_session(request)
    return _response({'status': 'PENDING', 'cancel_before': deadline, 'csrf': session.csrf})


@router.post('/cancel')
async def cancel_withdrawal(request: Request):
    store, session, request_id, _ = await _recovery_session(request)
    auth._csrf(request, session)
    try:
        await asyncio.to_thread(store.withdrawal_transition, subject=session.subject,
            request_id=request_id, now=int(time.time()), action='cancel', expected_epoch=session.epoch)
    except WithdrawalConflict:
        raise HTTPException(409, '취소 기한이 지났거나 계정 상태가 변경되었습니다.') from None
    await asyncio.to_thread(store.delete_session, request.cookies.get(auth.SESSION_COOKIE, ''))
    response = _response({'status': 'CANCELED', 'login_required': True})
    response.delete_cookie(auth.SESSION_COOKIE, path='/api')
    return response
