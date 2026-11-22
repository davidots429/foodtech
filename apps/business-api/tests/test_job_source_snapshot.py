import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.job_source_snapshot import JobSourceSnapshot


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.subject,self.request=str(uuid4()),str(uuid4());self.store=SimpleNamespace(load_checkpoint=Mock())
        self.inventory=SimpleNamespace(progress_store=self.store,cipher=object(),targets=lambda **kw:iter([SimpleNamespace(job_id='job')]),
                                       _json=json.loads)
        self.inspector=JobSourceSnapshot(inventory=self.inventory)
        self.scope={'subject':self.subject,'request_id':self.request,'index':0}
        self.meta={'pk':f'SCOPE#USER#{self.subject}#JOB#job','sk':'META','owner_subject':self.subject,
                   'job_id':'job','version':1,'source_binding':{'batch_id':'private-batch'}}

    def save(self,meta):
        encoder=TypeSerializer();snapshot={'schema':1,'job_id':'job','version':1,
                                           'metadata':{k:encoder.serialize(v) for k,v in meta.items()}}
        self.store.load_checkpoint.return_value={'state':'IN_PROGRESS','version':1,'checkpoint':json.dumps(snapshot).encode()}

    def test_binding_requires_ownership_and_never_exposes_batch(self):
        self.save(self.meta);result=self.inspector.inspect(**self.scope)
        self.assertEqual(result,{'state':'SOURCE_OWNERSHIP_REQUIRED'});self.assertNotIn('private-batch',repr(result))
        self.save({**self.meta,'source_binding':None})
        self.assertEqual(self.inspector.inspect(**self.scope),{'state':'NO_BOUND_SOURCE'})

    def test_missing_or_changed_owner_fails_closed(self):
        self.store.load_checkpoint.return_value=None
        with self.assertRaises(ValueError):self.inspector.inspect(**self.scope)
        self.save({**self.meta,'owner_subject':str(uuid4())})
        with self.assertRaises(ValueError):self.inspector.inspect(**self.scope)
        self.save({**self.meta,'source_binding':'bad'})
        with self.assertRaises(ValueError):self.inspector.inspect(**self.scope)

    def test_batch_stops_on_later_bound_source_without_exposing_it(self):
        self.inventory.targets=lambda **kw:iter([SimpleNamespace(job_id='job'),SimpleNamespace(job_id='second')])
        encoder=TypeSerializer()
        def saved(job, binding):
            meta={**self.meta,'pk':f'SCOPE#USER#{self.subject}#JOB#{job}','job_id':job,'source_binding':binding}
            return {'state':'IN_PROGRESS','version':1,'checkpoint':json.dumps(
                {'schema':1,'job_id':job,'version':1,'metadata':{k:encoder.serialize(v) for k,v in meta.items()}}).encode()}
        self.store.load_checkpoint.side_effect=[saved('job',None),saved('second',{'batch_id':'private'})]
        result=self.inspector.inspect_batch(subject=self.subject,request_id=self.request,start=0,count=2)
        self.assertEqual(result,{'state':'SOURCE_OWNERSHIP_REQUIRED'})
        self.assertNotIn('private',repr(result))


if __name__=='__main__':unittest.main()
