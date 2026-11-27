"""Reject additive Allows that bypass the base authentication boundary."""
import copy
import json
import sys
from check_gateway_policy import check_authentication_conditions


def main(path):
    resources = json.load(open(path, encoding='utf-8'))['Resources']
    check_authentication_conditions(resources)
    checked = 0
    for prefix in ('AiGatewayTaskRole', 'CredentialRegistrationTaskRole'):
        role, = [name for name, value in resources.items()
                 if value['Type'] == 'AWS::IAM::Role' and name.startswith(prefix)]
        policy, = [name for name, value in resources.items() if value['Type'] == 'AWS::IAM::Policy'
                   and {'Ref': role} in value['Properties'].get('Roles', [])]
        for variant in ('missing_null', 'all_keys', 'session_keys', 'control_keys', 'old_item',
                        'if_exists', 'wildcard_action', 'wildcard_resource', 'not_action',
                        'not_resource', 'extra_allow', 'managed_policy'):
            mutated = copy.deepcopy(resources)
            statements = mutated[policy]['Properties']['PolicyDocument']['Statement']
            auth, = [s for s in statements if 'foodtech-auth-sessions-dev' in json.dumps(s.get('Resource'))]
            condition = auth['Condition']
            if variant == 'missing_null':
                del condition['Null']
            elif variant in {'all_keys', 'session_keys', 'control_keys'}:
                condition['ForAllValues:StringLike']['dynamodb:LeadingKeys'] = [
                    {'all_keys': '*', 'session_keys': 'SESSION#*', 'control_keys': 'CONTROL#*'}[variant]]
            elif variant == 'old_item':
                condition['StringEquals']['dynamodb:ReturnValues'] = 'ALL_OLD'
            elif variant == 'if_exists':
                condition['StringEqualsIfExists'] = condition.pop('StringEquals')
            elif variant == 'wildcard_action':
                auth['Action'] = 'dynamodb:*'
            elif variant == 'wildcard_resource':
                auth['Resource'] = '*'
            elif variant == 'not_action':
                auth['NotAction'] = 'kms:*'
                del auth['Action']
            elif variant == 'not_resource':
                auth['NotResource'] = 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/unrelated'
                del auth['Resource']
            elif variant == 'extra_allow':
                broad = copy.deepcopy(auth)
                del broad['Condition']
                statements.append(broad)
            else:
                mutated[role]['Properties']['ManagedPolicyArns'] = ['unreviewed']
            try:
                check_authentication_conditions(mutated)
            except AssertionError:
                checked += 1
            else:
                raise AssertionError(f'unexpected acceptance: {prefix}/{variant}')
    print(f'AI auth boundary: baseline accepted; {checked} unsafe mutations rejected')


if __name__ == '__main__':
    main(sys.argv[1])
