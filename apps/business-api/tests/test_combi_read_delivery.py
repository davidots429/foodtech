import unittest
from unittest.mock import Mock,patch

from foodtech_business_api.combi_read_delivery import CombiDeliverySettings,CombiDeliveryUnavailable,deliver_internal_combi_handoff


class CombiDeliverySettingsTests(unittest.TestCase):
    def test_destination_is_explicit_https_without_userinfo_path_query_or_control_characters(self):
        for url in ('http://localhost','https://user:password@localhost','https://localhost/path','https://localhost?key=value',
                    'https://localhost#fragment','https://localhost:0','https://localhost:99999','https://localhost\n',''):
            with self.subTest(url=url),self.assertRaises(ValueError):CombiDeliverySettings(url,'server','broker','key')
        settings=CombiDeliverySettings('https://gateway.foodtech.internal:8444/','server','broker','key')
        self.assertEqual(settings.gateway_url,'https://gateway.foodtech.internal:8444')
        self.assertEqual(repr(settings),'CombiDeliverySettings()')


class CombiDeliveryInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_untyped_frame_is_not_encoded_or_sent(self):
        frame=Mock()
        with patch('foodtech_business_api.combi_read_delivery.httpx.AsyncClient') as client:
            with self.assertRaisesRegex(CombiDeliveryUnavailable,'^Combi invocation unavailable$'):
                await deliver_internal_combi_handoff(frame,CombiDeliverySettings('https://localhost','server','broker','key'))
            frame._encode_for_internal_tls.assert_not_called();client.assert_not_called()
