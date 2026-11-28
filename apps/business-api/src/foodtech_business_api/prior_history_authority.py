"""Internal prior-job witness from protected coverage and complete owner roster.

An authenticated BFF must supply the subject and trusted reader client. Control
records are attestations issued by a separate privileged verifier AFTER proving
all retained legacy rows/listings and deployed writer fencing. This module does
not issue those attestations, authenticate callers, or prove deployment policy.
Never provision its control records from customer inputs or a passing scan alone.
Per-account coverage is inside the existing Auth USER record, so normal final
retirement removes it without leaving another subject-keyed row. Reader roles
must not issue/change coverage, and ordinary writers must be unable to forge
its protected fields. Those effective permissions still need actual validation.
The witness establishes recorded order/coverage only, not source-data access,
safety, Evidence grade or permission to call a model.
No public route, factory, default credentials, writer API or AI activation here.
"""
import hashlib
import json
import re
from datetime import datetime
from uuid import UUID

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from foodtech_contracts.tonkatsu_prior_history import TonkatsuPriorHistoryWitness, fryer_stream

from .source_read_authority import _integer
from .subject_identity import valid_subject

from foodtech_contracts.prior_history_witness import HISTORY_CONTRACT as CONTRACT, PriorHistoryWitness
MAX_JOBS = 100


class PriorHistoryUnavailable(Exception):
    pass


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.utcoffset() is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


