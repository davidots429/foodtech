"""Opt-in synthetic credential context check; creates one isolated vault item."""
import os
import secrets
from uuid import uuid4

import boto3

from foodtech_ai_gateway.policy import Credential
from foodtech_ai_gateway.vault import DynamoCredentialVault
from foodtech_credential_registration.storage import CredentialWriter


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    outputs = session.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']
    values = {entry['OutputKey']: entry['OutputValue'] for entry in outputs}
    table_name = 'foodtech-ai-credential-vault-dev'
    if values.get('CredentialVaultTableName') != table_name:
        raise SystemExit('unexpected vault table')
    key_arn = values['CredentialStorageKeyArn']
    resource, kms = session.resource('dynamodb'), session.client('kms')
    table = resource.Table(table_name)
    writer = CredentialWriter(table_name, 'dev', key_arn, resource=resource, kms_client=kms)
    vault = DynamoCredentialVault(table_name, 'dev', {key_arn}, resource=resource, kms_client=kms)
    identifier = 'synthetic-context-' + uuid4().hex
    tenant = 'synthetic-tenant-' + uuid4().hex
    pseudonym = 'psn:v1:credential:2026-11:v1:' + secrets.token_urlsafe(32)
    key = {'pk': f'CREDENTIAL#{identifier}', 'sk': 'META'}
    try:
        rejected = bytearray(b'dummy')
        try:
            writer.store(identifier, 'raw-subject', 'openai', rejected, pool_version='test', shard_id='0')
        except ValueError:
            pass
        else:
            raise AssertionError('raw subject accepted')
        if any(rejected) or table.get_item(Key=key, ConsistentRead=True).get('Item'):
            raise AssertionError('rejection did not clear or prevent storage')
        secret = bytearray(b'synthetic-dummy-credential')
        writer.store(identifier, pseudonym, 'openai', secret, pool_version='test', shard_id='0')
        if any(secret):
            raise AssertionError('input buffer was not cleared')
        credential = Credential(identifier, tenant, 'openai', 'ACTIVE', None, pseudonym, f'vault:{identifier}')
        plaintext = vault.decrypt(vault.read(credential))
        try:
            if plaintext != b'synthetic-dummy-credential':
                raise AssertionError('credential round trip mismatch')
        finally:
            plaintext[:] = b'\x00' * len(plaintext)
    finally:
        table.delete_item(Key=key)
    print('raw subject rejected; synthetic pseudonym encrypt/decrypt and cleanup verified')


if __name__ == '__main__':
    main()
