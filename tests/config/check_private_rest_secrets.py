"""Verify separate empty stores and scoped KMS policy before material upload."""
import argparse
import copy
import fnmatch
import json
from collections import Counter
from pathlib import Path


def check(template):
    resources = template['Resources']
    counts = Counter(r['Type'] for r in resources.values())
    counts.pop('AWS::CDK::Metadata', None)
    assert counts == Counter({'AWS::KMS::Key': 2, 'AWS::SecretsManager::Secret': 2})
    outputs = template['Outputs']
    assert set(outputs) == {'MockSecretArn', 'MockSecretKeyArn', 'CollectorSecretArn', 'CollectorSecretKeyArn'}
    key_ids, names, accounts = set(), set(), set()
    for secret_id, secret in resources.items():
        if secret['Type'] != 'AWS::SecretsManager::Secret':
            continue
        props = secret['Properties']
        assert 'SecretString' not in props and 'GenerateSecretString' not in props
        assert 'ReplicaRegions' not in props
        name = props['Name']
        assert name in {'foodtech/dev/private-rest-mock', 'foodtech/dev/private-rest-collector'}
        assert name not in names
        names.add(name)
        kind = name.rsplit('-', 1)[1].capitalize()
        key_id, attr = props['KmsKeyId']['Fn::GetAtt']
        assert attr == 'Arn' and key_id not in key_ids
        key_ids.add(key_id)
        assert outputs[kind + 'SecretArn']['Value'] == {'Ref': secret_id}
        assert outputs[kind + 'SecretKeyArn']['Value'] == {'Fn::GetAtt': [key_id, 'Arn']}
        key = resources[key_id]
        assert key['Type'] == 'AWS::KMS::Key'
        for resource in (secret, key):
            assert resource['DeletionPolicy'] == resource['UpdateReplacePolicy'] == 'Retain'
        props = key['Properties']
        assert props['Enabled'] is True and props['EnableKeyRotation'] is True
        assert props['MultiRegion'] is False and props['PendingWindowInDays'] == 7
        assert props['KeySpec'] == 'SYMMETRIC_DEFAULT' and props['KeyUsage'] == 'ENCRYPT_DECRYPT'
        assert not props.get('BypassPolicyLockoutSafetyCheck', False)
        admin, crypto = props['KeyPolicy']['Statement']
        assert admin['Effect'] == crypto['Effect'] == 'Allow'
        assert admin['Resource'] == crypto['Resource'] == '*'
        root = admin['Principal']['AWS']
        assert root.startswith('arn:aws:iam::') and root.endswith(':root')
        account = root.split(':')[4]
        assert len(account) == 12 and account.isdigit()
        accounts.add(account)
        assert crypto['Principal'] == {'AWS': root}
        assert admin['Action'] == ['kms:Create*', 'kms:Describe*', 'kms:Enable*', 'kms:List*', 'kms:Put*',
            'kms:Update*', 'kms:Revoke*', 'kms:Disable*', 'kms:Get*', 'kms:Delete*', 'kms:TagResource',
            'kms:UntagResource', 'kms:ScheduleKeyDeletion', 'kms:CancelKeyDeletion']
        for action in ('kms:Decrypt', 'kms:Encrypt', 'kms:GenerateDataKey', 'kms:Sign', 'kms:Verify'):
            assert not any(fnmatch.fnmatchcase(action, pattern) for pattern in admin['Action'])
        assert crypto['Action'] == ['kms:Decrypt', 'kms:GenerateDataKey']
        assert crypto['Condition'] == {'StringEquals': {'kms:ViaService': 'secretsmanager.ap-southeast-2.amazonaws.com',
            'kms:CallerAccount': account}, 'StringLike': {'kms:EncryptionContext:SecretARN':
                f'arn:aws:secretsmanager:ap-southeast-2:{account}:secret:{name}-*'}}
    assert len(key_ids) == len(names) == 2 and len(accounts) == 1
    return True


def negative_checks(template):
    mutations = []
    for key, resource in template['Resources'].items():
        if resource['Type'] == 'AWS::KMS::Key':
            mutations += [(key, m) for m in ('no-rotation', 'multi-region', 'direct-crypto', 'broad-context')]
        elif resource['Type'] == 'AWS::SecretsManager::Secret':
            mutations += [(key, m) for m in ('plaintext', 'password-fallback', 'remove-retain', 'missing-key')]
    for key, mutation in mutations:
        edited = copy.deepcopy(template)
        resource = edited['Resources'][key]
        props = resource['Properties']
        if mutation == 'no-rotation': props['EnableKeyRotation'] = False
        elif mutation == 'multi-region': props['MultiRegion'] = True
        elif mutation == 'direct-crypto': props['KeyPolicy']['Statement'][0]['Action'].append('kms:Decrypt')
        elif mutation == 'broad-context': props['KeyPolicy']['Statement'][1]['Condition']['StringLike']['kms:EncryptionContext:SecretARN'] = '*'
        elif mutation == 'plaintext': props['SecretString'] = 'synthetic-never-store-in-template'
        elif mutation == 'password-fallback': props['GenerateSecretString'] = {}
        elif mutation == 'remove-retain': resource['DeletionPolicy'] = 'Delete'
        elif mutation == 'missing-key': props.pop('KmsKeyId')
        try:
            check(edited)
        except (AssertionError, KeyError, TypeError):
            continue
        raise AssertionError(f'unsafe mutation accepted: {mutation}')
    return len(mutations)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('template')
    options = parser.parse_args()
    template = json.loads(Path(options.template).read_text())
    check(template)
    count = negative_checks(template)
    print(f'Empty private REST stores and dedicated regional KMS boundaries verified; {count} unsafe mutations rejected.')