class DynamoPriorHistoryAuthority:
    def __init__(self, *, auth_table, cooking_table, account_id, client, clock):
        match = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', auth_table or '')
        if not match or cooking_table != 'foodtech-cooking-jobs-' + match[1] or \
                not isinstance(account_id, str) or not re.fullmatch('[0-9]{12}', account_id) or client is None:
            raise ValueError('explicit same-stage protected history reader required')
        self.auth, self.cooking, self.client, self.clock = auth_table, cooking_table, client, clock
        self.cooking_arn = f'arn:aws:dynamodb:ap-southeast-2:{account_id}:table/{cooking_table}'
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()

    def _encode(self, values):
        return {k: self.serializer.serialize(v) for k, v in values.items()}

    def _decode(self, row):
        if not isinstance(row, dict):
            raise PriorHistoryUnavailable('history record unavailable')
        return {k: self.decoder.deserialize(v) for k, v in row.items()}

    def _controls(self, owner, epoch):
        keys = [('USER#' + owner, None), ('CONTROL#COOKING_HISTORY_WRITERS', None),
                ('SCOPE#USER#' + owner + '#JOBS', 'HISTORY#REVISION')]
        projections = ['pk, #epoch, suspended, deletion_lock_request, history_coverage',
            'pk, #state, #version, generation, writer_contract, cooking_table_arn, manifest_sha256, verified_at, valid_until',
            'pk, sk, revision']
        names = [{'#epoch': 'epoch'}, {'#state': 'state', '#version': 'version'}, {}]
        items = []
        for (pk, sk), projection, aliases in zip(keys, projections, names):
            get = {'TableName': self.cooking if sk else self.auth,
                   'Key': self._encode({'pk': pk, **({'sk': sk} if sk else {})}), 'ProjectionExpression': projection}
            if aliases:
                get['ExpressionAttributeNames'] = aliases
            items.append({'Get': get})
        result = self.client.transact_get_items(TransactItems=items)
        try:
            rows = result['Responses']
            if not isinstance(rows, list) or len(rows) != 3:
                raise ValueError
            account, writers, history = (self._decode(row['Item']) for row in rows)
            coverage = account.get('history_coverage')
            if not isinstance(coverage, dict):
                raise ValueError
            now = self.clock()
            revision = _integer(history.get('revision'), 1)
            baseline = _integer(coverage.get('coverage_revision'), 1)
            generation = writers.get('generation')
            if any(row.get('pk') != key[0] for row, key in zip((account, writers, history), keys)) or \
                    history.get('sk') != 'HISTORY#REVISION' or account.get('suspended', False) is not False or \
                    'deletion_lock_request' in account or _integer(account.get('epoch')) != epoch or \
                    writers.get('state') != 'ENFORCED' or coverage.get('state') != 'ACTIVE' or \
                    not _uuid(generation) or coverage.get('generation') != generation or \
                    coverage.get('writer_manifest_sha256') != writers.get('manifest_sha256') or \
                    writers.get('writer_contract') != CONTRACT or coverage.get('writer_contract') != CONTRACT or \
                    writers.get('cooking_table_arn') != self.cooking_arn or coverage.get('coverage_scope') != 'ALL_RETAINED_JOBS' or \
                    revision is None or baseline is None or not baseline <= revision <= 10**18 or \
                    any(_integer(row.get('version'), 1) is None for row in (writers, coverage)) or \
                    any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value) for value in
                        [writers.get('manifest_sha256'), coverage.get('proof_sha256'), coverage.get('baseline_roster_sha256')]) or \
                    type(now) is not int or _integer(writers.get('verified_at'), 1) is None or \
                    _integer(writers.get('valid_until'), 1) is None or \
                    not int(writers['verified_at']) <= now < int(writers['valid_until']):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PriorHistoryUnavailable('protected history coverage unavailable') from None
        return account, writers, coverage, history

    def observe(self, *, owner, auth_epoch, current_job_id, prior_job_id, job_versions):
        return self._observe(owner=owner, auth_epoch=auth_epoch, current_job_id=current_job_id,
            prior_job_id=prior_job_id, job_versions=job_versions, fryer=False)

    def observe_tonkatsu(self, *, owner, auth_epoch, current_job_id, prior_job_id, job_versions):
        return self._observe(owner=owner, auth_epoch=auth_epoch, current_job_id=current_job_id,
            prior_job_id=prior_job_id, job_versions=job_versions, fryer=True)

    def _observe(self, *, owner, auth_epoch, current_job_id, prior_job_id, job_versions, fryer):
        if not valid_subject(owner) or type(auth_epoch) is not int or not 0 <= auth_epoch < 2**63 or \
                not _uuid(current_job_id) or not _uuid(prior_job_id) or current_job_id == prior_job_id or \
                type(job_versions) is not tuple or len(job_versions) != 2 or \
                any(type(v) is not int or not 1 <= v <= 10**18 for v in job_versions):
            raise ValueError('explicit authenticated prior-history scope required')
        before = self._controls(owner, auth_epoch)
        pk = 'SCOPE#USER#' + owner + '#JOBS'
        listed, cursor, seen = {}, None, set()
        while True:
            request = {'TableName': self.cooking, 'KeyConditionExpression': 'pk = :owner AND begins_with(sk, :created)',
                'ExpressionAttributeValues': self._encode({':owner': pk, ':created': 'CREATED#'}),
                'ProjectionExpression': 'pk, sk, job_id, created_at', 'ConsistentRead': True, 'Limit': 50}
            if cursor:
                request['ExclusiveStartKey'] = cursor
            page = self.client.query(**request)
            rows = page.get('Items')
            if not isinstance(rows, list):
                raise PriorHistoryUnavailable('history listing unavailable')
            for encoded in rows:
                row = self._decode(encoded)
                job_id = row.get('job_id')
                if not _uuid(job_id) or job_id in listed or row.get('pk') != pk or \
                        not isinstance(row.get('created_at'), str) or _instant(row['created_at']) is None or \
                        row.get('sk') != 'CREATED#' + row['created_at'] + '#' + job_id:
                    raise PriorHistoryUnavailable('invalid complete history listing')
                listed[job_id] = row['created_at']
                if len(listed) > MAX_JOBS:
                    raise PriorHistoryUnavailable('history exceeds verified query bounds')
            cursor = page.get('LastEvaluatedKey')
            if not cursor:
                break
            # Empty filtered pages and nonadvancing cursors cannot loop forever.
            token = json.dumps(cursor, sort_keys=True)
            if token in seen or len(seen) >= MAX_JOBS:
                raise PriorHistoryUnavailable('history listing did not finish')
            seen.add(token)
        if current_job_id not in listed or prior_job_id not in listed:
            raise PriorHistoryUnavailable('history job missing')
        ids = sorted(listed)
        result = self.client.transact_get_items(TransactItems=[{'Get': {'TableName': self.cooking,
            'Key': self._encode({'pk': 'SCOPE#USER#' + owner + '#JOB#' + job_id, 'sk': 'META'}),
            'ProjectionExpression': 'pk, sk, job_id, owner_subject, #version, created_at, menu, provenance, '
                'equipment_profile, source_binding, #status, actual_placed_at, actual_lifted_at' +
                (', latest_tonkatsu_reheat.started_at, latest_tonkatsu_reheat.lifted_at, pending_tonkatsu_reheat.segment_id'
                 if fryer else ''),
            'ExpressionAttributeNames': {'#version': 'version', '#status': 'status'}}} for job_id in ids])
        try:
            responses = result['Responses']
            if not isinstance(responses, list) or len(responses) != len(ids):
                raise ValueError
            jobs = {job_id: self._decode(row['Item']) for job_id, row in zip(ids, responses)}
            if any(row.get('pk') != 'SCOPE#USER#' + owner + '#JOB#' + job_id or row.get('sk') != 'META' or
                   row.get('job_id') != job_id or row.get('owner_subject') != owner or
                   row.get('created_at') != listed[job_id] or _integer(row.get('version'), 1) is None
                   for job_id, row in jobs.items()):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise PriorHistoryUnavailable('history metadata unavailable') from None
        if fryer:
            streams = _fryer_order(jobs, current_job_id, prior_job_id, job_versions)
        else:
            self._jeyuk_order(jobs, current_job_id, prior_job_id, job_versions)
        roster_hash = hashlib.sha256(json.dumps(sorted(listed.items()), separators=(',', ':')).encode('ascii')).hexdigest()
        coverage, revision = before[2], int(before[3]['revision'])
        if int(coverage['coverage_revision']) == revision and coverage['baseline_roster_sha256'] != roster_hash:
            raise PriorHistoryUnavailable('baseline roster proof mismatch')
        if self._controls(owner, auth_epoch) != before:
            raise PriorHistoryUnavailable('history or protected coverage changed')
        witness = PriorHistoryWitness(owner, current_job_id, prior_job_id, job_versions, auth_epoch, revision,
            int(before[1]['version']), int(coverage['version']), before[1]['generation'], roster_hash, before[1]['manifest_sha256'],
            int(coverage['coverage_revision']), coverage['baseline_roster_sha256'], coverage['proof_sha256'])

        if not fryer:
            return witness
        return TonkatsuPriorHistoryWitness(witness, streams,
            int(before[1]['verified_at']), int(before[1]['valid_until']))

    @staticmethod
    def _jeyuk_order(jobs, current_job_id, prior_job_id, job_versions):
        current, prior = jobs[current_job_id], jobs[prior_job_id]
        start, previous_start, previous_end = (_instant(current.get('actual_placed_at')),
            _instant(prior.get('actual_placed_at')), _instant(prior.get('actual_lifted_at')))
        binding = current.get('source_binding')
        if start is None or previous_start is None or previous_end is None or not previous_start <= previous_end <= start or \
                not previous_start < start or not isinstance(binding, dict) or \
                binding.get('source_id') != 'synthetic-rest' or binding.get('device_id') != 'mockprofile-110-a-001' or \
                current.get('menu') != 'JEYUK' or current.get('provenance') != 'SYNTHETIC' or \
                not isinstance(current.get('equipment_profile'), str) or not current['equipment_profile'] or \
                prior.get('menu') != 'JEYUK' or prior.get('provenance') != 'SYNTHETIC' or \
                prior.get('equipment_profile') != current.get('equipment_profile') or \
                prior.get('status') not in {'COMPLETED', 'FAILED', 'CANCELED'} or \
                tuple(_integer(row.get('version'), 1) for row in (current, prior)) != job_versions:
            raise PriorHistoryUnavailable('completed comparable prior job unavailable')
        eligible = []
        for job_id, row in jobs.items():
            if job_id == current_job_id:
                continue
            other = row.get('source_binding')
            if row.get('menu') not in (None, 'JEYUK') and row.get('equipment_profile') not in (None, current.get('equipment_profile')) and \
                    (not isinstance(other, dict) or any(other.get(k) != binding.get(k) for k in ('source_id', 'device_id'))):
                continue
            placed = _instant(row.get('actual_placed_at'))
            if placed is None:
                if row.get('actual_placed_at') is not None or row.get('status') not in {'PENDING', 'READY'}:
                    raise PriorHistoryUnavailable('unresolved historical job')
                continue
            if placed > start:
                continue
            if not isinstance(other, dict) or any(not other.get(k) for k in ('source_id', 'device_id')):
                raise PriorHistoryUnavailable('historical binding unavailable')
            if any(other.get(k) != binding.get(k) for k in ('source_id', 'device_id')):
                continue
            if placed == start:
                raise PriorHistoryUnavailable('ambiguous historical order')
            eligible.append((placed, job_id))
        latest = max((placed for placed, _ in eligible), default=None)
        if [job_id for placed, job_id in eligible if placed == latest] != [prior_job_id]:
            raise PriorHistoryUnavailable('selected job is not uniquely immediately prior')

    def revalidate_tonkatsu(self, witness):
        if type(witness) is not TonkatsuPriorHistoryWitness:
            raise ValueError('typed fryer history witness required')
        history = witness.history
        current = self.observe_tonkatsu(owner=history.owner, auth_epoch=history.auth_epoch,
            current_job_id=history.current_job_id, prior_job_id=history.prior_job_id, job_versions=history.job_versions)
        if current != witness:
            raise PriorHistoryUnavailable('fryer history witness changed')
        return current

    def revalidate(self, witness):
        """Re-observe the whole authoritative scope; no cached proof is sufficient."""
        if type(witness) is not PriorHistoryWitness:
            raise ValueError('typed prior history witness required')
        current = self.observe(owner=witness.owner, auth_epoch=witness.auth_epoch,
            current_job_id=witness.current_job_id, prior_job_id=witness.prior_job_id,
            job_versions=witness.job_versions)
        if current != witness:
            raise PriorHistoryUnavailable('prior history witness changed')
        return current


