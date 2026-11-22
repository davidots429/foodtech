"""Resumable tier deletion and index observation; never a deletion receipt.

A trusted authenticated worker must stop writers and verify backups and every
other store. A GSI NOT_OBSERVED result is eventual, not proof of erasure.
"""
import json


class TierDeletionCoordinator:
    @staticmethod
    def _writer_quiescence_proven():
        # No deployed legacy-writer fencing evidence is connected yet.
        return False

    def __init__(self, *, capture, worker):
        if worker.index_capture is not capture or worker.tier_store is not capture.tier_store or \
                worker.progress_store is not capture.progress_store or worker.cipher is not capture.cipher:
            raise ValueError('tier deletion dependencies do not match')
        self.capture,self.worker=capture,worker
        self.progress,self.cipher=capture.progress_store,capture.cipher

    def step(self, *, subject, request_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:raise ValueError('invalid tier deletion limit')
        scope={'subject':subject,'request_id':request_id,'domain':'TIER_COORDINATOR'}
        item=self.capture._load(subject,request_id,scope['domain'])
        if item is None:
            state,version={'schema':1,'phase':'CAPTURE','index':0,'total':0},0
        else:
            state,version=self.capture._json(item['checkpoint']),item['version']
            if set(state)!={'schema','phase','index','total'} or type(state['schema']) is not int or state['schema']!=1 or \
                    state['phase'] not in {'CAPTURE','DELETE','OBSERVE','VERIFYING'} or \
                    type(state['index']) is not int or type(state['total']) is not int or \
                    not 0 <= state['index'] <= state['total'] < 10**12 or \
                    (state['phase']=='CAPTURE' and (state['index']!=0 or state['total']!=0)) or \
                    (state['phase']=='DELETE' and state['index']!=0) or \
                    (state['phase']=='VERIFYING' and state['index']!=state['total']) or \
                    type(version) is not int or version < 1 or \
                    item['state'] != ('VERIFYING' if state['phase']=='VERIFYING' else 'IN_PROGRESS'):
                raise ValueError('invalid tier coordinator checkpoint')
        if not self._writer_quiescence_proven():
            return {'phase':'QUIESCENCE','writer_quiescence_required':True}
        if state['phase']=='VERIFYING':return {'phase':'VERIFYING'}
        if state['phase']=='CAPTURE':
            result=self.capture.capture_page(subject=subject,request_id=request_id,limit=limit)
            if result['complete']:state.update(phase='DELETE',total=result['captured_count'])
        elif state['phase']=='DELETE':
            result=self.worker.run_page(subject=subject,request_id=request_id,limit=limit)
            if result['state']=='VERIFYING':state['phase']='OBSERVE'
            elif result['state']!='IN_PROGRESS':raise ValueError('invalid tier cleanup result')
        else:
            if state['index'] < state['total']:
                result=self.capture.observe_and_record(subject=subject,request_id=request_id,index=state['index'])
                observation=result.get('observation')
                if observation in {'PRESENT','INCONCLUSIVE'}:
                    return {'phase':'OBSERVE','observation':observation}
                if observation!='NOT_OBSERVED':raise ValueError('invalid tier observation')
                state['index']+=1
            if state['index']==state['total']:state['phase']='VERIFYING'
        raw=json.dumps(state,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')
        self.progress.save_checkpoint(cipher=self.cipher,**scope,expected_version=version,
            state='VERIFYING' if state['phase']=='VERIFYING' else 'IN_PROGRESS',checkpoint=raw)
        return {'phase':state['phase']}

    def recheck_index(self, *, subject, request_id, index):
        """Reobserve one captured key; a late visible/unknown result reopens observation.

        NOT_OBSERVED is a point-in-time GSI observation only. The caller must
        arrange repeated checks and must not treat this result as a receipt.
        """
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid tier recheck index')
        scope={'subject':subject,'request_id':request_id,'domain':'TIER_COORDINATOR'}
        item=self.capture._load(subject,request_id,scope['domain'])
        if not item or item.get('state')!='VERIFYING' or type(item.get('version')) is not int:
            raise ValueError('tier verification not available')
        state=self.capture._json(item['checkpoint'])
        if set(state)!={'schema','phase','index','total'} or type(state['schema']) is not int or state['schema']!=1 or \
                state['phase']!='VERIFYING' or type(state['index']) is not int or type(state['total']) is not int or \
                not 0 <= index < state['total'] < 10**12 or state['index']!=state['total']:
            raise ValueError('invalid tier verification checkpoint')
        if not self._writer_quiescence_proven():
            return {'phase':'QUIESCENCE','writer_quiescence_required':True}
        result=self.capture.observe_and_record(subject=subject,request_id=request_id,index=index)
        observation=result.get('observation')
        if observation not in {'PRESENT','NOT_OBSERVED','INCONCLUSIVE'}:
            raise ValueError('invalid tier observation')
        if observation in {'PRESENT','INCONCLUSIVE'}:
            state.update(phase='OBSERVE',index=index)
            raw=json.dumps(state,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')
            self.progress.save_checkpoint(cipher=self.cipher,**scope,expected_version=item['version'],
                                           state='IN_PROGRESS',checkpoint=raw)
            return {'phase':'OBSERVE','observation':observation}
        return {'phase':'VERIFYING','observation':'NOT_OBSERVED'}
