"""Check the synthesized Gateway settlement permission, without AWS access."""
import json
import sys
from fnmatch import fnmatchcase


def main(path):
    with open(path, encoding='utf-8') as stream:
        resources = json.load(stream)['Resources']
    roles = [name for name, resource in resources.items()
             if resource['Type'] == 'AWS::IAM::Role' and name.startswith('AiGatewayTaskRole')]
    if len(roles) != 1:
        raise AssertionError('expected one Gateway task role')
    role = resources[roles[0]]['Properties']
    if role.get('ManagedPolicyArns') or role.get('Policies'):
        raise AssertionError('unreviewed policies attached directly to Gateway role')
    for resource in resources.values():
        if resource['Type'] == 'AWS::IAM::ManagedPolicy' and \
                {'Ref': roles[0]} in resource['Properties'].get('Roles', []):
            raise AssertionError('unreviewed managed policy attached to Gateway role')
    policies = [resource['Properties']['PolicyDocument'] for resource in resources.values()
                if resource['Type'] == 'AWS::IAM::Policy' and
                {'Ref': roles[0]} in resource['Properties'].get('Roles', [])]
    deletes = []
    for policy in policies:
        for statement in policy['Statement']:
            if statement.get('Effect') == 'Allow' and 'NotAction' in statement:
                raise AssertionError('Gateway allow policy must not use NotAction')
            actions = statement.get('Action', [])
            actions = [actions] if isinstance(actions, str) else actions
            if statement.get('Effect') == 'Allow' and any(
                    fnmatchcase('dynamodb:deleteitem', action.lower()) for action in actions):
                deletes.append(statement)
    if len(deletes) != 1:
        raise AssertionError('expected exactly one bounded Gateway deletion grant')
    statement = deletes[0]
    if statement['Action'] != 'dynamodb:DeleteItem':
        raise AssertionError('Gateway deletion action is broader than expected')
    resource_text = json.dumps(statement['Resource'])
    if 'table/foodtech-ai-execution-registry-dev' not in resource_text or '*' in resource_text:
        raise AssertionError('Gateway deletion resource is not the execution registry')
    condition = statement.get('Condition', {})
    if condition.get('ForAllValues:StringLike', {}).get('dynamodb:LeadingKeys') != ['ACTIVE#*'] or \
            condition.get('Null', {}).get('dynamodb:LeadingKeys') != 'false' or \
            condition.get('StringEquals', {}).get('dynamodb:EnclosingOperation') != 'TransactWriteItems':
        raise AssertionError('Gateway deletion conditions are incomplete')
    print('Gateway active-marker settlement policy verified')


if __name__ == '__main__':
    main(sys.argv[1])
