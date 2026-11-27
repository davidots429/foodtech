"""Check the synthesized Gateway settlement permission, without AWS access."""
import json
import sys
from fnmatch import fnmatchcase


def check_authentication_conditions(resources):
    """Inspect every base Allow that could grant authentication checks."""
    def render(value):
        if isinstance(value, str):
            return value
        if value == {'Ref': 'AWS::Partition'}:
            return 'aws'
        if isinstance(value, dict) and 'Fn::Join' in value:
            separator, parts = value['Fn::Join']
            return separator.join(render(part) for part in parts)
        if isinstance(value, dict) and 'Fn::GetAtt' in value:
            name, attribute = value['Fn::GetAtt']
            target = resources[name]
            if attribute == 'Arn' and target['Type'] == 'AWS::DynamoDB::Table':
                return 'arn:aws:dynamodb:ap-southeast-2:*:table/' + target['Properties']['TableName']
        raise AssertionError('unreviewed condition-check resource expression')

    roles = [name for name, r in resources.items() if r['Type'] == 'AWS::IAM::Role' and
             name.startswith(('AiGatewayTaskRole', 'CredentialRegistrationTaskRole'))]
    assert len(roles) == 2, 'expected registration and Gateway roles'
    expected = {'ForAllValues:StringLike': {'dynamodb:LeadingKeys': ['USER#*']},
                'Null': {'dynamodb:LeadingKeys': 'false'},
                'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2', 'dynamodb:ReturnValues': 'NONE'}}
    for role in roles:
        props = resources[role]['Properties']
        assert not props.get('ManagedPolicyArns') and not props.get('Policies'), 'unreviewed role policy'
        grants = []
        for resource in resources.values():
            if resource['Type'] not in {'AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'} or \
                    {'Ref': role} not in resource['Properties'].get('Roles', []):
                continue
            assert resource['Type'] == 'AWS::IAM::Policy', 'unreviewed attached managed policy'
            for statement in resource['Properties']['PolicyDocument']['Statement']:
                if statement.get('Effect') != 'Allow':
                    continue
                assert 'NotAction' not in statement, 'unreviewed Allow NotAction'
                actions = statement.get('Action', [])
                actions = [actions] if isinstance(actions, str) else actions
                if not any(fnmatchcase('dynamodb:conditioncheckitem', action.lower()) for action in actions):
                    continue
                assert 'NotResource' not in statement, 'unreviewed Allow NotResource'
                targets = statement.get('Resource', [])
                targets = targets if isinstance(targets, list) else [targets]
                for target in targets:
                    arn = render(target)
                    if ':table/' in arn:
                        applies = fnmatchcase('foodtech-auth-sessions-dev', arn.split(':table/', 1)[1])
                    else:
                        applies = True  # wildcard/unknown scopes must not bypass review
                    if applies:
                        assert arn.endswith(':table/foodtech-auth-sessions-dev') and '*' not in arn, 'broad auth resource'
                        assert statement['Action'] == 'dynamodb:ConditionCheckItem', 'broad auth action'
                        assert statement.get('Condition') == expected, 'incomplete USER/NONE auth boundary'
                        grants.append(statement)
        assert len(grants) == 1, 'expected exactly one USER/NONE auth grant per role'


def main(path):
    with open(path, encoding='utf-8') as stream:
        resources = json.load(stream)['Resources']
    check_authentication_conditions(resources)
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
