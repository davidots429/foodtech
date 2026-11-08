"""Initial Cognito code/PKCE BFF and server-owned browser sessions."""

import base64
import asyncio
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx
import jwt
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .auth_store import LoginFlow, Session, get_store
from .tier_store import get_tier_store

router = APIRouter(prefix="/api")
SESSION_COOKIE = "ft_session"
FLOW_COOKIE = "ft_login_flow"
FLOW_SECONDS = 300
SESSION_SECONDS = 3600
DEMO_NOTICE = "실제 과금이 발생하지 않으며 사업자 진위 조회를 수행하지 않는 비영리 시연 기능입니다."
ADMIN_ACR = "urn:cognito:loa:4"


@dataclass(frozen=True)
class Settings:
    issuer: str
    domain: str
    customer_client_id: str
    admin_client_id: str
    customer_callback: str
    admin_callback: str
    customer_web: str
    admin_web: str
    secure_cookies: bool

    @classmethod
    def from_env(cls) -> "Settings":
        names = (
            "FOODTECH_COGNITO_ISSUER", "FOODTECH_COGNITO_DOMAIN",
            "FOODTECH_CUSTOMER_CLIENT_ID", "FOODTECH_ADMIN_CLIENT_ID",
            "FOODTECH_CUSTOMER_CALLBACK", "FOODTECH_ADMIN_CALLBACK",
            "FOODTECH_CUSTOMER_WEB", "FOODTECH_ADMIN_WEB",
        )
        values = [os.getenv(name, "").rstrip("/") for name in names]
        if not all(values):
            raise HTTPException(503, "Cognito 설정이 완료되지 않았습니다.")
        secure = os.getenv("FOODTECH_SECURE_COOKIES", "true").lower() == "true"
        if secure and not os.getenv("FOODTECH_AUTH_TABLE"):
            raise HTTPException(503, "배포 환경에는 공유 인증 저장소가 필요합니다.")
        if not values[0].startswith("https://") or not values[1].startswith("https://"):
            raise HTTPException(503, "Cognito 주소는 HTTPS여야 합니다.")
        if secure and any(not value.startswith("https://") for value in values[4:]):
            raise HTTPException(503, "공개 URL은 HTTPS여야 합니다.")
        if not secure and any(urlparse(value).hostname not in {"localhost", "127.0.0.1"} for value in values[4:]):
            raise HTTPException(503, "비보안 쿠키는 로컬 개발에서만 허용됩니다.")
        return cls(*values, secure)

    def client(self, surface: str) -> tuple[str, str, str]:
        if surface == "customer":
            return self.customer_client_id, self.customer_callback, self.customer_web
        if surface == "admin":
            return self.admin_client_id, self.admin_callback, self.admin_web
        raise HTTPException(404)


def _cookie(response: Response, name: str, value: str, max_age: int, secure: bool) -> None:
    response.set_cookie(
        name, value, max_age=max_age, httponly=True, secure=secure,
        samesite="lax", path="/api",
    )


def _cognito_access(settings: Settings, subject: str, surface: str) -> bool:
    """Check current account and admin group, including changes outside this API."""
    client = boto3.client("cognito-idp", region_name="ap-southeast-2")
    pool_id = settings.issuer.rsplit("/", 1)[-1]
    try:
        user = client.admin_get_user(UserPoolId=pool_id, Username=subject)
        if not user.get("Enabled", False) or user.get("UserStatus") != "CONFIRMED":
            return False
        if surface == "admin":
            pages = client.get_paginator("admin_list_groups_for_user").paginate(
                UserPoolId=pool_id, Username=subject,
            )
            return any(group.get("GroupName") == "foodtech-admin"
                       for page in pages for group in page.get("Groups", []))
        return True
    except client.exceptions.UserNotFoundException:
        return False


async def _checked_cognito_access(settings: Settings, subject: str, surface: str) -> bool:
    try:
        return await asyncio.to_thread(_cognito_access, settings, subject, surface)
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(503, "현재 계정 권한을 확인할 수 없습니다.") from exc


async def _flow(surface: str, settings: Settings, *, signup: bool = False) -> RedirectResponse:
    client_id, callback, _ = settings.client(surface)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    flow_id = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    await asyncio.to_thread(get_store().save_flow, flow_id,
                            LoginFlow(surface, state, verifier, int(time.time() + FLOW_SECONDS)))
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": callback,
        "scope": "openid email profile aws.cognito.signin.user.admin" if surface == "customer" else "openid email profile",
        "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    }
    if surface == "admin":
        params["acr_values"] = ADMIN_ACR
        params["max_age"] = "0"
    query = urlencode(params)
    path = "/signup" if signup else "/oauth2/authorize"
    response = RedirectResponse(f"{settings.domain}{path}?{query}", status_code=303)
    _cookie(response, FLOW_COOKIE, flow_id, FLOW_SECONDS, settings.secure_cookies)
    return response


