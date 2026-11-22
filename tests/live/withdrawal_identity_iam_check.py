"""Read-only simulator gate for an identity-worker policy candidate.

Accepts a plain IAM document or IAM Policy Autopilot's Policies output. Never
creates, edits or attaches policies. A passing result is NOT a deployed-role,
SCP, KMS table-encryption or real ECS execution verification.
"""
import argparse
import json
import re
from pathlib import Path

import boto3

REGION = 'ap-southeast-2'


def policy_inputs(document):
    if isinstance(document, dict) and set(document) == {'Policies'}:
        entries = document['Policies']
        if not isinstance(entries, list) or not entries:
            raise ValueError('missing policy candidates')
        documents = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('PolicyType') != 'Identity':
                raise ValueError('identity policy candidates are required')
            value = entry['Policy']
            documents.append(json.loads(value) if isinstance(value, str) else value)
    else:
        documents = [document]
    if any(not isinstance(value, dict) or value.get('Version') != '2012-10-17' or
           not isinstance(value.get('Statement'), list) or not value['Statement'] for value in documents):
        raise ValueError('invalid policy candidate')
    # Preserve the candidate exactly: this verifier never narrows or rewrites it.
    return [json.dumps(value) for value in documents]


def scenarios(account, stage, pool_id):
    if not re.fullmatch(r'[0-9]{12}', account) or not re.fullmatch(r'[a-z0-9-]{1,32}', stage) or \
            not re.fullmatch(r'ap-southeast-2_[A-Za-z0-9]+', pool_id):
        raise ValueError('invalid development resource scope')
    auth = f'arn:aws:dynamodb:{REGION}:{account}:table/foodtech-auth-sessions-{stage}'
    registry = f'arn:aws:dynamodb:{REGION}:{account}:table/foodtech-deletion-registry-{stage}'
    unrelated = f'arn:aws:dynamodb:{REGION}:{account}:table/foodtech-unrelated-synthetic'
    pool = f'arn:aws:cognito-idp:{REGION}:{account}:userpool/{pool_id}'
    checks = []
    def add(name, action, resource, allow, *, key=None, transaction=None, attributes=None, select=None):
        context = [{'ContextKeyName': 'aws:RequestedRegion', 'ContextKeyValues': [REGION], 'ContextKeyType': 'string'}]
        for name_, value, kind in (('dynamodb:LeadingKeys', [key] if key else None, 'stringList'),
                                   ('dynamodb:EnclosingOperation', [transaction] if transaction else None, 'string'),
                                   ('dynamodb:Attributes', attributes, 'stringList'),
                                   ('dynamodb:Select', [select] if select else None, 'string')):
            if value:
                context.append({'ContextKeyName': name_, 'ContextKeyValues': value, 'ContextKeyType': kind})
        checks.append({'name': name, 'action': action, 'resource': resource, 'expected_allow': allow, 'context': context})
    add('read_principal', 'dynamodb:GetItem', auth, True, key='USER#synthetic', transaction='TransactGetItems')
    add('read_enrollment_control', 'dynamodb:GetItem', auth, True, key='CONTROL#PRINCIPAL_ENROLLMENT')
    add('read_session_denied', 'dynamodb:GetItem', auth, False, key='SESSION#synthetic')
    add('missing_read_key_denied', 'dynamodb:GetItem', auth, False, transaction='TransactGetItems')
    add('scan_projection', 'dynamodb:Scan', auth, True,
        attributes=['pk', 'withdrawal_request_id', 'epoch'], select='SPECIFIC_ATTRIBUTES')
    add('scan_all_attributes_denied', 'dynamodb:Scan', auth, False, select='ALL_ATTRIBUTES')
    add('mark_old_principal', 'dynamodb:UpdateItem', auth, True, key='USER#synthetic', transaction='TransactWriteItems')
    add('direct_principal_update_denied', 'dynamodb:UpdateItem', auth, False, key='USER#synthetic')
    add('missing_update_key_denied', 'dynamodb:UpdateItem', auth, False, transaction='TransactWriteItems')
    for key, label in (('CONTROL#PRINCIPAL_ENROLLMENT', 'control'), ('SESSION#synthetic', 'session')):
        add(label + '_update_denied', 'dynamodb:UpdateItem', auth, False, key=key, transaction='TransactWriteItems')
    for action in ('PutItem', 'DeleteItem'):
        add('auth_' + action + '_denied', 'dynamodb:' + action, auth, False,
            key='USER#synthetic', transaction='TransactWriteItems')
    add('read_registry', 'dynamodb:GetItem', registry, True, key='REGISTRY', transaction='TransactGetItems')
    add('condition_registry', 'dynamodb:ConditionCheckItem', registry, True, key='REGISTRY', transaction='TransactWriteItems')
    for action in ('PutItem', 'DeleteItem', 'UpdateItem', 'Scan'):
        add('registry_' + action + '_denied', 'dynamodb:' + action, registry, False,
            key='REGISTRY', transaction='TransactWriteItems')
    for action in ('GetItem', 'PutItem', 'DeleteItem', 'UpdateItem', 'Scan', 'ConditionCheckItem'):
        add('unrelated_' + action + '_denied', 'dynamodb:' + action, unrelated, False, key='USER#synthetic')
    other_pool = f'arn:aws:cognito-idp:{REGION}:{account}:userpool/ap-southeast-2_SyntheticOtherPool'
    if other_pool == pool:
        other_pool += 'Different'
    for action in ('AdminGetUser', 'AdminDeleteUser'):
        add(action, 'cognito-idp:' + action, pool, True)
        add('other_pool_' + action + '_denied', 'cognito-idp:' + action, other_pool, False)
    for action in ('AdminCreateUser', 'AdminSetUserPassword', 'AdminUpdateUserAttributes'):
        add(action + '_denied', 'cognito-idp:' + action, pool, False)
    arbitrary_key = f'arn:aws:kms:{REGION}:{account}:key/00000000-0000-4000-8000-000000000001'
    add('arbitrary_direct_decrypt_denied', 'kms:Decrypt', arbitrary_key, False)
    add('arbitrary_storage_decrypt_denied', 'kms:Decrypt', arbitrary_key, False)
    checks[-1]['context'].append({'ContextKeyName': 'kms:ViaService',
                                 'ContextKeyValues': [f'dynamodb.{REGION}.amazonaws.com'], 'ContextKeyType': 'string'})
    return checks


