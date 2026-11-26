"""Offline gate for a core-only, two-permission Worker policy update.

Read the deployed template with CloudFormation GetTemplate immediately before
review. This gate does not deploy, authorize execution, or prove runtime IAM.
"""
import argparse
import json
from pathlib import Path
import re


def template(path):
    value = json.loads(Path(path).read_text())
    if 'TemplateBody' in value:
        value = value['TemplateBody']
        if isinstance(value, str):
            value = json.loads(value)
    return value


def permissions(document, resources, account):
    def resolve(value):
        if isinstance(value, str):
            return value
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError('unsupported policy resource')
        if 'Fn::Join' in value:
            separator, parts = value['Fn::Join']
            return separator.join(resolve(part) for part in parts)
        if 'Ref' in value and value['Ref'] == 'AWS::Partition':
            return 'aws'
        if 'Fn::GetAtt' in value:
            logical, attribute = value['Fn::GetAtt']
            resource = resources[logical]
            if attribute != 'Arn':
                raise ValueError('unsupported resource attribute')
            props = resource['Properties']
            if resource['Type'] == 'AWS::DynamoDB::Table':
                return f'arn:aws:dynamodb:ap-southeast-2:{account}:table/' + props['TableName']
            if resource['Type'] == 'AWS::SQS::Queue':
                return f'arn:aws:sqs:ap-southeast-2:{account}:' + props['QueueName']
        raise ValueError('unsupported policy reference')
    if set(document) != {'Version', 'Statement'} or document['Version'] != '2012-10-17':
        raise ValueError('unsupported identity policy document')
    result = set()
    for statement in document['Statement']:
        if not {'Effect', 'Action', 'Resource'} <= set(statement) or \
                set(statement) - {'Effect', 'Action', 'Resource', 'Sid'} or statement['Effect'] != 'Allow':
            raise ValueError('unsupported identity policy statement')
        actions = statement['Action'] if isinstance(statement['Action'], list) else [statement['Action']]
        targets = statement['Resource'] if isinstance(statement['Resource'], list) else [statement['Resource']]
        result.update((action, resolve(target)) for action in actions for target in targets)
    return result


def review(deployed, candidate, account):
    if not re.fullmatch(r'[0-9]{12}', account):
        raise ValueError('explicit verified project required')
    old, new = deployed['Resources'], candidate['Resources']
    if set(old) != set(new) or {k: v for k, v in deployed.items() if k != 'Resources'} != \
            {k: v for k, v in candidate.items() if k != 'Resources'}:
        raise ValueError('resource inventory or template contract changed')
    changed = [key for key in old if old[key] != new[key]]
    if len(changed) != 1:
        raise ValueError('exactly one existing worker policy must change')
    logical = changed[0]
    if old[logical]['Type'] != 'AWS::IAM::Policy' or new[logical]['Type'] != 'AWS::IAM::Policy':
        raise ValueError('non-policy resource changed')
    before, after = old[logical]['Properties'], new[logical]['Properties']
    if {k: v for k, v in before.items() if k != 'PolicyDocument'} != \
            {k: v for k, v in after.items() if k != 'PolicyDocument'} or \
            {k: v for k, v in old[logical].items() if k != 'Properties'} != \
            {k: v for k, v in new[logical].items() if k != 'Properties'}:
        raise ValueError('policy attachment or resource contract changed')
    roles = before.get('Roles')
    if not isinstance(roles, list) or len(roles) != 1 or set(roles[0]) != {'Ref'}:
        raise ValueError('one existing worker role attachment required')
    role = old[roles[0]['Ref']]
    if role['Type'] != 'AWS::IAM::Role' or 'standardization' not in role['Properties'].get('Description', '').lower():
        raise ValueError('existing standardization role unavailable')
    previous = permissions(before['PolicyDocument'], old, account)
    proposed = permissions(after['PolicyDocument'], new, account)
    expected = {('dynamodb:Query', f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-data-flow-dev'),
                ('dynamodb:ConditionCheckItem', f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-auth-sessions-dev')}
    if previous - proposed or proposed - previous != expected:
        raise ValueError('unexpected worker permission delta')
    return {'schema': 1, 'reviewed': True, 'changed_policy': logical,
            'added_permissions': sorted([list(pair) for pair in expected]),
            'added_resources': [], 'removed_resources': [], 'runtime_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployed-template', required=True, type=Path)
    parser.add_argument('--candidate-template', required=True, type=Path)
    parser.add_argument('--account-id', required=True)
    args = parser.parse_args()
    print(json.dumps(review(template(args.deployed_template), template(args.candidate_template), args.account_id), sort_keys=True))


if __name__ == '__main__':
    main()