@router.get("/auth/{surface}/login")
async def login(surface: str) -> RedirectResponse:
    return await _flow(surface, Settings.from_env())


@router.get("/auth/customer/signup")
async def signup() -> RedirectResponse:
    return await _flow("customer", Settings.from_env(), signup=True)


def _claims(token: str, settings: Settings, client_id: str, use: str) -> dict:
    try:
        key = jwt.PyJWKClient(f"{settings.issuer}/.well-known/jwks.json").get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], issuer=settings.issuer,
            audience=client_id if use == "id" else None,
            options={"verify_aud": use == "id", "require": ["exp", "iss", "sub", "token_use"]},
            leeway=30,
        )
        if claims.get("token_use") != use:
            raise ValueError("wrong token type")
        if use == "access" and claims.get("client_id") != client_id:
            raise ValueError("wrong client")
        return claims
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(401, "인증 결과를 확인할 수 없습니다.") from exc


async def _exchange(settings: Settings, client_id: str, callback_url: str, code: str, verifier: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        token_response = await client.post(
            f"{settings.domain}/oauth2/token",
            data={"grant_type": "authorization_code", "client_id": client_id,
                  "code": code, "redirect_uri": callback_url, "code_verifier": verifier},
        )
        token_response.raise_for_status()
        return token_response.json()


@router.get("/auth/{surface}/callback")
async def callback(surface: str, request: Request, code: str = "", state: str = "") -> RedirectResponse:
    settings = Settings.from_env()
    client_id, callback_url, web = settings.client(surface)
    flow_id = request.cookies.get(FLOW_COOKIE, "")
    flow = await asyncio.to_thread(get_store().take_flow, flow_id) if flow_id else None
    if not flow or flow.surface != surface or not code or not secrets.compare_digest(flow.state, state):
        raise HTTPException(400, "로그인 요청이 만료되었거나 일치하지 않습니다.")
    try:
        tokens = await _exchange(settings, client_id, callback_url, code, flow.verifier)
        identity = _claims(tokens["id_token"], settings, client_id, "id")
        access = _claims(tokens["access_token"], settings, client_id, "access")
        if identity["sub"] != access["sub"]:
            raise HTTPException(401, "인증 주체가 일치하지 않습니다.")
        if surface == "admin" and "foodtech-admin" not in access.get("cognito:groups", []):
            raise HTTPException(403, "관리자 권한이 없습니다.")
        if surface == "admin" and not (
            access.get("acr") == ADMIN_ACR
            or {"pwd", "otp", "mfa"}.issubset(set(access.get("amr") or []))
        ):
            raise HTTPException(403, "관리자 추가 인증이 필요합니다.")
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(401, "로그인을 완료할 수 없습니다.") from exc
    store = get_store()
    principal = await asyncio.to_thread(store.principal, identity["sub"])
    if principal.get("suspended"):
        raise HTTPException(403, "이용이 중단된 계정입니다.")
    if surface == "admin" and principal.get("admin_blocked"):
        raise HTTPException(403, "관리자 권한이 회수되었습니다.")
    if not await _checked_cognito_access(settings, identity["sub"], surface):
        raise HTTPException(403, "현재 계정 권한을 확인할 수 없습니다.")
    old_session_id = request.cookies.get(SESSION_COOKIE, "")
    if old_session_id:
        await asyncio.to_thread(store.delete_session, old_session_id)
    session_id = secrets.token_urlsafe(32)
    await asyncio.to_thread(store.save_session, session_id, Session(
        surface, identity["sub"], identity.get("email"), secrets.token_urlsafe(32),
        int(min(time.time() + SESSION_SECONDS, float(access["exp"]))),
        int(principal.get("epoch", 0)),
        tokens["access_token"],
    ))
    response = RedirectResponse(web, status_code=303)
    response.delete_cookie(FLOW_COOKIE, path="/api")
    _cookie(response, SESSION_COOKIE, session_id, SESSION_SECONDS, settings.secure_cookies)
    return response


def _admin_password_auth(client_id: str, username: str, password: str) -> dict:
    client = boto3.client("cognito-idp", region_name="ap-southeast-2")
    return client.initiate_auth(
        ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )


def _admin_totp_auth(client_id: str, username: str, challenge_session: str, code: str) -> dict:
    client = boto3.client("cognito-idp", region_name="ap-southeast-2")
    return client.respond_to_auth_challenge(
        ClientId=client_id, ChallengeName="SOFTWARE_TOKEN_MFA", Session=challenge_session,
        ChallengeResponses={"USERNAME": username, "SOFTWARE_TOKEN_MFA_CODE": code},
    )


@router.post("/auth/admin/password")
async def admin_password(body: dict) -> Response:
    """Use Cognito's direct MFA challenge when ACR/AMR is unavailable in this pool."""
    settings = Settings.from_env()
    username, password = body.get("username"), body.get("password")
    if not isinstance(username, str) or not 3 <= len(username) <= 320 or not isinstance(password, str) or not password:
        raise HTTPException(400, "계정과 비밀번호를 입력해야 합니다.")
    try:
        result = await asyncio.to_thread(_admin_password_auth, settings.admin_client_id, username, password)
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(401, "로그인 정보를 확인하지 못했습니다.") from exc
    if result.get("ChallengeName") != "SOFTWARE_TOKEN_MFA" or not result.get("Session"):
        raise HTTPException(403, "관리자 인증에는 인증 앱 TOTP가 필요합니다.")
    flow_id = secrets.token_urlsafe(32)
    await asyncio.to_thread(get_store().save_flow, flow_id, LoginFlow(
        "admin_totp", username, result["Session"], int(time.time() + FLOW_SECONDS),
    ))
    response = JSONResponse({"next": "SOFTWARE_TOKEN_MFA"})
    response.headers["Cache-Control"] = "no-store"
    _cookie(response, FLOW_COOKIE, flow_id, FLOW_SECONDS, settings.secure_cookies)
    return response


@router.post("/auth/admin/totp")
async def admin_totp(request: Request, body: dict) -> Response:
    settings = Settings.from_env()
    code = body.get("code")
    if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        raise HTTPException(400, "6자리 인증 코드를 입력해야 합니다.")
    flow_id = request.cookies.get(FLOW_COOKIE, "")
    flow = await asyncio.to_thread(get_store().take_flow, flow_id) if flow_id else None
    if not flow or flow.surface != "admin_totp":
        raise HTTPException(400, "관리자 인증 요청이 만료되었습니다.")
    try:
        result = await asyncio.to_thread(_admin_totp_auth, settings.admin_client_id, flow.state, flow.verifier, code)
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(401, "인증 코드를 확인하지 못했습니다. 다시 로그인해 주세요.") from exc
    tokens = result.get("AuthenticationResult")
    if not tokens or result.get("ChallengeName"):
        raise HTTPException(403, "관리자 추가 인증을 완료하지 못했습니다.")
    identity = _claims(tokens["IdToken"], settings, settings.admin_client_id, "id")
    access = _claims(tokens["AccessToken"], settings, settings.admin_client_id, "access")
    if identity["sub"] != access["sub"] or "foodtech-admin" not in access.get("cognito:groups", []):
        raise HTTPException(403, "관리자 권한이 없습니다.")
    store = get_store()
    principal = await asyncio.to_thread(store.principal, identity["sub"])
    if principal.get("suspended") or principal.get("admin_blocked"):
        raise HTTPException(403, "관리자 권한이 회수되었습니다.")
    if not await _checked_cognito_access(settings, identity["sub"], "admin"):
        raise HTTPException(403, "현재 계정 권한을 확인할 수 없습니다.")
    old_session_id = request.cookies.get(SESSION_COOKIE, "")
    if old_session_id:
        await asyncio.to_thread(store.delete_session, old_session_id)
    session_id = secrets.token_urlsafe(32)
    await asyncio.to_thread(store.save_session, session_id, Session(
        "admin", identity["sub"], identity.get("email"), secrets.token_urlsafe(32),
        int(min(time.time() + SESSION_SECONDS, float(access["exp"]))), int(principal.get("epoch", 0)),
        tokens["AccessToken"],
    ))
    response = JSONResponse({"authenticated": True})
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(FLOW_COOKIE, path="/api")
    _cookie(response, SESSION_COOKIE, session_id, SESSION_SECONDS, settings.secure_cookies)
    return response


async def _session(request: Request, surface: str) -> Session:
    if surface not in {"customer", "admin"}:
        raise HTTPException(404)
    session_id = request.cookies.get(SESSION_COOKIE, "")
    store = get_store()
    session = await asyncio.to_thread(store.read_session, session_id) if session_id else None
    if not session or session.surface != surface:
        raise HTTPException(401, "로그인이 필요합니다.")
    principal = await asyncio.to_thread(store.principal, session.subject)
    if (int(principal.get("epoch", 0)) != session.epoch or principal.get("suspended")
            or (surface == "admin" and principal.get("admin_blocked"))):
        await asyncio.to_thread(store.delete_session, session_id)
        raise HTTPException(401, "세션 권한이 회수되었습니다.")
    if not await _checked_cognito_access(Settings.from_env(), session.subject, surface):
        await asyncio.to_thread(store.delete_session, session_id)
        raise HTTPException(401, "현재 계정 권한이 없습니다.")
    return session


@router.get("/{surface}/me")
async def me(surface: str, request: Request) -> dict:
    session = await _session(request, surface)
    current_tier = await asyncio.to_thread(get_tier_store().current, session.subject) if surface == "customer" else None
    tier = None if surface == "admin" else "DEMO_PAID" if current_tier and current_tier["status"] == "APPROVED_FOR_DEMO" else "FREE"
    return {"email": session.email, "surface": session.surface, "csrf": session.csrf,
            "tier": tier}


@router.get("/customer/example")
async def example(request: Request) -> dict:
    await _session(request, "customer")
    return {"source": "SYNTHETIC", "readOnly": True,
            "title": "예시 조리 데이터", "message": "고객별 작업과 분리된 읽기 전용 시연 자료입니다."}


@router.get("/customer/demo-plan")
async def demo_plan(request: Request) -> dict:
    await _session(request, "customer")
    return {"price": "00원", "notice": DEMO_NOTICE, "billingEnabled": False,
            "businessVerificationEnabled": False}


def _totp_start(access_token: str) -> str:
    client = boto3.client("cognito-idp", region_name="ap-southeast-2")
    return client.associate_software_token(AccessToken=access_token)["SecretCode"]


def _totp_verify(access_token: str, code: str) -> None:
    client = boto3.client("cognito-idp", region_name="ap-southeast-2")
    result = client.verify_software_token(AccessToken=access_token, UserCode=code)
    if result.get("Status") != "SUCCESS":
        raise ValueError("TOTP verification failed")
    client.set_user_mfa_preference(
        AccessToken=access_token,
        SoftwareTokenMfaSettings={"Enabled": True, "PreferredMfa": True},
    )


def _csrf(request: Request, session: Session) -> None:
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.csrf):
        raise HTTPException(403, "CSRF 확인에 실패했습니다.")


