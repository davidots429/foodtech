"""Prepare a locked, encrypted credential-vault target; never delete it here.

An authenticated worker must first quiesce all customer executions and verify
legacy reservations. This preparer does not prove either condition or authorize
its caller. The final transaction must recheck both metadata records and lock.
"""
import json
import re

from .legacy_tenant_inventory import resolve_credential_vault


class CredentialDeletionPlan:
    def __init__(self, *, capture, progress_store, cipher, source_table, vault_table):
        if capture.progress_store is not progress_store or capture.cipher is not cipher or \
                capture.source_table is not source_table or source_table.name == vault_table.name or \
                vault_table.name in {progress_store.auth_table, progress_store.registry_table, progress_store.progress_table}:
            raise ValueError('invalid credential planning dependencies')
        self.capture, self.progress_store, self.cipher = capture, progress_store, cipher
        self.source_table, self.vault_table = source_table, vault_table

    def observe_readiness(self, *, observer, subject, request_id, index):
        """Recheck metadata and execution state without authorizing deletion."""
        if observer.capture is not self.capture or observer.progress_store is not self.progress_store or \
                observer.source_table is not self.source_table:
            raise ValueError('execution observer does not match credential plan')
        self.prepare(subject=subject, request_id=request_id, index=index)
        scope = {'subject': subject, 'request_id': request_id, 'domain': f'AI_CREDENTIAL_{index:012d}'}
        saved = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        if not saved:
            raise ValueError('credential deletion plan unavailable')
        try:
            plan = json.loads(saved['checkpoint'])
            if saved['state'] != 'IN_PROGRESS' or saved['version'] != 1 or \
                    set(plan) != {'schema', 'partition', 'vault_arn', 'tenant_pseudonym'} or \
                    plan['schema'] != 1 or plan['vault_arn'] != self.vault_table.table_arn or \
                    not re.fullmatch(r'CREDENTIAL#[A-Za-z0-9_-]{1,128}', plan['partition']):
                raise ValueError
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise ValueError('invalid credential deletion plan') from None
        location = resolve_credential_vault(self.source_table, self.vault_table, tenant=subject,
                                             credential_id=plan['partition'].removeprefix('CREDENTIAL#'))
        if location.key['pk'] != plan['partition'] or location.tenant_pseudonym != plan['tenant_pseudonym']:
            raise ValueError('credential metadata changed')
        result = observer.observe(subject=subject, request_id=request_id)
        if not isinstance(result, dict) or result.get('observation') not in {'BLOCKED', 'CLEAR_OBSERVED'}:
            raise ValueError('invalid execution observation')
        # An execution check may take multiple pages. Reject a changed target
        # before returning; final deletion must still repeat transactional guards.
        after = resolve_credential_vault(self.source_table, self.vault_table, tenant=subject,
                                          credential_id=plan['partition'].removeprefix('CREDENTIAL#'))
        if after.key != location.key or after.tenant_pseudonym != location.tenant_pseudonym:
            raise ValueError('credential metadata changed')
        return {'index': index, 'observation': result['observation']}

    def prepare(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid credential inventory index')
        target = next((target for n, target in enumerate(self.capture.partitions(
            subject=subject, request_id=request_id)) if n == index), None)
        if target is None or not re.fullmatch(r'CREDENTIAL#[A-Za-z0-9_-]{1,128}', target.partition):
            raise ValueError('credential inventory target unavailable')
        vault_arn = getattr(self.vault_table, 'table_arn', None)
        if not isinstance(vault_arn, str) or not re.fullmatch(
                r'arn:aws:dynamodb:ap-southeast-2:[0-9]{12}:table/[A-Za-z0-9_.-]{3,255}', vault_arn) or \
                vault_arn.split(':')[4] != self.cipher.source_table_arn.split(':')[4]:
            raise ValueError('invalid credential vault scope')
        scope = {'subject': subject, 'request_id': request_id, 'domain': f'AI_CREDENTIAL_{index:012d}'}
        previous = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        if previous is not None:
            try:
                plan = json.loads(previous['checkpoint'])
                if previous['state'] != 'IN_PROGRESS' or previous['version'] != 1 or \
                        set(plan) != {'schema', 'partition', 'vault_arn', 'tenant_pseudonym'} or \
                        plan['schema'] != 1 or plan['partition'] != target.partition or plan['vault_arn'] != vault_arn or \
                        not isinstance(plan['tenant_pseudonym'], str) or not 1 <= len(plan['tenant_pseudonym']) <= 256:
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise ValueError('invalid credential deletion plan') from None
            return {'index': index, 'state': 'PREPARED', 'version': 1}
        location = resolve_credential_vault(self.source_table, self.vault_table, tenant=subject,
                                            credential_id=target.partition.removeprefix('CREDENTIAL#'))
        plan = {'schema': 1, 'partition': location.key['pk'], 'vault_arn': vault_arn,
                'tenant_pseudonym': location.tenant_pseudonym}
        raw = json.dumps(plan, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
        version = self.progress_store.save_checkpoint(cipher=self.cipher, **scope, expected_version=0,
                                                      state='IN_PROGRESS', checkpoint=raw)
        return {'index': index, 'state': 'PREPARED', 'version': version}