def _fryer_interval(row):
    placed = _instant(row.get('actual_placed_at'))
    lifted = _instant(row.get('actual_lifted_at'))
    if placed is None or lifted is None or placed >= lifted or row.get('pending_tonkatsu_reheat') is not None:
        raise PriorHistoryUnavailable('unresolved fryer interval')
    reheat = row.get('latest_tonkatsu_reheat')
    if reheat is None:
        return placed, lifted
    if not isinstance(reheat, dict):
        raise PriorHistoryUnavailable('invalid fryer reheat interval')
    started = _instant(reheat.get('started_at'))
    ended = _instant(reheat.get('lifted_at'))
    if started is None or ended is None or not lifted <= started < ended:
        raise PriorHistoryUnavailable('invalid fryer reheat interval')
    return placed, ended


def _fryer_order(jobs, current_id, prior_id, versions):
    current, prior = jobs[current_id], jobs[prior_id]
    selected = (current, prior)
    if any(row.get('menu') != 'TONKATSU' or row.get('provenance') != 'SYNTHETIC' or
           row.get('equipment_profile') != 'synthetic-fryer-approved-v2' or
           row.get('status') not in {'COMPLETED', 'FAILED'} for row in selected):
        raise PriorHistoryUnavailable('comparable fryer jobs unavailable')
    if tuple(_integer(row.get('version'), 1) for row in selected) != versions:
        raise PriorHistoryUnavailable('fryer job version changed')
    try:
        streams = tuple(fryer_stream(row.get('source_binding')) for row in selected)
    except ValueError:
        raise PriorHistoryUnavailable('fryer job source unavailable') from None
    start, _ = _fryer_interval(current)
    previous_start, previous_end = _fryer_interval(prior)
    if streams[0] == streams[1] or not previous_start < previous_end <= start:
        raise PriorHistoryUnavailable('comparable fryer interval unavailable')
    for key, row in jobs.items():
        if key in (current_id, prior_id):
            continue
        binding = row.get('source_binding')
        same_device = isinstance(binding, dict) and binding.get('device_id') == 'mockprofile-100-a-001'
        if not same_device and row.get('menu') not in (None, 'TONKATSU') and row.get('equipment_profile') not in (
            None, 'synthetic-fryer', 'synthetic-fryer-approved-v2'
        ):
            continue
        placed = _instant(row.get('actual_placed_at'))
        if placed is None and row.get('actual_placed_at') is None and row.get('status') in {'PENDING', 'READY'}:
            continue
        if placed is not None and placed > start:
            continue
        if not same_device and isinstance(binding, dict) and binding.get('source_id') == 'synthetic-rest' and (
            isinstance(binding.get('device_id'), str) and binding['device_id'].startswith('mockprofile-')
        ):
            continue
        if not same_device or placed is None or placed >= previous_start:
            raise PriorHistoryUnavailable('selected fryer job is not uniquely immediately prior')
        _, other_end = _fryer_interval(row)
        if other_end > previous_start:
            raise PriorHistoryUnavailable('overlapping fryer history')
    return streams
