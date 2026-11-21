"""Opt-in synthetic registration lock test against existing development tables."""
import os
from types import SimpleNamespace
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from foodtech_credential_registration.registration import CredentialRegistrar


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    db = session.resource('dynamodb')
    vault, registry = db.Table('foodtech-ai-credential-vault-dev'), db.Table('foodtech-ai-execution-registry-dev')
    auth = db.Table('foodtech-auth-sessions-dev')
    tenant, auth_key = 'synthetic-' + uuid4().hex, None
    auth_key = {'pk': f'USER#{tenant}'}
    keys, auth_created = [], False
    class Transfer:
        def decrypt(self, *_): return bytearray(b'synthetic')
    class Pseudonym:
        def create(self, *_): return 'synthetic-pseudonym'
    class Writer:
        table = SimpleNamespace(name=vault.name)
        def encrypt_item(self, credential_id, pseudonym, provider, plaintext, **_):
            key = {'pk': f'CREDENTIAL#{credential_id}', 'sk': 'META'}
            keys.append(key)
            return {**key, 'credential_id': credential_id, 'tenant_pseudonym': pseudonym}
    registrar = CredentialRegistrar(Writer(), registry.name, Transfer(), Pseudonym(),
                                    auth_table=auth.name, dynamodb_client=session.client('dynamodb'))
    try:
        auth.put_item(Item={**auth_key, 'epoch': 0, 'deletion_lock_request': str(uuid4())},
                      ConditionExpression='attribute_not_exists(pk)')
        auth_created = True
        def rejected(epoch):
            try:
                registrar.register(tenant, 'openai', 'v1', 'unused', epoch)
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') != 'TransactionCanceledException':
                    raise
                return
            raise AssertionError('registration was accepted')
        rejected(0)
        auth.update_item(Key=auth_key, UpdateExpression='REMOVE deletion_lock_request')
        rejected(1)
        registrar.register(tenant, 'openai', 'v1', 'unused', 0)
        if any(vault.get_item(Key=key, ConsistentRead=True).get('Item') or
               registry.get_item(Key=key, ConsistentRead=True).get('Item') for key in keys[:-1]):
            raise AssertionError('rejected registration left data')
        if not vault.get_item(Key=keys[-1], ConsistentRead=True).get('Item') or \
                not registry.get_item(Key=keys[-1], ConsistentRead=True).get('Item'):
            raise AssertionError('valid registration was not saved to both tables')
    finally:
        for key in keys:
            vault.delete_item(Key=key)
            registry.delete_item(Key=key)
        if auth_created:
            auth.delete_item(Key=auth_key)
    print('registration lock, epoch, atomic write and cleanup verified')


if __name__ == '__main__':
    main()
