import time
import asyncio
import unittest
from unittest.mock import patch

import httpx

from foodtech_business_api import auth
from foodtech_business_api.auth_store import MemoryStore, Session
from foodtech_business_api.main import app


ENV = {
    'FOODTECH_COGNITO_ISSUER': 'https://cognito-idp.ap-southeast-2.amazonaws.com/test',
    'FOODTECH_COGNITO_DOMAIN': 'https://example.auth.ap-southeast-2.amazoncognito.com',
    'FOODTECH_CUSTOMER_CLIENT_ID': 'customer', 'FOODTECH_ADMIN_CLIENT_ID': 'admin',
    'FOODTECH_CUSTOMER_CALLBACK': 'http://localhost/callback', 'FOODTECH_ADMIN_CALLBACK': 'http://localhost/admin',
    'FOODTECH_CUSTOMER_WEB': 'http://localhost/customer', 'FOODTECH_ADMIN_WEB': 'http://localhost/admin',
    'FOODTECH_SECURE_COOKIES': 'false',
}


class FakeCipher:
    def open(self, ciphertext, session_id, surface):
        assert ciphertext == b'encrypted'
        return bytearray(b'old-refresh')

    def seal(self, token, session_id, surface):
        assert token == b'new-refresh'
        token[:] = b'\x00' * len(token)
        return b'rotated-ciphertext'


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict('os.environ', ENV, clear=True); self.env.start()
        self.store = MemoryStore()
        now = int(time.time())
        self.store.save_session('session-id', Session('customer', 'subject', None, 'csrf', now + 800, 0,
            access_token='old-access', refresh_ciphertext=b'encrypted', refresh_deadline=now + 800))
        self.store_patch = patch.object(auth, 'get_store', return_value=self.store); self.store_patch.start()
        self.cipher_patch = patch.object(auth, '_refresh_cipher', return_value=FakeCipher()); self.cipher_patch.start()
        self.access_patch = patch.object(auth, '_cognito_access', return_value=True); self.access_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                        cookies={auth.SESSION_COOKIE: 'session-id'})

    async def asyncTearDown(self):
        await self.client.aclose()
        self.access_patch.stop(); self.cipher_patch.stop(); self.store_patch.stop(); self.env.stop()

    async def test_refresh_rotates_without_exposing_tokens(self):
        tokens = {'IdToken': 'new-id', 'AccessToken': 'new-access', 'RefreshToken': 'new-refresh'}
        claims = [{'sub': 'subject'}, {'sub': 'subject', 'exp': time.time() + 400}]
        with patch.object(auth, '_cognito_refresh', return_value=tokens), patch.object(auth, '_claims', side_effect=claims):
            denied = await asyncio.wait_for(self.client.post('/api/customer/session/refresh'), 3)
            self.assertEqual(denied.status_code, 403)
            response = await asyncio.wait_for(self.client.post('/api/customer/session/refresh', headers={'X-CSRF-Token': 'csrf'}), 3)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('new-refresh', response.text)
        self.assertNotIn('new-access', response.text)
        self.assertEqual(self.store.sessions['session-id'].refresh_ciphertext, b'rotated-ciphertext')
        self.assertEqual(self.store.sessions['session-id'].refresh_version, 1)

    async def test_logout_during_cognito_request_cannot_restore_session(self):
        def refresh(*_):
            self.store.delete_session('session-id')
            return {'IdToken': 'id', 'AccessToken': 'access', 'RefreshToken': 'new-refresh'}
        claims = [{'sub': 'subject'}, {'sub': 'subject', 'exp': time.time() + 400}]
        with patch.object(auth, '_cognito_refresh', side_effect=refresh), patch.object(auth, '_claims', side_effect=claims), \
             patch.object(auth, '_cognito_revoke') as revoke:
            response = await asyncio.wait_for(self.client.post('/api/customer/session/refresh', headers={'X-CSRF-Token': 'csrf'}), 3)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn('session-id', self.store.sessions)
        revoke.assert_called_once_with('customer', 'new-refresh')

    async def test_expired_access_may_only_use_refresh_path(self):
        self.store.sessions['session-id'].access_expires = int(time.time()) - 1
        self.assertEqual((await self.client.get('/api/customer/me')).status_code, 401)
        tokens = {'IdToken': 'id', 'AccessToken': 'access', 'RefreshToken': 'new-refresh'}
        claims = [{'sub': 'subject'}, {'sub': 'subject', 'exp': time.time() + 400}]
        with patch.object(auth, '_cognito_refresh', return_value=tokens), patch.object(auth, '_claims', side_effect=claims):
            result = await self.client.post('/api/customer/session/refresh', headers={'X-CSRF-Token': 'csrf'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual((await self.client.get('/api/customer/me')).status_code, 200)

    async def test_logout_deletes_first_and_revokes_latest_token(self):
        def revoke(client_id, token):
            self.assertNotIn('session-id', self.store.sessions)
            self.assertEqual((client_id, token), ('customer', 'old-refresh'))
        with patch.object(auth, '_cognito_revoke', side_effect=revoke):
            response = await self.client.post('/api/customer/logout', headers={'X-CSRF-Token': 'csrf'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['cognitoTokenRevoked'])
        self.assertNotIn('old-refresh', response.text)

    async def test_logout_still_deletes_if_revocation_fails(self):
        with patch.object(auth, '_cognito_revoke', side_effect=ValueError('failure')):
            response = await self.client.post('/api/customer/logout', headers={'X-CSRF-Token': 'csrf'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['cognitoTokenRevoked'])
        self.assertNotIn('session-id', self.store.sessions)
