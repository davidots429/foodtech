import unittest
from unittest.mock import patch
import httpx
from foodtech_collection_adapter import __main__ as cli
from foodtech_collection_adapter.rest import PollResult, RestMockClient


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

    def test_invalid_bytes_are_quarantined_before_cursor_advances(self):
        calls = []
        class Client:
            def __init__(self, *args, **kwargs):
                assert kwargs['preserve_invalid_utf8'] is True
            def poll(self, cursor): return PollResult((b'\xff\xfe',), cursor + 1)
            def close(self): pass
        class Ingestor:
            def __init__(self, *args): pass
            def cursor_state(self, key): return 0, set()
            def ingest(self, raw): raise AssertionError('invalid input entered normal pipeline')
            def save_cursor(self, *args): calls.append('cursor')
        class Sink:
            def __init__(self, *args): pass
            def preserve(self, raw, **kwargs): calls.append((raw, kwargs['reason'], kwargs['profile_id']))
        config = {name: 'test' for name in ('FOODTECH_MOCK_BASE_URL', 'FOODTECH_MOCK_CLIENT_ID',
            'FOODTECH_MOCK_CLIENT_SECRET', 'FOODTECH_MOCK_CA_FILE', 'FOODTECH_DATA_TABLE',
            'FOODTECH_ORDERED_QUEUE_URL', 'FOODTECH_INGESTION_DLQ_BUCKET')}
        for profile in ('110', '120'):
            instance = f'mockprofile-{profile}-b-001'
            with self.subTest(profile=profile), patch.dict('os.environ', config), \
                 patch.object(cli, 'RestMockClient', Client), patch.object(cli, 'DynamoOrderedIngestor', Ingestor), \
                 patch.object(cli, 'IngestionQuarantine', Sink), \
                 patch('sys.argv', ['collector', '--profile', f'mockprofile-{profile}', '--instance', instance]):
                self.assertEqual(cli.main(), 0)
                self.assertEqual(calls, [(b'\xff\xfe', 'UNSUPPORTED_ENCODING', f'mockprofile-{profile}'), 'cursor'])
                calls.clear()
                with patch.object(Sink, 'preserve', side_effect=RuntimeError('storage failed')):
                    with self.assertRaisesRegex(RuntimeError, 'storage failed'): cli.main()
                self.assertEqual(calls, [])


if __name__ == '__main__': unittest.main()
