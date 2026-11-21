"""Resumable job-record and reverse-binding cleanup, not a completion receipt.

An authenticated worker must keep all legacy writers stopped. Job META, listing
references, residual checks and other stores require separate handling.
"""
import json
from .job_listing_deletion import clean_one_job_listing


class JobDeletionCoordinator:
    def __init__(self, *, jobs, worker, bindings, residual, binding_residual):
        if worker.inventory is not jobs or bindings.source_table is not jobs.source_table or \
                bindings.progress_store is not jobs.progress_store or bindings.cipher is not jobs.cipher or \
                residual.source_table is not jobs.source_table or residual.progress_store is not jobs.progress_store or \
                residual.cipher is not jobs.cipher or binding_residual.source_table is not jobs.source_table or \
                binding_residual.progress_store is not jobs.progress_store or binding_residual.cipher is not jobs.cipher:
            raise ValueError('job deletion dependencies do not match')
        self.jobs,self.worker,self.bindings,self.residual,self.binding_residual=jobs,worker,bindings,residual,binding_residual
        self.store,self.cipher=jobs.progress_store,jobs.cipher

    def step(self, *, subject, request_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid job deletion limit')
        scope={'subject':subject,'request_id':request_id,'domain':'JOB_COORDINATOR'}
        item=self.jobs._load(subject,request_id,scope['domain'])
        if item is None:
            state,version={'schema':1,'phase':'JOBS','index':0,'total':0},0
        else:
            state,version=self.jobs._json(item['checkpoint']),item['version']
            if set(state) != {'schema','phase','index','total'} or type(state['schema']) is not int or \
                    state['schema'] != 1 or state['phase'] not in {'JOBS','RECORDS','BINDINGS','REMOVE_BINDINGS','LISTINGS','RESIDUAL','BINDING_RESIDUAL','VERIFYING'} or \
                    type(state['index']) is not int or type(state['total']) is not int or \
                    not 0 <= state['index'] <= state['total'] < 10**12 or \
                    (state['phase'] in {'JOBS','BINDINGS'} and (state['index'] != 0 or state['total'] != 0)) or \
                    (state['phase'] in {'LISTINGS','RESIDUAL','BINDING_RESIDUAL','VERIFYING'} and state['index'] != state['total']) or \
                    type(version) is not int or version < 1 or \
                    item['state'] != ('VERIFYING' if state['phase']=='VERIFYING' else 'IN_PROGRESS'):
                raise ValueError('invalid job deletion checkpoint')
        phase=state['phase']
        if phase=='VERIFYING': return {'phase':'VERIFYING'}
        if phase=='JOBS':
            result=self.jobs.capture_page(subject=subject,request_id=request_id,limit=limit)
            if result['complete']:state.update(phase='RECORDS',total=result['count'])
        elif phase=='RECORDS':
            if state['index'] < state['total']:
                result=self.worker.delete_one(subject=subject,request_id=request_id,index=state['index'])
                if (result.get('state'),result.get('removed')) == ('META_ONLY',False):state['index']+=1
                elif (result.get('state'),result.get('removed')) != ('IN_PROGRESS',True):
                    raise ValueError('invalid job cleanup result')
            if state['index']==state['total']:state.update(phase='BINDINGS',index=0,total=0)
        elif phase=='BINDINGS':
            result=self.bindings.capture_page(subject=subject,request_id=request_id,limit=limit)
            if result['complete']:state.update(phase='REMOVE_BINDINGS',total=result['count'])
        elif phase=='REMOVE_BINDINGS':
            if state['index'] < state['total']:
                result=self.bindings.remove_one(subject=subject,request_id=request_id,index=state['index'])
                if result.get('state') != 'VERIFYING':raise ValueError('invalid binding cleanup result')
                state['index']+=1
            if state['index']==state['total']:state['phase']='LISTINGS'
        elif phase=='LISTINGS':
            result=clean_one_job_listing(source_table=self.jobs.source_table,progress_store=self.store,
                                         cipher=self.cipher,subject=subject,request_id=request_id)
            if (result.get('state'),result.get('removed')) == ('VERIFYING',False):state['phase']='RESIDUAL'
            elif (result.get('state'),result.get('removed')) != ('IN_PROGRESS',True):
                raise ValueError('invalid job listing cleanup result')
        elif phase=='RESIDUAL':
            result=self.residual.observe_page(subject=subject,request_id=request_id,limit=limit)
            if result['residual_found']:
                return {'phase':'RESIDUAL','residual_found':True}
            if result['scan_complete']:state['phase']='BINDING_RESIDUAL'
        else:
            result=self.binding_residual.observe_page(subject=subject,request_id=request_id,limit=limit)
            if result['residual_found']:
                return {'phase':'BINDING_RESIDUAL','residual_found':True}
            if result['scan_complete']:state['phase']='VERIFYING'
        raw=json.dumps(state,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')
        self.store.save_checkpoint(cipher=self.cipher,**scope,expected_version=version,
            state='VERIFYING' if state['phase']=='VERIFYING' else 'IN_PROGRESS',checkpoint=raw)
        return {'phase':state['phase']}
