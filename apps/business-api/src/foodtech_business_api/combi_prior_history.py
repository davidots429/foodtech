"""Protected full-roster combi ordering reader, without coverage issuance.

An independently privileged verifier must establish retained-data coverage and
exclusive deployment of the atomic writer contract before publishing controls.
A scan, counter or customer Boolean cannot do that. No default client, factory,
public route or model activation. Reads are limited to metadata and timestamps.
"""
from datetime import datetime, timedelta, timezone
import json
import re
import math
from boto3.dynamodb.types import TypeSerializer, TypeDeserializer

from foodtech_contracts.combi_execution_binding import CombiExecutionBinding
from foodtech_contracts.combi_history_witness import (CombiHistoryWitness, HISTORY_CONTRACT, COVERAGE_SCOPE,
    CONTROL_KEY, COVERAGE_FIELD, SIZES, history_integer, canonical_uuid, roster_digest)
from .prior_history_authority import PriorHistoryUnavailable
from .subject_identity import valid_subject

MAX_MEALS = 10_000
MAX_PAGES = 1000
PAGE_SIZE = 50


def _instant(value):
    if type(value) is not str:
        raise ValueError('recorded UTC time required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() != timedelta(0):
        raise ValueError('recorded UTC time required')
    return result


class DynamoCombiPriorHistoryAuthority:
    def __init__(self, *, auth_table, cooking_table, account_id, client, clock):
        stage = re.fullmatch('foodtech-auth-sessions-([a-z0-9-]{1,32})', auth_table) if type(auth_table) is str else None
        if not stage or cooking_table != 'foodtech-cooking-jobs-' + stage[1] or type(account_id) is not str or \
                not re.fullmatch('[0-9]{12}', account_id) or client is None or not callable(clock):
            raise ValueError('explicit same-stage protected combi reader required')
        self.auth, self.cooking, self.client, self.clock = auth_table, cooking_table, client, clock
        self.cooking_arn = f'arn:aws:dynamodb:ap-southeast-2:{account_id}:table/{cooking_table}'
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()

    def _encode(self, values):
        return {key: self.serializer.serialize(value) for key, value in values.items()}

    def _decode(self, row):
        if type(row) is not dict:
            raise PriorHistoryUnavailable('combi history record unavailable')
        return {key: self.decoder.deserialize(value) for key, value in row.items()}

    def __repr__(self):
        return 'DynamoCombiPriorHistoryAuthority()'

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not 0 < value < 2**63 or not math.isfinite(value):
            raise ValueError('valid combi history clock required')
        return value

    def _controls(self, owner, epoch):
        keys = [(self.auth, {'pk': 'USER#' + owner}), (self.auth, {'pk': CONTROL_KEY}),
                (self.cooking, {'pk': 'SCOPE#USER#' + owner + '#COMBI_MEALS', 'sk': 'HISTORY#REVISION'})]
        projections = ['pk, #epoch, suspended, deletion_lock_request, ' + COVERAGE_FIELD,
            'pk, #state, #version, #generation, writer_contract, cooking_table_arn, manifest_sha256, verified_at, valid_until',
            'pk, sk, #revision']
        aliases = [{'#epoch': 'epoch'}, {'#state': 'state', '#version': 'version', '#generation': 'generation'}, {'#revision': 'revision'}]
        requests = []
        for (table, key), projection, names in zip(keys, projections, aliases):
            get = {'TableName': table, 'Key': self._encode(key), 'ProjectionExpression': projection}
            if names: get['ExpressionAttributeNames'] = names
            requests.append({'Get': get})
        response = self.client.transact_get_items(TransactItems=requests).get('Responses')
        try:
            if type(response) is not list or len(response) != 3:
                raise ValueError
            account, writers, history = [self._decode(x['Item']) for x in response]
            coverage = account[COVERAGE_FIELD]
            revision = history_integer(history['revision'])
            baseline = history_integer(coverage['coverage_revision'], 0)
            now = self._now()
            verified = history_integer(writers['verified_at'], 1, 2**63-1)
            expires = history_integer(writers['valid_until'], 1, 2**63-1)
            if any(row.get('pk') != key['pk'] for row, (_, key) in zip((account, writers, history), keys)) or \
                    history.get('sk') != 'HISTORY#REVISION' or history_integer(account['epoch'], 0, 2**63-1) != epoch or \
                    account.get('suspended', False) is not False or 'deletion_lock_request' in account or \
                    writers.get('state') != 'ENFORCED' or coverage.get('state') != 'ACTIVE' or \
                    not canonical_uuid(writers.get('generation')) or coverage.get('generation') != writers['generation'] or \
                    writers.get('writer_contract') != HISTORY_CONTRACT or coverage.get('writer_contract') != HISTORY_CONTRACT or \
                    writers.get('cooking_table_arn') != self.cooking_arn or coverage.get('coverage_scope') != COVERAGE_SCOPE or \
                    coverage.get('writer_manifest_sha256') != writers.get('manifest_sha256') or not baseline <= revision or \
                    (baseline == 0 and coverage.get('baseline_roster_sha256') != roster_digest([])) or \
                    not verified <= now < expires:
                raise ValueError
            # The typed witness below validates all digest and version fields;
            # validate them now too, before any roster/metadata read.
            for digest in (writers['manifest_sha256'], coverage['proof_sha256'], coverage['baseline_roster_sha256']):
                if type(digest) is not str or not re.fullmatch('[a-f0-9]{64}', digest): raise ValueError
            history_integer(writers['version']); history_integer(coverage['version'])
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise PriorHistoryUnavailable('protected combi history coverage unavailable') from None
        return account, writers, coverage, history

    def _roster(self, owner):
        pk = 'SCOPE#USER#' + owner + '#COMBI_MEALS'
        ids, cursor, seen, previous, previous_cursor = [], None, set(), '', ''
        pages = iter(self.client.get_paginator('query').paginate(
            TableName=self.cooking, KeyConditionExpression='pk = :owner AND begins_with(sk, :meal)',
            ExpressionAttributeValues=self._encode({':owner': pk, ':meal': 'MEAL#'}),
            ProjectionExpression='pk, sk, meal_batch_id', ConsistentRead=True,
            PaginationConfig={'PageSize': PAGE_SIZE}))
        for _ in range(MAX_PAGES):
            try:
                page = next(pages)
            except StopIteration:
                raise PriorHistoryUnavailable('combi roster completion not observed') from None
            rows = page.get('Items')
            if type(rows) is not list or len(rows) > PAGE_SIZE:
                raise PriorHistoryUnavailable('combi roster page unavailable')
            for encoded in rows:
                row = self._decode(encoded); meal = row.get('meal_batch_id'); sk = row.get('sk')
                if not canonical_uuid(meal) or row.get('pk') != pk or sk != 'MEAL#' + meal or sk <= previous:
                    raise PriorHistoryUnavailable('invalid complete combi roster')
                previous = sk; ids.append(meal)
                if len(ids) > MAX_MEALS: raise PriorHistoryUnavailable('combi roster exceeds verified bounds')
            cursor = page.get('LastEvaluatedKey')
            if not cursor: return ids
            try:
                decoded = self._decode(cursor)
                if set(decoded) != {'pk', 'sk'} or decoded['pk'] != pk or not decoded['sk'].startswith('MEAL#') or \
                        not canonical_uuid(decoded['sk'][5:]) or (rows and decoded['sk'] != previous) or decoded['sk'] <= previous_cursor:
                    raise ValueError
                token = json.dumps(cursor, sort_keys=True)
                if token in seen: raise ValueError
                seen.add(token)
                previous_cursor = decoded['sk']
                previous = max(previous, previous_cursor)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise PriorHistoryUnavailable('combi roster cursor unavailable') from None
        raise PriorHistoryUnavailable('combi roster did not finish')

    def _get(self, keys, projection, names=None):
        requests = []
        for key in keys:
            get = {'TableName': self.cooking, 'Key': self._encode(key), 'ProjectionExpression': projection}
            if names: get['ExpressionAttributeNames'] = names
            requests.append({'Get': get})
        responses = self.client.transact_get_items(TransactItems=requests).get('Responses')
        try:
            if type(responses) is not list or len(responses) != len(keys): raise ValueError
            rows = [self._decode(x['Item']) for x in responses]
            if any(any(row.get(k) != v for k, v in key.items()) for row, key in zip(rows, keys)): raise ValueError
            return rows
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PriorHistoryUnavailable('combi history metadata unavailable') from None

    def _reheats(self, pk, step, now, meal_version):
        latest = step.get('latest_reheat')
        if latest is None: return []
        spans, cursor, seen = [], None, set()
        pages = iter(self.client.get_paginator('query').paginate(
            TableName=self.cooking, KeyConditionExpression='pk = :pk AND begins_with(sk, :event)',
            ExpressionAttributeValues=self._encode({':pk': pk, ':event': 'EVENT#'}),
            ProjectionExpression='pk, sk, #event, #segment.segment_id, #segment.started_at, #segment.unloaded_at',
            ExpressionAttributeNames={'#event': 'event', '#segment': 'segment'}, ConsistentRead=True,
            PaginationConfig={'PageSize': PAGE_SIZE}))
        for _ in range(MAX_PAGES):
            try:
                page = next(pages)
            except StopIteration:
                raise ValueError('reheat history completion not observed') from None
            if type(page.get('Items')) is not list or len(page['Items']) > PAGE_SIZE: raise ValueError('invalid reheat page')
            for encoded in page['Items']:
                row = self._decode(encoded)
                if row.get('pk') != pk or type(row.get('sk')) is not str or not re.fullmatch('EVENT#[0-9]{12,19}', row['sk']) or \
                        row['sk'] != f"EVENT#{int(row['sk'][6:]):012d}" or not 1 <= int(row['sk'][6:]) <= meal_version:
                    raise ValueError('invalid reheat row scope')
                if row.get('event') != 'MACKEREL_REHEATED': continue
                s = row['segment']; start, end = _instant(s['started_at']), _instant(s['unloaded_at'])
                if not canonical_uuid(s['segment_id']) or not start < end <= now or \
                        any(s['segment_id'] == identifier for identifier, _, _ in spans): raise ValueError('invalid reheat span')
                spans.append((s['segment_id'], start, end))
                if len(spans) > 1000: raise ValueError('reheat history exceeds verified bounds')
            cursor = page.get('LastEvaluatedKey')
            if not cursor: break
            key = self._decode(cursor)
            if set(key) != {'pk', 'sk'} or key['pk'] != pk or type(key['sk']) is not str or not key['sk'].startswith('EVENT#'):
                raise ValueError('invalid reheat cursor')
            token = json.dumps(cursor, sort_keys=True)
            if token in seen: raise ValueError('reheat history did not advance')
            seen.add(token)
        else: raise ValueError('reheat history did not finish')
        spans.sort(key=lambda x: x[1])
        if not spans or (latest['segment_id'], _instant(latest['started_at']), _instant(latest['unloaded_at'])) != spans[-1] or \
                _instant(step['current_unloaded_at']) != spans[-1][2] or \
                any(b[1] < a[2] for a, b in zip(spans, spans[1:])): raise ValueError('reheat pointer mismatch')
        return spans

    def observe(self, *, owner, binding):
        if not valid_subject(owner) or type(binding) is not CombiExecutionBinding:
            raise ValueError('explicit authenticated combi binding required')
        try:
            return self._observe(owner=owner, binding=binding)
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError, OverflowError):
            raise PriorHistoryUnavailable('verified immediately-prior combi history unavailable') from None

    def _observe(self, *, owner, binding):
        before = self._controls(owner, binding.auth_epoch)
        ids = self._roster(owner)
        if binding.current_meal_id not in ids or binding.prior_meal_id not in ids:
            raise PriorHistoryUnavailable('combi history meal missing')
        meals, jobs = {}, {}
        field = 'rice_cooking_job_id' if binding.menu == 'RICE' else 'mackerel_cooking_job_id'
        try:
            for offset in range(0, len(ids), PAGE_SIZE):
                page_ids = ids[offset:offset + PAGE_SIZE]
                keys = [{'pk': f'SCOPE#USER#{owner}#COMBI_MEAL#{meal}', 'sk': 'META'} for meal in page_ids]
                page = self._get(keys, 'pk, sk, meal_batch_id, #version, #provenance, equipment_size, '
                    'rice_cooking_job_id, mackerel_cooking_job_id, mode_transition_id, #source.device_id, #source.raw_sha256',
                    {'#version': 'version', '#source': 'source', '#provenance': 'provenance'})
                for meal, meta in zip(page_ids, page):
                    if meta.get('meal_batch_id') != meal or meta.get('provenance') != 'SYNTHETIC' or meta.get('equipment_size') not in SIZES or \
                            any(not canonical_uuid(meta.get(k)) for k in ('rice_cooking_job_id', 'mackerel_cooking_job_id', 'mode_transition_id')) or \
                            len({meta[k] for k in ('rice_cooking_job_id', 'mackerel_cooking_job_id', 'mode_transition_id')}) != 3:
                        raise ValueError('invalid combi metadata')
                    history_integer(meta['version']); meals[meal] = meta
                keys = [{'pk': key['pk'], 'sk': f'JOB#{binding.menu}#{meta[field]}'} for key, meta in zip(keys, page)]
                page_jobs = self._get(keys, 'pk, sk, meal_batch_id, job_id, #menu, #status, active_cooking_started_at, '
                    'program_completed_at, food_unloaded_at, current_unloaded_at, '
                    'latest_reheat.segment_id, latest_reheat.started_at, latest_reheat.unloaded_at',
                    {'#menu': 'menu', '#status': 'status'})
                for meal, meta, job in zip(page_ids, page, page_jobs):
                    if job.get('meal_batch_id') != meal or job.get('job_id') != meta[field] or job.get('menu') != binding.menu:
                        raise ValueError('invalid historical combi job')
                    jobs[meal] = job
            current_meta, prior_meta = meals[binding.current_meal_id], meals[binding.prior_meal_id]
            current, prior = jobs[binding.current_meal_id], jobs[binding.prior_meal_id]
            current_source, prior_source = current_meta['source'], prior_meta['source']
            device, size = current_source['device_id'], current_meta['equipment_size']
            now = datetime.fromtimestamp(self._now(), timezone.utc)
            start, previous_start, previous_end = _instant(current['active_cooking_started_at']), \
                _instant(prior['active_cooking_started_at']), _instant(prior['food_unloaded_at'])
            if type(device) is not str or not 1 <= len(device) <= 128 or prior_source.get('device_id') != device or \
                    prior_meta['equipment_size'] != size or current.get('status') != 'UNLOADED' or prior.get('status') != 'UNLOADED' or \
                    not previous_start < previous_end <= start < _instant(current['food_unloaded_at']) <= now or \
                    (current['job_id'], prior['job_id'], history_integer(current_meta['version']), history_integer(prior_meta['version']),
                     current_source.get('raw_sha256'), prior_source.get('raw_sha256')) != \
                    (binding.current_job_id, binding.prior_job_id, binding.current_meal_version, binding.prior_meal_version,
                     binding.current_source_sha256, binding.prior_source_sha256): raise ValueError('combi binding changed')
            eligible = []
            for meal, job in jobs.items():
                if meal == binding.current_meal_id: continue
                meta = meals[meal]
                if meta['equipment_size'] != size: continue
                source = meta.get('source')
                if type(source) is dict and type(source.get('device_id')) is str and \
                        1 <= len(source['device_id']) <= 128 and source['device_id'] != device: continue
                placed = job.get('active_cooking_started_at')
                if placed is None and job.get('status') == 'PENDING' and not any(job.get(k) is not None for k in
                    ('program_completed_at', 'food_unloaded_at', 'current_unloaded_at', 'latest_reheat')): continue
                if type(source) is not dict or source.get('device_id') != device: raise ValueError('historical device unavailable')
                placed = _instant(placed)
                if placed > now or job.get('status') not in {'RUNNING', 'AWAITING_UNLOAD', 'UNLOADED'}:
                    raise ValueError('unresolved historical cooking interval')
                unloaded = _instant(job['food_unloaded_at']) if job.get('status') == 'UNLOADED' else None
                if unloaded is not None and not placed < unloaded <= now: raise ValueError('invalid historical unload')
                spans = [(None, placed, unloaded)]
                reheats = self._reheats(meta['pk'], job, now, history_integer(meta['version']))
                if reheats and (binding.menu != 'MACKEREL' or unloaded is None or reheats[0][1] < unloaded):
                    raise ValueError('invalid reheat order')
                for segment, began, ended in spans + reheats:
                    if began > start: continue
                    if began == start or ended is None or ended > start: raise ValueError('ambiguous overlapping historical operation')
                    eligible.append((began, meal, segment))
            latest = max((value[0] for value in eligible), default=None)
            selected = [(meal, segment) for began, meal, segment in eligible if began == latest]
            if len(selected) != 1 or selected[0][0] != binding.prior_meal_id:
                raise ValueError('selected meal is not uniquely immediately prior')
            digest = roster_digest(ids)
            coverage, writer, revision = before[2], before[1], history_integer(before[3]['revision'])
            if history_integer(coverage['coverage_revision'], 0) == revision and coverage['baseline_roster_sha256'] != digest:
                raise ValueError('baseline combi roster proof mismatch')
            if self._controls(owner, binding.auth_epoch) != before: raise ValueError('protected combi history changed')
            return CombiHistoryWitness(owner, binding, device, size, revision, history_integer(writer['version']),
                history_integer(coverage['version']), writer['generation'], digest, writer['manifest_sha256'],
                history_integer(coverage['coverage_revision'], 0), coverage['baseline_roster_sha256'], coverage['proof_sha256'],
                history_integer(writer['verified_at'], 1, 2**63-1), history_integer(writer['valid_until'], 1, 2**63-1))
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError, OverflowError):
            raise PriorHistoryUnavailable('verified immediately-prior combi history unavailable') from None

    def revalidate(self, witness):
        if type(witness) is not CombiHistoryWitness: raise ValueError('typed combi history witness required')
        current = self.observe(owner=witness.owner, binding=witness.binding)
        if current != witness: raise PriorHistoryUnavailable('combi history observation changed')
        return current
