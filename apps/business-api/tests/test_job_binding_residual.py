import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from foodtech_business_api.job_binding_residual import JobBindingResidualScan
from test_profile_deletion_inventory import Progress


class BindingResidualTests(unittest.TestCase):
    def test_remaining_binding_blocks_until_rechecked(self):
        subject,request=str(uuid4()),str(uuid4());source=Mock()
        source.name='cooking';source.table_arn='arn:source';progress=Progress()
        scan=JobBindingResidualScan(source_table=source,progress_store=progress,cipher=SimpleNamespace(source_table_arn='arn:source'))
        row={'pk':'JEYUK#jeyuk-test','sk':'BINDING','job_pk':f'SCOPE#USER#{subject}#JOB#job','job_id':'job'}
        source.scan.side_effect=[{'Items':[row],'ScannedCount':1},{'Items':[],'ScannedCount':1}]
        scope={'subject':subject,'request_id':request,'limit':100}
        self.assertEqual(scan.observe_page(**scope),{'scan_complete':False,'residual_found':True})
        with self.assertRaises(ValueError):scan.repair_one(**scope)
        self.assertEqual(scan.observe_page(**scope),{'scan_complete':True,'residual_found':False})
        self.assertEqual(source.scan.call_args.kwargs['Limit'],20)


if __name__=='__main__':unittest.main()