@router.post("/customer/mfa/totp/start")
async def totp_start(request: Request) -> Response:
    session = await _session(request, "customer")
    _csrf(request, session)
    if not session.access_token:
        raise HTTPException(401, "다시 로그인해야 합니다.")
    try:
        secret = await asyncio.to_thread(_totp_start, session.access_token)
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(503, "인증 앱 등록을 시작하지 못했습니다.") from exc
    response = JSONResponse({"secret": secret})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/customer/mfa/totp/verify")
async def totp_verify(request: Request, body: dict) -> dict:
    session = await _session(request, "customer")
    _csrf(request, session)
    if not session.access_token:
        raise HTTPException(401, "다시 로그인해야 합니다.")
    code = body.get("code")
    if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
        raise HTTPException(400, "6자리 인증 코드를 입력해야 합니다.")
    try:
        await asyncio.to_thread(_totp_verify, session.access_token, code)
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise HTTPException(400, "인증 코드를 확인하지 못했습니다.") from exc
    return {"totp": "ENABLED"}


@router.post("/{surface}/logout")
async def logout(surface: str, request: Request) -> Response:
    session = await _session(request, surface)
    _csrf(request, session)
    await asyncio.to_thread(get_store().delete_session, request.cookies.get(SESSION_COOKIE, ""))
    settings = Settings.from_env()
    client_id, _, web = settings.client(surface)
    logout_url = f"{settings.domain}/logout?{urlencode({'client_id': client_id, 'logout_uri': web})}"
    response = JSONResponse({"logoutUrl": logout_url})
    response.delete_cookie(SESSION_COOKIE, path="/api")
    return response


@router.post("/admin/users/{subject}/revoke-admin")
async def revoke_admin(subject: str, request: Request) -> dict:
    """Project-admin action: deny future admin logins and all existing sessions."""
    session = await _session(request, "admin")
    _csrf(request, session)
    if not subject or len(subject) > 128:
        raise HTTPException(400, "대상 식별자가 올바르지 않습니다.")
    await asyncio.to_thread(get_store().block_admin, subject)
    return {"subject": subject, "adminAccess": "REVOKED"}


@router.post("/admin/users/{subject}/suspend")
async def suspend_user(subject: str, request: Request) -> dict:
    session = await _session(request, "admin")
    _csrf(request, session)
    if not subject or len(subject) > 128:
        raise HTTPException(400, "대상 식별자가 올바르지 않습니다.")
    await asyncio.to_thread(get_store().suspend_user, subject)
    return {"subject": subject, "account": "SUSPENDED"}
