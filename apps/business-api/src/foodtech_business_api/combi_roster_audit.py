"""Privileged retained-Combi roster observation, never a writer attestation.

A bounded full-table key scan is necessary to detect meals omitted from the
owner's listing. Its server-side filter/projection does not reduce evaluated
read capacity or grant customer-level scan permission. No default client, CLI,
route or model permit is provided. An independent ENFORCED writer prerequisite
and a final account/writer/revision CAS are the registrar's responsibility.
Auxiliary live/source keys establish parent presence only; their payloads and
completeness are not certified by this roster observation.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import re

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
from foodtech_contracts.combi_history_witness import canonical_uuid, roster_digest, history_integer, SIZES
from .subject_identity import valid_subject

MAX_MEALS=10_000
MAX_SCAN_ITEMS=1_000_000
MAX_PAGES=1000
SCAN_PAGE_SIZE=1000
LIST_PAGE_SIZE=50
GET_BATCH_SIZE=50


@dataclass(frozen=True)
class CombiRosterObservation:
    owner: str = field(repr=False)
    revision: int
    revision_present: bool
    roster_sha256: str = field(repr=False)
    proof_sha256: str = field(repr=False)
    count: int


class DynamoCombiRosterAudit:
    def __init__(self,*,store):
        if not re.fullmatch(r'foodtech-cooking-jobs-[a-z0-9-]{1,32}',store.table.name):
            raise ValueError('explicit Foodtech cooking table required')
        self.store=store;self.table=store.table
        self.serializer,self.decoder=TypeSerializer(),TypeDeserializer()

    def _encode(self,row):
        return {k:self.serializer.serialize(v) for k,v in row.items()}

    def _decode(self,row):
        if type(row) is not dict:raise ValueError('record unavailable')
        return {k:self.decoder.deserialize(v) for k,v in row.items()}

    def _revision(self,owner):
        value=self.store.meal_history_revision(owner)
        return None if value is None else history_integer(value)

    def _scan(self,owner,max_scanned):
        prefix=f'SCOPE#USER#{owner}#COMBI_MEAL#'
        pages=iter(self.table.meta.client.get_paginator('scan').paginate(TableName=self.table.name,
            FilterExpression=Attr('pk').begins_with(prefix),ProjectionExpression='#pk, #sk',
            ExpressionAttributeNames={'#pk':'pk','#sk':'sk'},ConsistentRead=True,
            PaginationConfig={'PageSize':min(SCAN_PAGE_SIZE,max_scanned)}))
        groups={};scanned=0;seen=set()
        page_size=min(SCAN_PAGE_SIZE,max_scanned)
        for number in range(MAX_PAGES):
            if scanned+page_size>max_scanned:raise ValueError('remaining scan budget cannot cover another page')
            try:page=next(pages)
            except StopIteration:raise ValueError('scan completion was not observed') from None
            items,count=page.get('Items'),page.get('ScannedCount')
            if type(items) is not list or type(count) is not int or not 0 <= len(items) <= count <= min(SCAN_PAGE_SIZE,max_scanned):
                raise ValueError('invalid scan accounting')
            scanned+=count
            if scanned>max_scanned:raise ValueError('scan item limit exceeded')
            for row in items:
                pk,sk=row.get('pk'),row.get('sk')
                if type(pk) is not str or not pk.startswith(prefix) or type(sk) is not str or not 1 <= len(sk) <= 1024:
                    raise ValueError('foreign or invalid retained key')
                meal,separator,suffix=pk[len(prefix):].partition('#')
                if not canonical_uuid(meal) or (separator and suffix not in ('LIVE','SOURCE')):
                    raise ValueError('unknown retained partition layout')
                group=groups.setdefault(meal,{'meta':False,'jobs':set(),'transitions':set()})
                if len(groups)>MAX_MEALS:raise ValueError('retained meal limit exceeded')
                if suffix:continue
                if sk=='META':
                    if group['meta']:raise ValueError('duplicate retained metadata observation')
                    group['meta']=True
                elif sk.startswith('JOB#'):
                    if sk in group['jobs'] or len(group['jobs'])>=2:raise ValueError('ambiguous retained jobs')
                    group['jobs'].add(sk)
                elif sk.startswith('TRANSITION#'):
                    if group['transitions']:raise ValueError('ambiguous retained transition')
                    group['transitions'].add(sk)
            cursor=page.get('LastEvaluatedKey')
            if not cursor:return groups
            if scanned>=max_scanned:raise ValueError('scan ended at its bound without completion')
            if type(cursor) is not dict or set(cursor)!={'pk','sk'} or any(type(v) is not str or not v for v in cursor.values()):
                raise ValueError('invalid scan cursor')
            token=hashlib.sha256(json.dumps(cursor,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if token in seen:raise ValueError('repeated scan cursor')
            seen.add(token)
        raise ValueError('scan page limit exceeded')

    def _listed(self,owner):
        pk=f'SCOPE#USER#{owner}#COMBI_MEALS'
        pages=iter(self.table.meta.client.get_paginator('query').paginate(TableName=self.table.name,
            KeyConditionExpression=Key('pk').eq(pk)&Key('sk').begins_with('MEAL#'),
            ProjectionExpression='pk, sk, meal_batch_id',ConsistentRead=True,
            PaginationConfig={'PageSize':LIST_PAGE_SIZE}))
        ids=[];previous='';seen=set()
        for number in range(MAX_PAGES):
            try:page=next(pages)
            except StopIteration:raise ValueError('listing completion was not observed') from None
            rows=page.get('Items')
            if type(rows) is not list or len(rows)>LIST_PAGE_SIZE:raise ValueError('invalid listing page')
            for row in rows:
                meal=row.get('meal_batch_id');sk=row.get('sk')
                if row.get('pk')!=pk or not canonical_uuid(meal) or sk!='MEAL#'+meal or sk<=previous:
                    raise ValueError('foreign, duplicate or unordered listing')
                previous=sk;ids.append(meal)
                if len(ids)>MAX_MEALS:raise ValueError('listing meal limit exceeded')
            cursor=page.get('LastEvaluatedKey')
            if not cursor:return ids
            if type(cursor) is not dict or set(cursor)!={'pk','sk'} or cursor.get('pk')!=pk or \
                    type(cursor.get('sk')) is not str or not cursor['sk'].startswith('MEAL#') or \
                    not canonical_uuid(cursor['sk'][5:]) or (rows and cursor['sk']!=previous) or cursor['sk'] in seen or cursor['sk']<previous:
                raise ValueError('invalid listing cursor')
            seen.add(cursor['sk']);previous=cursor['sk']
        raise ValueError('listing page limit exceeded')

    def _get(self,keys,projection,names=None):
        rows=[]
        for offset in range(0,len(keys),GET_BATCH_SIZE):
            batch=keys[offset:offset+GET_BATCH_SIZE]
            operations=[]
            for key in batch:
                operation={'TableName':self.table.name,'Key':self._encode(key),'ProjectionExpression':projection}
                if names:operation['ExpressionAttributeNames']=names
                operations.append({'Get':operation})
            response=self.store.client.transact_get_items(TransactItems=operations).get('Responses')
            if type(response) is not list or len(response)!=len(batch):raise ValueError('incomplete retained metadata batch')
            decoded=[self._decode(row['Item']) for row in response]
            if any(any(row.get(k)!=v for k,v in key.items()) for row,key in zip(decoded,batch)):
                raise ValueError('retained metadata key mismatch')
            rows.extend(decoded)
        return rows

    def observe(self,*,owner,max_scanned=MAX_SCAN_ITEMS):
        if not valid_subject(owner) or type(max_scanned) is not int or not 1 <= max_scanned <= MAX_SCAN_ITEMS:
            raise ValueError('bounded trusted owner audit required')
        before=self._revision(owner)
        groups=self._scan(owner,max_scanned);ids=self._listed(owner)
        if set(groups)!=set(ids) or any(not group['meta'] for group in groups.values()):
            raise ValueError('retained meals and listing differ or have orphaned children')
        if groups and before is None:raise ValueError('retained meals have no tracked history')
        keys=[{'pk':f'SCOPE#USER#{owner}#COMBI_MEAL#{meal}','sk':'META'} for meal in ids]
        meta=self._get(keys,'pk, sk, meal_batch_id, #version, #provenance, equipment_size, created_at, '
            'rice_cooking_job_id, mackerel_cooking_job_id, mode_transition_id',{'#version':'version','#provenance':'provenance'})
        facts=[];children=[]
        for meal,key,row in zip(ids,keys,meta):
            version=history_integer(row['version'])
            references=[row.get(k) for k in ('rice_cooking_job_id','mackerel_cooking_job_id','mode_transition_id')]
            if row.get('meal_batch_id')!=meal or row.get('provenance')!='SYNTHETIC' or row.get('equipment_size') not in SIZES or \
                    any(not canonical_uuid(v) for v in references) or len(set(references))!=3:
                raise ValueError('invalid retained meal references')
            created=row.get('created_at')
            if type(created) is not str or len(created)>40 or datetime.fromisoformat(created.replace('Z','+00:00')).utcoffset()!=timedelta(0):
                raise ValueError('invalid retained creation timestamp')
            required={f'JOB#RICE#{references[0]}',f'JOB#MACKEREL#{references[1]}'}
            transition='TRANSITION#'+references[2]
            if groups[meal]['jobs']!=required or groups[meal]['transitions']!={transition}:
                raise ValueError('retained child keys differ from metadata')
            children.extend({'pk':key['pk'],'sk':sk} for sk in sorted(required|{transition}))
            facts.append([meal,version,*references,row['equipment_size'],created])
        rows=self._get(children,'pk, sk, meal_batch_id, job_id, transition_id, #menu',{'#menu':'menu'})
        by_meal={row['meal_batch_id']:row for row in meta}
        for row in rows:
            meal=row.get('meal_batch_id');parent=by_meal.get(meal)
            if parent is None or row['pk']!=f'SCOPE#USER#{owner}#COMBI_MEAL#{meal}':
                raise ValueError('retained child has no matching parent')
            if row['sk'].startswith('TRANSITION#'):
                if row.get('transition_id')!=parent['mode_transition_id']:raise ValueError('transition reference mismatch')
            else:
                menu=row.get('menu')
                if menu not in ('RICE','MACKEREL') or row.get('job_id')!=parent[menu.lower()+'_cooking_job_id'] or \
                        row['sk']!=f"JOB#{menu}#{row['job_id']}":raise ValueError('retained job reference mismatch')
        after=self._revision(owner)
        if before!=after:raise ValueError('history changed during retained roster audit')
        revision=0 if before is None else before
        digest=roster_digest(ids)
        proof={'schema_version':'combi-retained-roster-observation-v1','owner':owner,
            'revision':revision,'revision_present':before is not None,'roster_sha256':digest,'metadata':facts}
        proof_digest=hashlib.sha256(json.dumps(proof,sort_keys=True,separators=(',',':')).encode('ascii')).hexdigest()
        return CombiRosterObservation(owner,revision,before is not None,digest,proof_digest,len(ids))
