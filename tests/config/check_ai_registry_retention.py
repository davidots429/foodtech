"""Require the approved seven-day AI ledger backup window without schema drift."""
import json
import sys
from pathlib import Path


def main(path):
    template = json.loads(Path(path).read_text())
    table, = [resource for resource in template['Resources'].values()
              if resource['Type'] == 'AWS::DynamoDB::Table']
    props = table['Properties']
    assert props['TableName'] == 'foodtech-ai-execution-registry-dev'
    assert props['PointInTimeRecoverySpecification'] == {
        'PointInTimeRecoveryEnabled': True, 'RecoveryPeriodInDays': 7}
    assert props['KeySchema'] == [{'AttributeName': 'pk', 'KeyType': 'HASH'},
                                  {'AttributeName': 'sk', 'KeyType': 'RANGE'}]
    assert props['TimeToLiveSpecification'] == {'AttributeName': 'expires', 'Enabled': True}
    assert props['BillingMode'] == 'PAY_PER_REQUEST'
    assert table['DeletionPolicy'] == table['UpdateReplacePolicy'] == 'Retain'
    assert not any(resource['Type'].startswith(('AWS::IAM::', 'AWS::ECS::', 'AWS::Cognito::'))
                   for resource in template['Resources'].values())


if __name__ == '__main__':
    main(sys.argv[1])
