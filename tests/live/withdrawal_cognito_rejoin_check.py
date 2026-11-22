"""Disposable Cognito identity release and same-email re-enrollment.

Uses trusted synthetic setup, not customer email verification or OAuth login.
Email delivery is suppressed. Only this run's example.invalid users and random
DynamoDB keys are modified. No pool/client settings or real users are changed.
The seven-day clock is shifted; complete business erasure is NOT asserted.
"""
import argparse
import secrets
import time
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config

from foodtech_business_api.account_withdrawal import DynamoWithdrawalStore, GRACE_SECONDS
from foodtech_business_api.deletion_registry_cipher import DeletionRegistryCipher
from foodtech_business_api.guarded_deletion import GuardedDeletion
from foodtech_business_api.identity_vault import IdentityVault
from foodtech_business_api.principal_enrollment import FENCE_KEY, DynamoPrincipalEnrollment
from foodtech_business_api.withdrawal_dispatch import WithdrawalDispatcher
from foodtech_business_api.withdrawal_identity import CognitoWithdrawalRelease


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--business-env', required=True, type=Path)
    parser.add_argument('--allow-disposable-cognito-users', action='store_true', required=True)
    options = parser.parse_args()
    issuer = None
    for line in options.business_env.read_text().splitlines():
        name, separator, value = line.partition('=')
        if separator and name.strip() == 'FOODTECH_COGNITO_ISSUER':
            issuer = value.strip().strip('"').rstrip('/')
            break
    if not issuer or not issuer.startswith('https://cognito-idp.ap-southeast-2.amazonaws.com/'):
        raise RuntimeError('existing development Cognito issuer configuration is required')
    pool = issuer.rsplit('/', 1)[-1]
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    cognito = session.client('cognito-idp', config=Config(retries={'total_max_attempts': 1}))
    pool_config = cognito.describe_user_pool(UserPoolId=pool)['UserPool']
    # Do not trigger arbitrary customer-specific Lambda side effects in this test.
    if pool_config.get('LambdaConfig'):
        raise RuntimeError('disposable-user test requires a pool without Lambda triggers')
    token = uuid4().hex
    email = 'foodtech-withdrawal-test-' + token + '@example.invalid'
    username = email if 'email' in pool_config.get('UsernameAttributes', []) else 'foodtech-withdrawal-test-' + token
    started_at = time.time()
    resource, raw = session.resource('dynamodb'), session.client('dynamodb')
    names = {'auth': 'foodtech-auth-sessions-dev', 'vault': 'foodtech-cooking-jobs-dev',
             'capsule': 'foodtech-data-flow-dev', 'registry': 'foodtech-ai-execution-registry-dev'}
    tables = {name: resource.Table(value) for name, value in names.items()}
    owned_rows, subjects = [], set()
    create_attempted = False

    def get_user(identifier):
        try:
            return cognito.admin_get_user(UserPoolId=pool, Username=identifier)
        except cognito.exceptions.UserNotFoundException:
            return None

    def owned_subject(user):
        attributes = {a['Name']: a['Value'] for a in user['UserAttributes']}
        if attributes.get('email') != email or user['UserCreateDate'].timestamp() < started_at - 2:
            raise RuntimeError('refusing to modify a non-test Cognito identity')
        return attributes['sub']

    def own_row(domain, key):
        if 'Item' in tables[domain].get_item(Key=key, ConsistentRead=True):
            raise RuntimeError('generated test key already exists')
        owned_rows.append((domain, key))

    def create_identity():
        nonlocal create_attempted
        if get_user(username) is not None:
            raise RuntimeError('test username is already in use')
        create_attempted = True
        # This is an explicitly trusted fixture, never a real email-ownership check.
        password = 'Aa9!' + secrets.token_urlsafe(40)
        try:
            response = cognito.admin_create_user(UserPoolId=pool, Username=username,
                UserAttributes=[{'Name': 'email', 'Value': email}, {'Name': 'email_verified', 'Value': 'true'}],
                TemporaryPassword=password, MessageAction='SUPPRESS', ForceAliasCreation=False)
            attributes = {a['Name']: a['Value'] for a in response['User']['Attributes']}
            subject = attributes['sub']
            subjects.add(subject)
            user = get_user(subject)
            assert owned_subject(user) == subject
            cognito.admin_set_user_password(UserPoolId=pool, Username=subject, Password=password, Permanent=True)
            user = get_user(subject)
            assert owned_subject(user) == subject and user['Enabled'] is True and user['UserStatus'] == 'CONFIRMED'
            return subject
        finally:
            password = None

    enrollment = DynamoPrincipalEnrollment(table=tables['auth'], fence_key=FENCE_KEY + '#' + token)
    try:
        # Detect collisions before any write; never migrate an existing alias.
        if cognito.list_users(UserPoolId=pool, Filter='email = "' + email + '"', Limit=2)['Users']:
            raise RuntimeError('test email is already in use')
        own_row('auth', {'pk': enrollment.fence_key})
        enrollment.bootstrap()
        generation = enrollment.fence()
        old = create_identity()
        own_row('auth', {'pk': 'USER#' + old})
        enrollment.enroll(subject=old, expected_generation=generation)
        identity = IdentityVault(auth_table=names['auth'], vault_table=names['vault'], capsule_table=names['capsule'],
            approved_purposes=frozenset({'BILLING'}), client=raw)
        prepared = identity.prepare(authenticated_subject=old, request_id=str(uuid4()), expected_epoch=0)
        own_row('vault', {'pk': prepared.vault_id, 'sk': 'META'})
        own_row('capsule', {'pk': prepared.locator, 'sk': 'META'})
        identity.commit(authenticated_subject=old, prepared=prepared)
        request = str(uuid4())
        own_row('registry', {'pk': 'REGISTRY', 'sk': 'REQ#' + request})
        DynamoWithdrawalStore(table=tables['auth']).transition(subject=old, request_id=request,
            now=int(time.time()) - GRACE_SECONDS - 10, action='request')
        outputs = session.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']
        key = next(o['OutputValue'] for o in outputs if o['OutputKey'] == 'CredentialStorageKeyArn')
        cipher = DeletionRegistryCipher(key, 'dev', kms_client=session.client('kms'))
        deletion = GuardedDeletion(auth_table=names['auth'], vault_table=names['vault'], capsule_table=names['capsule'],
            registry_table=names['registry'], approved_purposes=frozenset({'BILLING'}), client=raw)
        dispatcher = WithdrawalDispatcher(auth_table=tables['auth'], identity=identity, cipher=cipher, deletion=deletion)
        assert dispatcher.step(subject=old, request_id=request) == {'state': 'LOCKED'}
        locked = tables['auth'].get_item(Key={'pk': 'USER#' + old}, ConsistentRead=True)['Item']
        release = CognitoWithdrawalRelease(auth_table=tables['auth'], registry_table=names['registry'],
            enrollment=enrollment, cognito_client=cognito, pool_id=pool)
        assert release.release(subject=old, request_id=request, expected_epoch=int(locked['epoch'])) == \
            {'state': 'COGNITO_REMOVED', 'deletion_complete': False}
        assert get_user(old) is None
        print('Actual expired locked Cognito subject removal and absence verified')

        generation = enrollment.fence()
        new = create_identity()
        assert new != old
        own_row('auth', {'pk': 'USER#' + new})
        fresh = enrollment.enroll(subject=new, expected_generation=generation)
        assert fresh == {'pk': 'USER#' + new, 'epoch': 0, 'suspended': False}
        old_guard = tables['auth'].get_item(Key={'pk': 'USER#' + old}, ConsistentRead=True)['Item']
        assert old_guard['identity_retired_request'] == request and old_guard['deletion_lock_request'] == request
        assert old_guard['withdrawal_status'] == 'DELETING' and old_guard['suspended'] is True
        # A retry of old-account deletion after username/email reuse must leave
        # the new subject intact and still must not report complete erasure.
        assert release.release(subject=old, request_id=request, expected_epoch=int(locked['epoch'])) == \
            {'state': 'COGNITO_REMOVED', 'deletion_complete': False}
        assert owned_subject(get_user(new)) == new and get_user(old) is None
        assert tables['registry'].get_item(Key={'pk': 'REGISTRY', 'sk': 'REQ#' + request}, ConsistentRead=True)['Item']['status'] == 'DELETING'
        print('Same email/username re-created with new subject; clean enrollment and old-delete retry isolation verified')
    finally:
        # Resolve only this unique run's candidate after an uncertain create
        # response. Always delete by verified immutable sub, never by email.
        if create_attempted:
            candidate = get_user(username)
            if candidate is not None:
                subjects.add(owned_subject(candidate))
            for subject in subjects:
                user = get_user(subject)
                if user is not None:
                    assert owned_subject(user) == subject
                    cognito.admin_delete_user(UserPoolId=pool, Username=subject)
                assert get_user(subject) is None
            assert get_user(username) is None
        for domain, row in reversed(owned_rows):
            tables[domain].delete_item(Key=row)
        assert all('Item' not in tables[domain].get_item(Key=row, ConsistentRead=True) for domain, row in owned_rows)
        print('Disposable Cognito identities and generated DynamoDB items removed; absence verified')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Provider exceptions can contain request/identity values. Never print
        # raw exceptions, request dictionaries, credentials or tracebacks here.
        print('Disposable rejoin check failed:', type(error).__name__)
        raise SystemExit(1) from None
