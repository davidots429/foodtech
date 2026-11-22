"""Read-only IAM simulation of the deployed Gateway marker permission.

Simulation does not run an ECS task or prove that AWS permits the transaction.
"""
import json
import os

import boto3


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    outputs = session.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']
    role = next((item['OutputValue'] for item in outputs if item['OutputKey'] == 'AiGatewayTaskRoleArn'), None)
    if not role:
        raise SystemExit('Gateway role output is unavailable')
    arn = 'arn:aws:dynamodb:ap-southeast-2:573693340100:table/foodtech-ai-execution-registry-dev'
    iam = session.client('iam')
    cases = [('marker_transaction', ['ACTIVE#synthetic'], 'TransactWriteItems', True),
             ('other_partition', ['EXEC#synthetic'], 'TransactWriteItems', False),
             ('missing_partition', [], 'TransactWriteItems', False),
             ('direct_delete', ['ACTIVE#synthetic'], None, False)]
    results = {}
    for name, keys, operation, expected in cases:
        context = [{'ContextKeyName': 'aws:RequestedRegion',
                    'ContextKeyValues': ['ap-southeast-2'], 'ContextKeyType': 'string'}]
        if keys:
            context.append({'ContextKeyName': 'dynamodb:LeadingKeys', 'ContextKeyValues': keys,
                            'ContextKeyType': 'stringList'})
        if operation:
            context.append({'ContextKeyName': 'dynamodb:EnclosingOperation', 'ContextKeyValues': [operation],
                            'ContextKeyType': 'stringList'})
        result = iam.simulate_principal_policy(PolicySourceArn=role, ActionNames=['dynamodb:DeleteItem'],
            ResourceArns=[arn], ContextEntries=context)['EvaluationResults'][0]
        decision = result['EvalDecision']
        organizations = result.get('OrganizationsDecisionDetail', {}).get('AllowedByOrganizations')
        results[name] = {'decision': decision, 'organizations_allowed': organizations,
                         'matches_expected': (decision == 'allowed') == expected}
    print(json.dumps(results, sort_keys=True))
    if not all(result['matches_expected'] for result in results.values()):
        raise SystemExit('deployed Gateway role does not satisfy the marker policy; inspect deployment and organization policy')


if __name__ == '__main__':
    main()
