"""Synthetic expiry-to-lock test on four existing development tables.

No business data is erased, no production role is assumed, and Cognito is not
changed. Synthetic request times are shifted back seven days explicitly.
"""
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import boto3

from foodtech_business_api.account_withdrawal import DynamoWithdrawalStore, GRACE_SECONDS
from foodtech_business_api.deletion_registry_cipher import DeletionRegistryCipher
from foodtech_business_api.guarded_deletion import GuardedDeletion
from foodtech_business_api.identity_vault import IdentityVault, IdentityVaultConflict
from foodtech_business_api.withdrawal_dispatch import WithdrawalDispatcher
from foodtech_business_api.principal_enrollment import DynamoPrincipalEnrollment, FENCE_KEY
from foodtech_business_api.withdrawal_identity import CognitoWithdrawalRelease


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity-release-env', type=Path)
    options = parser.parse_args()
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    resource = session.resource('dynamodb')
    client = session.client('dynamodb')
    names = {'auth': 'foodtech-auth-sessions-dev', 'vault': 'foodtech-cooking-jobs-dev',
             'capsule': 'foodtech-data-flow-dev', 'registry': 'foodtech-ai-execution-registry-dev'}
    tables = {key: resource.Table(name) for key, name in names.items()}
    subject, request_id = str(uuid4()), str(uuid4())
    keys = {'auth': {'pk': f'USER#{subject}'}, 'registry': {'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}}
    identity = IdentityVault(auth_table=names['auth'], vault_table=names['vault'], capsule_table=names['capsule'],
                             approved_purposes=frozenset({'BILLING'}), client=client)
    prepared = identity.prepare(authenticated_subject=subject, request_id=str(uuid4()), expected_epoch=0)
    keys.update(vault={'pk': prepared.vault_id, 'sk': 'META'}, capsule={'pk': prepared.locator, 'sk': 'META'})
    if any('Item' in tables[name].get_item(Key=key, ConsistentRead=True) for name, key in keys.items()):
        raise RuntimeError('generated test identities already exist')
    created = False
    internal_id = None
    extra_auth_keys = []
    try:
        tables['auth'].put_item(Item={**keys['auth'], 'epoch': 0}, ConditionExpression='attribute_not_exists(pk)')
        created = True
        identity.commit(authenticated_subject=subject, prepared=prepared)
        withdrawal = DynamoWithdrawalStore(table=tables['auth'])
        requested = int(time.time()) - GRACE_SECONDS - 10
        withdrawal.transition(subject=subject, request_id=request_id, now=requested, action='request')
        try:
            identity.resolve_deletion(authenticated_subject=subject, expected_epoch=1, withdrawal_request_id=request_id)
        except IdentityVaultConflict:
            pass
        else:
            raise AssertionError('pending withdrawal must not resolve a deletion snapshot')
        expired = withdrawal.transition(subject=subject, request_id=request_id, now=int(time.time()), action='expire')
        snapshot, internal_id = identity.resolve_deletion(authenticated_subject=subject,
            expected_epoch=int(expired['epoch']), withdrawal_request_id=request_id)
        output = session.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']
        key_arn = next(item['OutputValue'] for item in output if item['OutputKey'] == 'CredentialStorageKeyArn')
        cipher = DeletionRegistryCipher(key_arn, 'dev', kms_client=session.client('kms'))
        encrypted = cipher.seal(request_id, internal_id, bytearray(snapshot.capsule_locator.encode('ascii')))
        lock = GuardedDeletion(auth_table=names['auth'], vault_table=names['vault'], capsule_table=names['capsule'],
            registry_table=names['registry'], approved_purposes=frozenset({'BILLING'}), client=client)
        args = dict(subject=subject, request_id=request_id, snapshot=snapshot,
            encrypted_user_id=encrypted['internal_user_id'], encrypted_capsule_locator=encrypted['capsule_locator'],
            requested_at=datetime.fromtimestamp(requested, timezone.utc).isoformat(), policy_version='withdrawal-7d-v1')
        # A concurrent schedule change must reject all four writes atomically.
        tables['auth'].update_item(Key=keys['auth'], UpdateExpression='SET withdrawal_deadline = :changed',
            ExpressionAttributeValues={':changed': snapshot.withdrawal_deadline + 1})
        try:
            lock.lock_and_register(**args)
        except client.exceptions.TransactionCanceledException:
            assert 'Item' not in tables['registry'].get_item(Key=keys['registry'], ConsistentRead=True)
            assert tables['vault'].get_item(Key=keys['vault'], ConsistentRead=True)['Item']['state'] == 'ACTIVE'
            assert tables['capsule'].get_item(Key=keys['capsule'], ConsistentRead=True)['Item']['state'] == 'ACTIVE'
        else:
            raise AssertionError('changed schedule must reject the lock transaction')
        tables['auth'].update_item(Key=keys['auth'], UpdateExpression='SET withdrawal_deadline = :restored',
            ExpressionAttributeValues={':restored': snapshot.withdrawal_deadline})
        dispatcher = WithdrawalDispatcher(auth_table=tables['auth'], identity=identity, cipher=cipher, deletion=lock)
        assert dispatcher.step(subject=subject, request_id=request_id) == {'state': 'LOCKED'}
        assert dispatcher.step(subject=subject, request_id=request_id) == {'state': 'RECOVERY_REQUIRED'}
        locked = tables['auth'].get_item(Key=keys['auth'], ConsistentRead=True)['Item']
        assert locked['suspended'] is True and locked['deletion_lock_request'] == request_id
        assert all(tables[name].get_item(Key=keys[name], ConsistentRead=True)['Item']['state'] == 'DELETION_LOCKED'
                   for name in ('vault', 'capsule'))
        assert tables['registry'].get_item(Key=keys['registry'], ConsistentRead=True)['Item']['status'] == 'DELETING'
        print('Pending rejection, changed-deadline atomic rejection, expiry dispatch and four-domain lock passed')
        if options.identity_release_env:
            issuer = None
            for line in options.identity_release_env.read_text().splitlines():
                name, separator, value = line.partition('=')
                if separator and name.strip() == 'FOODTECH_COGNITO_ISSUER':
                    issuer = value.strip().strip('"').rstrip('/')
                    break
            if not issuer or not issuer.startswith('https://cognito-idp.ap-southeast-2.amazonaws.com/'):
                raise RuntimeError('existing development Cognito issuer configuration is required')
            control_key = FENCE_KEY + '#' + str(uuid4())
            if 'Item' in tables['auth'].get_item(Key={'pk': control_key}, ConsistentRead=True):
                raise RuntimeError('generated test control already exists')
            extra_auth_keys.append({'pk': control_key})
            enrollment = DynamoPrincipalEnrollment(table=tables['auth'], fence_key=control_key)
            enrollment.bootstrap()
            cognito = session.client('cognito-idp')
            def forbidden_delete(**kwargs):
                raise RuntimeError('this test must never delete a Cognito user')
            readonly_cognito = SimpleNamespace(admin_get_user=cognito.admin_get_user,
                admin_delete_user=forbidden_delete, exceptions=cognito.exceptions)
            release = CognitoWithdrawalRelease(auth_table=tables['auth'], registry_table=names['registry'],
                enrollment=enrollment, cognito_client=readonly_cognito, pool_id=issuer.rsplit('/', 1)[-1])
            assert release.release(subject=subject, request_id=request_id, expected_epoch=int(locked['epoch'])) == \
                {'state': 'COGNITO_REMOVED', 'deletion_complete': False}
            account = tables['auth'].get_item(Key=keys['auth'], ConsistentRead=True)['Item']
            assert account['identity_retired_request'] == request_id and account['suspended'] is True
            assert account['deletion_lock_request'] == request_id
            assert tables['registry'].get_item(Key=keys['registry'], ConsistentRead=True)['Item']['status'] == 'DELETING'
            print('Random-subject Cognito absence and conditional identity marker passed; no Cognito deletion performed')
    finally:
        if isinstance(internal_id, bytearray):
            internal_id[:] = b'\x00' * len(internal_id)
        if created:
            for name, key in keys.items():
                tables[name].delete_item(Key=key)
            for key in extra_auth_keys:
                tables['auth'].delete_item(Key=key)
            assert all('Item' not in tables[name].get_item(Key=key, ConsistentRead=True) for name, key in keys.items())
            assert all('Item' not in tables['auth'].get_item(Key=key, ConsistentRead=True) for key in extra_auth_keys)
            print('All generated synthetic items removed and absence verified')


if __name__ == '__main__':
    main()
