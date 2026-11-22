"""Loopback-only UI harness; authentication and account storage are synthetic.

No AWS, Cognito, email delivery or AI provider is contacted. The real business
API and built customer UI run against explicit in-memory test doubles.
"""
import argparse
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from unittest.mock import patch

import uvicorn
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from foodtech_business_api import auth, withdrawal_api
from foodtech_business_api.auth_store import MemoryStore, Session
from foodtech_business_api.main import app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8136)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('use a non-privileged loopback port')
    root = Path(__file__).resolve().parents[2]
    dist = root / 'apps/customer-web/dist'
    if not (dist / 'index.html').is_file():
        parser.error('build the customer web application first')
    origin = f'http://127.0.0.1:{args.port}'
    subject = str(uuid4())
    store = MemoryStore()
    store.principals[subject] = {'epoch': 0}
    environment = {
        'FOODTECH_COGNITO_ISSUER': 'https://cognito-idp.ap-southeast-2.amazonaws.com/synthetic',
        'FOODTECH_COGNITO_DOMAIN': 'https://synthetic.example.invalid',
        'FOODTECH_CUSTOMER_CLIENT_ID': 'synthetic-customer', 'FOODTECH_ADMIN_CLIENT_ID': 'synthetic-admin',
        'FOODTECH_CUSTOMER_CALLBACK': origin + '/api/auth/customer/callback',
        'FOODTECH_ADMIN_CALLBACK': origin + '/api/auth/admin/callback',
        'FOODTECH_CUSTOMER_WEB': origin, 'FOODTECH_ADMIN_WEB': origin,
        'FOODTECH_SECURE_COOKIES': 'false', 'FOODTECH_ACCOUNT_WITHDRAWAL_ENABLED': 'true',
        'FOODTECH_AUTH_REFRESH_KEY_ARN': '', 'FOODTECH_COOKING_TABLE': '', 'FOODTECH_TIER_TABLE': '',
    }
    original_flow = auth._flow
    async def fixture_flow(surface, settings, **kwargs):
        response = await original_flow(surface, settings, **kwargs)
        state = parse_qs(urlparse(response.headers['location']).query)['state'][0]
        response.headers['location'] = '/fixture/identity?state=' + state
        return response
    async def fixture_exchange(*_):
        return {'id_token': 'synthetic-id', 'access_token': 'synthetic-access'}
    def fixture_claims(token, settings, client_id, use):
        return {'sub': subject, 'email': 'synthetic@example.invalid', 'auth_time': int(time.time()),
                'exp': int(time.time()) + 3600}

    @app.get('/fixture/start')
    async def start():
        if store.principal(subject).get('suspended'):
            return HTMLResponse('Synthetic account is pending withdrawal; use cancellation authentication.', 409)
        session_id = str(uuid4())
        store.save_session(session_id, Session('customer', subject, 'synthetic@example.invalid',
            'synthetic-csrf', int(time.time()) + 3600, int(store.principal(subject)['epoch'])))
        response = RedirectResponse('/', 303)
        auth._cookie(response, auth.SESSION_COOKIE, session_id, 3600, False)
        return response

    @app.get('/fixture/identity')
    async def identity(state: str):
        # State is a server-generated urlsafe token, still escape before HTML.
        import html
        value = html.escape(state, quote=True)
        return HTMLResponse('<h1>합성 본인 인증 시험</h1><p>실제 Cognito 인증이 아닙니다.</p>'
            '<form action="/api/auth/customer/callback"><input type="hidden" name="state" value="' + value + '">'
            '<input type="hidden" name="code" value="synthetic"><button>합성 인증 완료</button></form>')

    app.mount('/', StaticFiles(directory=dist, html=True))
    with patch.dict(os.environ, environment), patch.object(auth, 'get_store', return_value=store), \
            patch.object(withdrawal_api, 'get_store', return_value=store), \
            patch.object(auth, '_cognito_access', return_value=True), \
            patch.object(auth, '_flow', side_effect=fixture_flow), \
            patch.object(auth, '_exchange', side_effect=fixture_exchange), \
            patch.object(auth, '_claims', side_effect=fixture_claims):
        print(f'Synthetic withdrawal UI: {origin}/fixture/start', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
