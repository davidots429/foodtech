import os
import time
import unittest
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from unittest.mock import patch

import httpx

from foodtech_business_api import auth, withdrawal_api
from foodtech_business_api.auth_store import MemoryStore, Session
from foodtech_business_api.main import app
from foodtech_business_api.principal_enrollment import PrincipalEnrollmentConflict
from test_auth import ENV


class WithdrawalApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subject = str(uuid4())
        self.store = MemoryStore()
        self.store.principals[self.subject] = {'epoch': 0}
        self.store.save_session('existing', Session('customer', self.subject, 'synthetic@example.invalid',
                                                  'original-csrf', int(time.time()) + 300, 0))
        self.environment = patch.dict(os.environ, {**ENV, 'FOODTECH_ACCOUNT_WITHDRAWAL_ENABLED': 'true',
                                                   'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true'})
        self.environment.start()
        self.patches = [patch.object(auth, 'get_store', return_value=self.store),
                        patch.object(withdrawal_api, 'get_store', return_value=self.store),
                        patch.object(auth, '_cognito_access', return_value=True)]
        for context in self.patches:
            context.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url='http://127.0.0.1:8000', follow_redirects=False)
        self.client.cookies.set(auth.SESSION_COOKIE, 'existing', domain='127.0.0.1', path='/api')

    async def asyncTearDown(self):
        await self.client.aclose()
        for context in reversed(self.patches):
            context.stop()
        self.environment.stop()

    async def request_withdrawal(self):
        return await self.client.post('/api/customer/withdrawal/request',
            headers={'X-CSRF-Token': 'original-csrf'}, json={'request_id': str(uuid4()), 'confirmed': True})

    async def recover(self, *, auth_time=None, subject=None):
        start = await self.client.get('/api/customer/withdrawal/login')
        self.assertEqual(start.status_code, 303)
        query = parse_qs(urlparse(start.headers['location']).query)
        self.assertEqual((query['prompt'], query['max_age']), (['login'], ['0']))
        now = int(time.time())
        claims = [{'sub': subject or self.subject, 'auth_time': now if auth_time is None else auth_time},
                  {'sub': subject or self.subject, 'exp': now + 300}]
        async def exchange(*_):
            return {'id_token': 'synthetic-id', 'access_token': 'synthetic-access'}
        with patch.object(auth, '_exchange', side_effect=exchange), patch.object(auth, '_claims', side_effect=claims):
            return await self.client.get('/api/auth/customer/callback',
                params={'code': 'synthetic', 'state': query['state'][0]})

    async def test_request_and_fresh_recovery_cancel_without_general_access(self):
        request = await self.request_withdrawal()
        self.assertEqual(request.status_code, 200)
        self.assertEqual(request.json()['cancel_before'] - request.json()['requested_at'], 604800)
        self.assertEqual(request.headers['Cache-Control'], 'no-store')
        # Even a retained copy of the old cookie loses authorization immediately.
        self.client.cookies.clear()
        self.client.cookies.set(auth.SESSION_COOKIE, 'existing', domain='127.0.0.1', path='/api')
        self.assertEqual((await self.client.get('/api/customer/me')).status_code, 401)
        self.assertEqual((await self.recover()).status_code, 303)
        for path in ('/api/customer/me', '/api/customer/example', '/api/admin/me'):
            self.assertEqual((await self.client.get(path)).status_code, 401)
        status = await self.client.get('/api/customer/withdrawal/status')
        self.assertEqual(status.status_code, 200)
        csrf = status.json()['csrf']
        self.assertEqual((await self.client.post('/api/customer/withdrawal/cancel')).status_code, 403)
        canceled = await self.client.post('/api/customer/withdrawal/cancel', headers={'X-CSRF-Token': csrf})
        self.assertEqual(canceled.json(), {'status': 'CANCELED', 'login_required': True})
        self.assertFalse(self.store.principal(self.subject)['suspended'])
        self.assertEqual(self.store.principal(self.subject)['epoch'], 2)
        self.assertEqual((await self.client.get('/api/customer/me')).status_code, 401)

    async def test_old_login_cookie_or_other_subject_cannot_cancel(self):
        self.assertEqual((await self.request_withdrawal()).status_code, 200)
        self.assertEqual((await self.recover(auth_time=int(time.time()) - 600)).status_code, 401)
        other = str(uuid4())
        self.store.principals[other] = {'epoch': 0}
        self.assertEqual((await self.recover(subject=other)).status_code, 403)
        self.assertTrue(self.store.principal(self.subject)['suspended'])

    async def test_recovery_at_deadline_is_denied_and_pending_remains_locked(self):
        await self.request_withdrawal()
        self.store.principals[self.subject]['withdrawal_requested_at'] -= 604800
        self.store.principals[self.subject]['withdrawal_deadline'] -= 604800
        self.assertEqual((await self.recover()).status_code, 403)
        self.assertTrue(self.store.principal(self.subject)['suspended'])

    async def test_suspension_after_recovery_blocks_cancel(self):
        await self.request_withdrawal()
        await self.recover()
        status = (await self.client.get('/api/customer/withdrawal/status')).json()
        self.store.suspend_user(self.subject)
        response = await self.client.post('/api/customer/withdrawal/cancel', headers={'X-CSRF-Token': status['csrf']})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.store.principal(self.subject)['suspended'])

    async def test_request_requires_csrf_confirmation_and_current_epoch(self):
        body = {'request_id': str(uuid4()), 'confirmed': True}
        self.assertEqual((await self.client.post('/api/customer/withdrawal/request', json=body)).status_code, 403)
        self.assertEqual((await self.client.post('/api/customer/withdrawal/request',
            headers={'X-CSRF-Token': 'original-csrf'}, json={**body, 'confirmed': False})).status_code, 422)
        original = self.store.withdrawal_transition
        def race(**kwargs):
            self.store.principals[self.subject]['epoch'] += 1
            return original(**kwargs)
        self.store.withdrawal_transition = race
        self.assertEqual((await self.request_withdrawal()).status_code, 409)
        self.assertNotIn('withdrawal_status', self.store.principal(self.subject))

    async def test_missing_enrollment_control_prevents_new_withdrawal(self):
        with patch.object(self.store, 'enrollment_fence', side_effect=PrincipalEnrollmentConflict('unavailable')):
            self.assertEqual((await self.request_withdrawal()).status_code, 503)
        self.assertNotIn('withdrawal_status', self.store.principal(self.subject))

    async def test_disabled_flow_and_missing_principal_fail_closed(self):
        with patch.dict(os.environ, {'FOODTECH_ACCOUNT_WITHDRAWAL_ENABLED': 'false'}):
            self.assertEqual((await self.client.get('/api/customer/withdrawal/options')).json(), {'enabled': False})
            self.assertEqual((await self.client.get('/api/customer/withdrawal/login')).status_code, 404)
            self.assertEqual((await self.request_withdrawal()).status_code, 404)
        self.store.principals.clear()
        self.assertEqual((await self.request_withdrawal()).status_code, 401)
        self.assertEqual(self.store.principals, {})


if __name__ == '__main__':
    unittest.main()
