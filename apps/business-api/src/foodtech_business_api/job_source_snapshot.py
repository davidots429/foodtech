"""Inspect protected META snapshots without treating a batch as customer-owned.

The current collection keys do not establish tenant ownership. A bound source
therefore requires an independent authority before source deletion or a global
completion receipt. No batch identifiers are returned or logged here.
"""
import base64
from boto3.dynamodb.types import TypeDeserializer


class JobSourceSnapshot:
    def __init__(self, *, inventory):
        self.inventory=inventory
        self.store,self.cipher=inventory.progress_store,inventory.cipher

    @staticmethod
    def _restore(value):
        if isinstance(value,dict):
            if set(value)=={'$binary'} and isinstance(value['$binary'],str):
                return base64.b64decode(value['$binary'],validate=True)
            return {key:JobSourceSnapshot._restore(item) for key,item in value.items()}
        if isinstance(value,list):return [JobSourceSnapshot._restore(item) for item in value]
        return value

    def inspect(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:raise ValueError('invalid job snapshot index')
        target=next((x for n,x in enumerate(self.inventory.targets(subject=subject,request_id=request_id)) if n==index),None)
        if target is None:raise ValueError('job snapshot target unavailable')
        saved=self.store.load_checkpoint(cipher=self.cipher,subject=subject,request_id=request_id,
                                         domain=f'JOB_META_{index:012d}')
        try:
            if not saved or saved['version']!=1 or saved['state']!='IN_PROGRESS':raise ValueError
            snap=self.inventory._json(saved['checkpoint'])
            if set(snap)!={'schema','job_id','version','metadata'} or type(snap['schema']) is not int or \
                    snap['schema']!=1 or snap['job_id']!=target.job_id or type(snap['version']) is not int or \
                    not 1 <= snap['version'] < 10**18 or not isinstance(snap['metadata'],dict):raise ValueError
            decoder=TypeDeserializer()
            meta={key:decoder.deserialize(self._restore(value)) for key,value in snap['metadata'].items()}
            if meta.get('pk')!=f'SCOPE#USER#{subject}#JOB#{target.job_id}' or meta.get('sk')!='META' or \
                    meta.get('owner_subject')!=subject or meta.get('job_id')!=target.job_id or \
                    meta.get('version')!=snap['version']:raise ValueError
            binding=meta.get('source_binding')
            if binding is not None and not isinstance(binding,dict):raise ValueError
        except Exception:
            raise ValueError('job snapshot unavailable') from None
        return {'state':'SOURCE_OWNERSHIP_REQUIRED' if binding is not None else 'NO_BOUND_SOURCE'}
