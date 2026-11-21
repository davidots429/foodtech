"""Verify the collection role in a synthesized development template."""
import json
import sys


def main(path):
    with open(path, encoding='utf-8') as stream:
        resources = json.load(stream)['Resources']
    policies = [value['Properties']['PolicyDocument']['Statement'] for key, value in resources.items()
                if key.startswith('CollectionAdapterTaskRole') and value['Type'] == 'AWS::IAM::Policy']
    if len(policies) != 1:
        raise AssertionError('expected one collection role policy')
    statements = policies[0]
    expected = {'dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
                'dynamodb:DeleteItem', 'sqs:SendMessage', 's3:PutObject'}
    actions = set()
    for statement in statements:
        if statement.get('Effect') != 'Allow' or 'NotAction' in statement:
            raise AssertionError('unexpected collection role statement')
        raw = statement['Action']
        actions.update([raw] if isinstance(raw, str) else raw)
    if actions != expected:
        raise AssertionError('unexpected collection role actions')
    def arn(logical_prefix, resource_type):
        matches = [key for key, value in resources.items()
                   if key.startswith(logical_prefix) and value['Type'] == resource_type]
        if len(matches) != 1:
            raise AssertionError('expected one resource for ' + logical_prefix)
        return {'Fn::GetAtt': [matches[0], 'Arn']}
    table = arn('DataFlow', 'AWS::DynamoDB::Table')
    queue = arn('Ordered', 'AWS::SQS::Queue')
    bucket = arn('IngestionDlq', 'AWS::S3::Bucket')
    for item in statements:
        values = item['Action'] if isinstance(item['Action'], list) else [item['Action']]
        if values[0].startswith('dynamodb:') and item['Resource'] not in (table, [table]):
            raise AssertionError('collection table scope changed')
        if values[0].startswith('sqs:') and item['Resource'] != queue:
            raise AssertionError('collection queue scope changed')
    deletion = next(item for item in statements if item['Action'] == 'dynamodb:DeleteItem')
    if deletion.get('Condition') != {'ForAllValues:StringLike': {'dynamodb:LeadingKeys': ['STREAM#*']},
                                    'Null': {'dynamodb:LeadingKeys': 'false'}}:
        raise AssertionError('collection deletion scope changed')
    put = next(item for item in statements if item['Action'] == 's3:PutObject')
    if put['Resource'] != {'Fn::Join': ['', [bucket, '/ingestion/*']]}:
        raise AssertionError('quarantine object scope changed')
    print('Collection role action and quarantine scope verified')


if __name__ == '__main__':
    main(sys.argv[1])
