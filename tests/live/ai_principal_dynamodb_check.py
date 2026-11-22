"""Synthetic strict-principal AI/credential transactions on existing dev tables.

No provider is called and no real key, KMS, Cognito or role is changed. Credential
encryption is explicitly replaced with a synthetic fixture; this test proves
the account guard and atomic writes, not encryption or end-user authentication.
"""
import os
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import boto3

from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_ai_gateway.registry import DynamoRegistry, RegistryConflict
from foodtech_credential_registration.registration import CredentialRegistrar, RegistrationError


def main():
    os.environ['FOODTECH_PRINCIPAL_PROVISIONING_ENABLED'] = 'true'
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    db, client = session.resource('dynamodb'), session.client('dynamodb')
    auth, table, vault = (db.Table(name) for name in ('foodtech-auth-sessions-dev',
        'foodtech-ai-execution-registry-dev', 'foodtech-ai-credential-vault-dev'))
    old, new, credential_owner = (str(uuid4()) for _ in range(3))
    run, new_run, blocked_run, quote = (str(uuid4()) for _ in range(4))
    provider = 'synthetic-' + uuid4().hex
    period = f'{3000 + uuid4().int % 6000:04d}-{1 + uuid4().int % 12:02d}'
    registry = DynamoRegistry(table.name, period, resource=db, client=client, auth_table=auth.name)
    auth_keys = [{'pk': 'USER#' + subject} for subject in (old, new, credential_owner)]
    keys = [{'pk': 'LIMIT#' + period + '#' + kind + '#' + name, 'sk': 'META'}
            for kind, name in (('TENANT', old), ('TENANT', new), ('PROVIDER', provider), ('PROJECT', 'GLOBAL'))]
    keys += [{'pk': 'EXEC#' + name, 'sk': sk} for name in (run, new_run, blocked_run) for sk in ('META', 'RESULT')]
    keys += [{'pk': 'ACTIVE#' + subject, 'sk': 'EXEC#' + name} for subject, name in ((old, run), (new, new_run))]
    keys.append({'pk': 'QUOTE#' + quote, 'sk': 'META'})
    credential_keys, buffers = [], []
    if any('Item' in auth.get_item(Key=key, ConsistentRead=True) for key in auth_keys) or \
            any('Item' in table.get_item(Key=key, ConsistentRead=True) for key in keys):
        raise RuntimeError('generated fixture scope already exists')
    def execution(name, subject):
        return Execution(name, subject, Feature.GENERAL_GUIDE, 'synthetic-model', None, 'SYNTHETIC', Decimal(0))
    def rejected(action, exception):
        try:
            action()
        except exception as error:
            if isinstance(error, RegistrationError) and error.code != 'TENANT_UNAVAILABLE':
                raise
            return
        raise AssertionError('missing principal was accepted')
    class Transfer:
        def decrypt(self, *_):
            value = bytearray(b'SYNTHETIC-NOT-A-PROVIDER-KEY')
            buffers.append(value)
            return value
    class Writer:
        table = SimpleNamespace(name=vault.name)
        def encrypt_item(self, credential_id, pseudonym, provider, plaintext, **_):
            key = {'pk': 'CREDENTIAL#' + credential_id, 'sk': 'META'}
            if any('Item' in t.get_item(Key=key, ConsistentRead=True) for t in (vault, table)):
                raise RuntimeError('generated credential already exists')
            credential_keys.append(key)
            return {**key, 'ciphertext': b'SYNTHETIC-AUTH-GUARD-ONLY', 'tenant_pseudonym': pseudonym}
    registrar = CredentialRegistrar(Writer(), table.name, Transfer(),
        SimpleNamespace(create=lambda *_: 'SYNTHETIC-AUTH-GUARD'), auth_table=auth.name, dynamodb_client=client)
    def register(subject):
        return registrar.register(subject, 'openai', 'synthetic-version', 'synthetic-transport', 0)
    try:
        rejected(lambda: registry.create_execution(execution(blocked_run, old)), RegistryConflict)
        rejected(lambda: registry.create_quote(quote, {'tenant_id': old}), RegistryConflict)
        rejected(lambda: registry.ensure_trial_tenant_limit(old, Decimal(0), 1), RegistryConflict)
        rejected(lambda: register(old), RegistrationError)
        assert all('Item' not in table.get_item(Key=key, ConsistentRead=True) for key in keys)
        auth.put_item(Item={**auth_keys[0], 'epoch': 0, 'suspended': False}, ConditionExpression='attribute_not_exists(pk)')
        for kind, name in (('TENANT', old), ('PROVIDER', provider), ('PROJECT', 'GLOBAL')):
            registry.create_limit(kind, name, Decimal(0), **({'calls_remaining': 1} if kind == 'TENANT' else {}))
        registry.create_execution(execution(run, old))
        registry.begin_request()
        observed = registry.execution(run)
        registry.budget(old, provider)
        auth.delete_item(Key=auth_keys[0])
        rejected(lambda: registry.reserve(observed, provider), RegistryConflict)
        assert registry.execution_state(run) == 'PENDING' and registry.budget(old, provider).calls_remaining == 1
        assert registry.active_execution_keys(old)['keys'] == []
        auth.put_item(Item={**auth_keys[1], 'epoch': 0, 'suspended': False}, ConditionExpression='attribute_not_exists(pk)')
        registry.create_limit('TENANT', new, Decimal(0), calls_remaining=1)
        registry.create_execution(execution(new_run, new))
        registry.begin_request()
        observed = registry.execution(new_run)
        registry.budget(new, provider)
        registry.reserve(observed, provider)
        auth.delete_item(Key=auth_keys[1])
        registry.begin_request()
        registry.finish(new_run, succeeded=False, actual_cost_usd=Decimal(0), error_code='SYNTHETIC_TERMINAL')
        assert registry.execution_state(new_run) == 'FAILED' and registry.active_execution_keys(new)['keys'] == []
        assert all('Item' not in auth.get_item(Key=key, ConsistentRead=True) for key in auth_keys[:2])
        auth.put_item(Item={**auth_keys[2], 'epoch': 0, 'suspended': False}, ConditionExpression='attribute_not_exists(pk)')
        register(credential_owner)
        accepted = credential_keys[-1]
        original = client.transact_write_items
        def lose_principal(**kwargs):
            auth.delete_item(Key=auth_keys[2])
            return original(**kwargs)
        registrar.client = SimpleNamespace(transact_write_items=lose_principal)
        rejected(lambda: register(credential_owner), RegistrationError)
        for key in credential_keys:
            for t in (vault, table):
                assert ('Item' in t.get_item(Key=key, ConsistentRead=True)) is (key == accepted)
        assert all(not any(buffer) for buffer in buffers)
        print('Missing principal creation/reservation/registration rejected; valid account and terminal settlement verified')
        print('Post-encryption account loss rejected both credential writes; all plaintext fixture buffers cleared')
    finally:
        for key in keys:
            table.delete_item(Key=key)
        for key in credential_keys:
            vault.delete_item(Key=key)
            table.delete_item(Key=key)
        for key in auth_keys:
            auth.delete_item(Key=key)
        assert all('Item' not in table.get_item(Key=key, ConsistentRead=True) for key in keys)
        assert all('Item' not in t.get_item(Key=key, ConsistentRead=True) for key in credential_keys for t in (vault, table))
        assert all('Item' not in auth.get_item(Key=key, ConsistentRead=True) for key in auth_keys)
        print('All generated AI, credential and principal items removed; absence verified')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('AI principal check failed:', type(error).__name__)
        raise SystemExit(1) from None