def verify(iam, policies, checks):
    results = []
    for check in checks:
        response = iam.simulate_custom_policy(PolicyInputList=policies, ActionNames=[check['action']],
            ResourceArns=[check['resource']], ContextEntries=check['context'])
        rows = response.get('EvaluationResults')
        if response.get('IsTruncated') or not isinstance(rows, list) or len(rows) != 1 or \
                rows[0].get('EvalActionName') != check['action'] or rows[0].get('EvalResourceName') != check['resource'] or \
                rows[0].get('EvalDecision') not in {'allowed', 'implicitDeny', 'explicitDeny'}:
            raise ValueError('incomplete IAM simulator evidence')
        decision = rows[0]['EvalDecision']
        results.append({'name': check['name'], 'expected_allow': check['expected_allow'],
                        'decision': decision, 'passed': (decision == 'allowed') == check['expected_allow']})
    return {'checks': results, 'passed': all(row['passed'] for row in results), 'runtime_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy-file', type=Path, required=True)
    parser.add_argument('--pool-id', required=True)
    parser.add_argument('--stage', default='dev')
    args = parser.parse_args()
    try:
        policies = policy_inputs(json.loads(args.policy_file.read_text()))
        session = boto3.Session(profile_name='foodtech', region_name=REGION)
        account = session.client('sts').get_caller_identity()['Account']
        result = verify(session.client('iam'), policies, scenarios(account, args.stage, args.pool_id))
    except Exception:
        print('Identity policy verification failed; no policy was applied.')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
