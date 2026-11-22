import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.job_deletion_inventory import JobDeletionInventory
from test_profile_deletion_inventory import Progress


class JobInventoryTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.source=Mock(); self.source.name='cooking'; self.source.table_arn='arn:source'
        self.progress=Progress(); self.cipher=SimpleNamespace(source_table_arn='arn:source')
        self.inventory=JobDeletionInventory(source_table=self.source,progress_store=self.progress,cipher=self.cipher)
        self.scope={'subject':self.subject,'request_id':self.request}
        self.row={'pk':f'SCOPE#USER#{self.subject}#JOB#job','sk':'META','job_id':'job','owner_subject':self.subject}

    def test_inventory_resumes_and_validates_all_pages_before_yielding(self):
        self.source.scan.side_effect=[{'Items':[],'ScannedCount':2,'LastEvaluatedKey':{'pk':'other','sk':'key'}},
                                      {'Items':[self.row],'ScannedCount':1}]
        self.assertFalse(self.inventory.capture_page(**self.scope,limit=20)['complete'])
        self.assertEqual(self.inventory.capture_page(**self.scope,limit=20)['count'],1)
        targets=list(self.inventory.targets(**self.scope))
        self.assertEqual(targets[0].job_id,'job'); self.assertNotIn('job',repr(targets))
        del self.progress.items[(self.subject,self.request,'JOB_INVENTORY_PAGE_000000000002')]
        with self.assertRaises(ValueError): next(self.inventory.targets(**self.scope))

    def test_bad_owner_and_malformed_source_are_not_captured(self):
        for row in ({**self.row,'owner_subject':str(uuid4())},{**self.row,'pk':self.row['pk']+'#extra'},
                    {**self.row,'job_id':'bad#id'}):
            self.source.scan.return_value={'Items':[row],'ScannedCount':1}
            with self.assertRaises(ValueError): self.inventory.capture_page(**self.scope,limit=20)
        self.assertFalse(self.progress.items)


if __name__ == '__main__': unittest.main()
