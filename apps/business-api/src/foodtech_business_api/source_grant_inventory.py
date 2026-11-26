"""Encrypted reference inventory before final principal removal.

Internal trusted maintenance only. This preserves catalog references for backup
reconciliation; it does not establish writer quiescence, run restore deletion,
or certify whole-account erasure. No runtime or IAM is provisioned here.
"""
import hashlib
import json
from uuid import UUID

from .subject_identity import valid_subject

from boto3.dynamodb.types import Binary, TypeDeserializer

from .source_grant_contract import SourceGrantConflict, _grant_mapping

PAGE_SIZE = 50
MAX_GRANTS = 2000
HEAD = 'SOURCE_GRANT_INVENTORY'


def _pack(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if not 1 <= len(raw) <= 16384:
        raise SourceGrantConflict('source reference inventory exceeds checkpoint bounds')
    return raw


class SourceGrantInventory:
    def __init__(self, *, deletion, progress_store, cipher):
        if deletion.registrar.auth_table != progress_store.auth_table or \
                deletion.registry_table != progress_store.registry_table:
            raise ValueError('source inventory lock boundary mismatch')
        catalog = deletion.registrar.catalog_table
        if progress_store.progress_table == catalog:
            raise ValueError('separate source inventory progress storage required')
        source_arn = getattr(cipher, 'source_table_arn', '')
        if not isinstance(source_arn, str) or not source_arn.startswith('arn:aws:dynamodb:ap-southeast-2:') or \
                source_arn.rsplit(':', 1)[-1] != 'table/' + catalog:
            raise ValueError('source inventory cipher boundary mismatch')
        self.deletion, self.store, self.cipher = deletion, progress_store, cipher
        self.catalog_table = catalog
        self.decoder = TypeDeserializer()

    @staticmethod
    def _records(grants):
        grants = _grant_mapping({'source_read_grants': grants})
        if len(grants) > MAX_GRANTS:
            raise SourceGrantConflict('source reference inventory exceeds supported scope')
        refs = sorted(grants.items())
        digest = hashlib.sha256(json.dumps(grants, sort_keys=True, separators=(',', ':'),
                                           ensure_ascii=True).encode('ascii')).hexdigest()
        pages = [(f'SOURCE_GRANT_PAGE_{i // PAGE_SIZE:04d}',
                  _pack({'schema': 1, 'index': i // PAGE_SIZE, 'references': refs[i:i + PAGE_SIZE]}))
                 for i in range(0, len(refs), PAGE_SIZE)]
        head = _pack({'schema': 1, 'count': len(refs), 'pages': len(pages), 'reference_sha256': digest})
        return pages + [(HEAD, head)]

    def capture(self, *, subject, request_id, expected_epoch, max_writes=2):
        """Capture bounded immutable encrypted pages while the Registry is DELETING."""
        if type(max_writes) is not int or not 1 <= max_writes <= 10:
            raise ValueError('invalid source inventory write bound')
        snapshot = self.deletion._locked(subject=subject, request_id=request_id, expected_epoch=expected_epoch)
        mapping = snapshot[-1]
        records, writes = self._records(mapping), 0
        for domain, raw in records:
            scope = {'subject': subject, 'request_id': request_id, 'domain': domain}
            previous = self.store.load_checkpoint(cipher=self.cipher, **scope)
            if previous is not None:
                if previous != {'version': 1, 'state': 'VERIFYING', 'checkpoint': raw}:
                    raise SourceGrantConflict('source reference inventory changed')
                continue
            if writes == max_writes:
                return {'state': 'IN_PROGRESS', 'writes': writes, 'deletion_complete': False}
            if self.deletion._locked(subject=subject, request_id=request_id, expected_epoch=expected_epoch) != snapshot:
                raise SourceGrantConflict('source grant account changed')
            self.store.save_checkpoint(cipher=self.cipher, **scope, expected_version=0,
                                       state='VERIFYING', checkpoint=raw)
            writes += 1
        if self.deletion._locked(subject=subject, request_id=request_id, expected_epoch=expected_epoch) != snapshot:
            raise SourceGrantConflict('source grant account changed')
        return {'state': 'CAPTURED', 'writes': writes, 'deletion_complete': False}

    @staticmethod
    def _scope(subject, request_id):
        try:
            if not valid_subject(subject) or not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid source recovery inventory scope') from None

    def _open_record(self, *, subject, request_id, domain):
        key = {'pk': 'REQ#' + request_id, 'sk': 'DOMAIN#' + domain}
        result = self.store.client.get_item(TableName=self.store.progress_table,
            Key=self.store._encode(key), ConsistentRead=True,
            ProjectionExpression='pk, sk, #version, #state, encrypted_checkpoint',
            ExpressionAttributeNames={'#version': 'version', '#state': 'state'})
        try:
            item = {k: self.decoder.deserialize(v) for k, v in result['Item'].items()}
            if item.get('pk') != key['pk'] or item.get('sk') != key['sk'] or \
                    type(item.get('version')) is bool or item.get('version') != 1 or item.get('state') != 'VERIFYING':
                raise ValueError
            ciphertext = item['encrypted_checkpoint']
            if isinstance(ciphertext, Binary):
                ciphertext = ciphertext.value
            if not isinstance(ciphertext, (bytes, bytearray)) or not 1 <= len(ciphertext) <= 65536:
                raise ValueError
            ciphertext = bytes(ciphertext)
            opened = self.cipher.open(subject=subject, request_id=request_id, domain=domain,
                                      version=1, state='VERIFYING', ciphertext=ciphertext)
            if not isinstance(opened, bytes) or not 1 <= len(opened) <= 16384:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceGrantConflict('encrypted source recovery inventory unavailable') from None
        return key, opened, ciphertext

    def retirement_conditions(self, *, subject, request_id, grants):
        """Verify saved pages, returning native DynamoDB final-transaction checks.

        Caller must independently verify Auth lock, completed Registry and every
        catalog row absent; all returned checks MUST join that final transaction.
        These checks preserve ciphertext at the instant of removal, not its
        subsequent retention or successful restore reconciliation.
        """
        self._scope(subject, request_id)
        conditions = []
        for domain, raw in self._records(grants):
            key, opened, ciphertext = self._open_record(subject=subject, request_id=request_id, domain=domain)
            if opened != raw:
                raise SourceGrantConflict('encrypted source recovery inventory changed')
            conditions.append({'ConditionCheck': {'TableName': self.store.progress_table, 'Key': key,
                'ConditionExpression': '#version = :one AND #state = :verified AND encrypted_checkpoint = :ciphertext',
                'ExpressionAttributeNames': {'#version': 'version', '#state': 'state'},
                'ExpressionAttributeValues': {':one': 1, ':verified': 'VERIFYING', ':ciphertext': ciphertext},
                'ReturnValuesOnConditionCheckFailure': 'NONE'}})
        return conditions

    def use_references(self, *, subject, request_id, action):
        """Use the saved references without needing the removed Auth record.

        A separately authorized restore worker MUST verify the authoritative
        request is not canceled, isolate restored writers, and reauthorize each
        mutation. This is only decryption/consistency, never caller authorization
        or a deletion receipt. No references or action result leave this method.
        Python string/SDK copies cannot be guaranteed erased.
        """
        self._scope(subject, request_id)
        if not callable(action):
            raise ValueError('trusted source inventory recovery action required')
        references = {}
        try:
            _, head_raw, _ = self._open_record(subject=subject, request_id=request_id, domain=HEAD)
            head = json.loads(head_raw)
            if not isinstance(head, dict) or set(head) != {'schema', 'count', 'pages', 'reference_sha256'} or \
                    type(head['schema']) is not int or head['schema'] != 1 or \
                    type(head['count']) is not int or not 0 <= head['count'] <= MAX_GRANTS or \
                    type(head['pages']) is not int or head['pages'] != (head['count'] + PAGE_SIZE - 1) // PAGE_SIZE:
                raise SourceGrantConflict('invalid source recovery inventory head')
            raw_pages = []
            for index in range(head['pages']):
                domain = f'SOURCE_GRANT_PAGE_{index:04d}'
                _, raw, _ = self._open_record(subject=subject, request_id=request_id, domain=domain)
                page = json.loads(raw)
                if not isinstance(page, dict) or set(page) != {'schema', 'index', 'references'} or \
                        type(page['schema']) is not int or page['schema'] != 1 or \
                        type(page['index']) is not int or page['index'] != index or \
                        not isinstance(page['references'], list) or \
                        len(page['references']) != min(PAGE_SIZE, head['count'] - index * PAGE_SIZE):
                    raise SourceGrantConflict('invalid source recovery inventory page')
                for pair in page['references']:
                    if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) or pair[0] in references:
                        raise SourceGrantConflict('invalid source recovery inventory reference')
                    references[pair[0]] = pair[1]
                raw_pages.append((domain, raw))
            if self._records(references) != raw_pages + [(HEAD, head_raw)]:
                raise SourceGrantConflict('source recovery inventory digest mismatch')
            count = len(references)
            action(references)
            return {'references_used': count, 'deletion_complete': False}
        except (TypeError, ValueError, KeyError, UnicodeError):
            raise SourceGrantConflict('source recovery inventory unavailable') from None
        finally:
            references.clear()
