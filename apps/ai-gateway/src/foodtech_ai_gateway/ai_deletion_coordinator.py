"""Bounded AI deletion preparation; not caller authentication or completion.

The caller must stop legacy writers and reconcile external calls before any
mutation. CLEAR_OBSERVED is a point-in-time observation, not quiescence proof.
Credential deletion and all-store/backup verification remain separate.
"""
import json


class AiDeletionCoordinator:
    def __init__(self, *, capture, worker, observer, credentials):
        if worker.capture is not capture or observer.capture is not capture or credentials.capture is not capture or \
                any(component.progress_store is not capture.progress_store or component.source_table is not capture.source_table
                    for component in (worker,observer,credentials)) or \
                worker.cipher is not capture.cipher or credentials.cipher is not capture.cipher:
            raise ValueError('AI deletion dependencies do not match')
        self.capture,self.worker,self.observer,self.credentials=capture,worker,observer,credentials
        self.progress,self.cipher=capture.progress_store,capture.cipher

    def step(self, *, subject, request_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:raise ValueError('invalid AI deletion limit')
        scope={'subject':subject,'request_id':request_id,'domain':'AI_COORDINATOR'}
        saved=self.capture._load(subject,request_id,scope['domain'])
        if saved is None:
            data,version={'schema':1,'phase':'CAPTURE','index':0,'total':0},0
        else:
            data,version=self.capture._json(saved['checkpoint']),saved['version']
            if set(data)!={'schema','phase','index','total'} or type(data['schema']) is not int or data['schema']!=1 or \
                    data['phase'] not in {'CAPTURE','PARTITIONS','CREDENTIALS','VERIFYING'} or \
                    type(data['index']) is not int or type(data['total']) is not int or \
                    not 0 <= data['index'] <= data['total'] < 10**12 or \
                    (data['phase']=='CAPTURE' and (data['index']!=0 or data['total']!=0)) or \
                    (data['phase']=='VERIFYING' and data['index']!=data['total']) or \
                    type(version) is not int or version < 1 or \
                    saved['state'] != ('VERIFYING' if data['phase']=='VERIFYING' else 'IN_PROGRESS'):
                raise ValueError('invalid AI coordinator checkpoint')
        phase=data['phase']
        if phase=='VERIFYING':return {'phase':'VERIFYING'}
        if phase=='CAPTURE':
            result=self.capture.capture_page(subject=subject,request_id=request_id,limit=limit)
            if result['complete']:data.update(phase='PARTITIONS',total=result['count'])
        else:
            if data['index'] < data['total']:
                target=next((x for n,x in enumerate(self.capture.partitions(subject=subject,request_id=request_id))
                             if n==data['index']),None)
                if target is None:raise ValueError('AI inventory target unavailable')
                is_credential=target.partition.startswith('CREDENTIAL#')
                if phase=='PARTITIONS' and not is_credential:
                    observation=self.observer.observe(subject=subject,request_id=request_id)
                    if observation.get('observation')=='BLOCKED':
                        return {'phase':'PARTITIONS','execution_blocked':True}
                    if observation.get('observation')!='CLEAR_OBSERVED':raise ValueError('invalid execution observation')
                    result=self.worker.run_page(subject=subject,request_id=request_id,index=data['index'],limit=limit)
                    if result['state']=='VERIFYING':data['index']+=1
                    elif result['state']!='IN_PROGRESS':raise ValueError('invalid AI partition result')
                elif phase=='CREDENTIALS' and is_credential:
                    result=self.credentials.observe_readiness(observer=self.observer,subject=subject,
                                                               request_id=request_id,index=data['index'])
                    if result.get('observation') not in {'BLOCKED','CLEAR_OBSERVED'}:
                        raise ValueError('invalid credential readiness')
                    return {'phase':'CREDENTIALS','credential_settlement_required':True,
                            'execution_blocked':result['observation']=='BLOCKED'}
                else:data['index']+=1
            if data['index']==data['total']:
                if phase=='PARTITIONS':data.update(phase='CREDENTIALS',index=0)
                else:data['phase']='VERIFYING'
        raw=json.dumps(data,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')
        self.progress.save_checkpoint(cipher=self.cipher,**scope,expected_version=version,
            state='VERIFYING' if data['phase']=='VERIFYING' else 'IN_PROGRESS',checkpoint=raw)
        return {'phase':data['phase']}
