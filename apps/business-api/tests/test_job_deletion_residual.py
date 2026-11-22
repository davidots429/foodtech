import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from foodtech_business_api.job_deletion_residual import JobResidualScan, JobFinalResidualScan
from test_profile_deletion_inventory import Progress


class JobResidualTests(unittest.TestCase):
    def setUp(self):
        self.subject=str(uuid4());self.request=str(uuid4());self.source=Mock()
        self.source.name='cooking';self.source.table_arn='arn:source';self.progress=Progress()
        self.scan=JobResidualScan(source_table=self.source,progress_store=self.progress,cipher=SimpleNamespace(source_table_arn='arn:source'))
        self.scope={'subject':self.subject,'request_id':self.request,'limit':20}

    def test_orphan_record_blocks_until_same_page_is_clean(self):
        row={'pk':f'SCOPE#USER#{self.subject}#JOB#orphan','sk':'EVENT#1'}
        self.source.scan.side_effect=[{'Items':[row],'ScannedCount':1},{'Items':[],'ScannedCount':1}]
        self.assertEqual(self.scan.observe_page(**self.scope),{'scan_complete':False,'residual_found':True})
        with self.assertRaises(ValueError):self.scan.repair_one(**self.scope)
        self.assertEqual(self.scan.observe_page(**self.scope),{'scan_complete':True,'residual_found':False})
        self.assertNotIn('ExclusiveStartKey',self.source.scan.call_args.kwargs)

    def test_final_scan_detects_late_meta(self):
        scan=JobFinalResidualScan(source_table=self.source,progress_store=self.progress,cipher=self.scan.cipher)
        self.source.scan.return_value={'Items':[{'pk':f'SCOPE#USER#{self.subject}#JOB#late','sk':'META'}],
                                       'ScannedCount':1}
        self.assertEqual(scan.observe_page(**self.scope),{'scan_complete':False,'residual_found':True})

    def test_unexpected_key_cannot_be_treated_as_clean(self):
        self.source.scan.return_value={'Items':[{'pk':f'SCOPE#USER#{self.subject}#JOB#bad#suffix','sk':'EVENT#1'}],
                                       'ScannedCount':1}
        with self.assertRaises(ValueError):self.scan.observe_page(**self.scope)
        self.assertFalse(self.progress.items)


if __name__=='__main__':unittest.main()
