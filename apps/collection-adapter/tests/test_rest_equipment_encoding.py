import unittest
import httpx
from foodtech_collection_adapter.rest import RestMockClient


class EquipmentEncodingTests(unittest.TestCase):
    def test_malformed_bytes_are_available_to_quarantine_only_when_enabled(self):
        for profile in ('mockprofile-110', 'mockprofile-120'):
            def handler(request):
                if request.url.path == '/oauth2/token':
                    return httpx.Response(200, json={'access_token': 'token', 'token_type': 'Bearer'})
                return httpx.Response(200, content=b'\xff\xfe\n', headers={
                    'X-Contract-Version': '1.0', 'X-Profile-ID': profile,
                    'X-Provenance': 'SYNTHETIC', 'X-Next-Cursor': '1'})
            for preserve in (False, True):
                client = httpx.Client(base_url='https://localhost', transport=httpx.MockTransport(handler))
                poller = RestMockClient('https://localhost', 'id', 'secret', 'ca', client,
                                        profile_id=profile, preserve_invalid_utf8=preserve)
                if preserve:
                    self.assertEqual(poller.poll().records, (b'\xff\xfe',))
                else:
                    with self.assertRaises(ValueError): poller.poll()
                poller.close()


if __name__ == '__main__': unittest.main()
