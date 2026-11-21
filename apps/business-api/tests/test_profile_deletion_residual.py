import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from foodtech_business_api.legacy_profile_scan import LegacyProfilePage
from foodtech_business_api.profile_deletion_residual import ProfileResidualScan
from test_profile_deletion_inventory import Progress


class ResidualTests(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(name='cooking', table_arn='arn:source')
        self.progress = Progress()
        self.cipher = SimpleNamespace(source_table_arn='arn:source')
        self.scan = ProfileResidualScan(source_table=self.source, progress_store=self.progress, cipher=self.cipher)
        self.scope = {'subject': str(uuid4()), 'request_id': str(uuid4()), 'limit': 10}

    def test_resumes_until_source_scan_complete_without_claiming_deletion(self):
        pages = [LegacyProfilePage((), {'pk':'other', 'sk':'key'}, 2), LegacyProfilePage((), None, 1)]
        with patch('foodtech_business_api.profile_deletion_residual.scan_legacy_profiles', side_effect=pages) as scan:
            self.assertEqual(self.scan.observe_page(**self.scope), {'scan_complete':False, 'residual_found':False})
            self.assertEqual(self.scan.observe_page(**self.scope), {'scan_complete':True, 'residual_found':False})
            self.assertEqual(self.scan.observe_page(**self.scope), {'scan_complete':True, 'residual_found':False})
            self.assertEqual(scan.call_count, 2)
            self.assertEqual(scan.call_args.kwargs['checkpoint'], {'pk':'other','sk':'key'})
        self.assertEqual(next(iter(self.progress.items.values()))['state'], 'VERIFYING')

    def test_residual_fails_closed_and_no_identifiers_returned(self):
        with patch('foodtech_business_api.profile_deletion_residual.scan_legacy_profiles',
                   side_effect=[LegacyProfilePage((('secret','v1'),), None, 1), LegacyProfilePage((), None, 1)]) as scan:
            self.assertEqual(self.scan.observe_page(**self.scope), {'scan_complete':False, 'residual_found':True})
            self.assertEqual(self.scan.observe_page(**self.scope), {'scan_complete':True, 'residual_found':False})
            self.assertEqual(scan.call_count, 2)

    def test_bad_checkpoint_and_nonadvancing_cursor_rejected(self):
        key=(self.scope['subject'],self.scope['request_id'],'PROFILE_RESIDUAL')
        self.progress.items[key]={'version':1,'state':'IN_PROGRESS','checkpoint':json.dumps(
            {'schema':1,'pages':1,'cursor':{'pk':'x','sk':'y'},'complete':False,'residual':False}).encode()}
        with patch('foodtech_business_api.profile_deletion_residual.scan_legacy_profiles',
                   return_value=LegacyProfilePage((), {'pk':'x','sk':'y'}, 1)):
            with self.assertRaises(ValueError): self.scan.observe_page(**self.scope)
        self.progress.items[key]['checkpoint']=b'{"schema":true}'
        with self.assertRaises(ValueError): self.scan.observe_page(**self.scope)


if __name__ == '__main__': unittest.main()
