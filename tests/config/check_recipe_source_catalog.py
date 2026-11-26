"""Catalog storage controls, without provisioning or authorizing any grant."""
import json
import sys
from pathlib import Path


def main(path):
    template = json.loads(Path(path).read_text())
    resources = list(template['Resources'].values())
    assert {r['Type'] for r in resources} <= {'AWS::DynamoDB::Table', 'AWS::CDK::Metadata'}
    resource, = [r for r in resources if r['Type'] == 'AWS::DynamoDB::Table']
    p = resource['Properties']
    assert p['TableName'] == 'foodtech-source-access-dev'
    assert p['BillingMode'] == 'PAY_PER_REQUEST'
    assert p['KeySchema'] == [{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}]
    assert p['SSESpecification'] == {'SSEEnabled': True}
    assert p['TimeToLiveSpecification'] == {'AttributeName': 'expires_at', 'Enabled': True}
    assert p['PointInTimeRecoverySpecification'] == {'PointInTimeRecoveryEnabled': True, 'RecoveryPeriodInDays': 7}
    assert p['DeletionProtectionEnabled'] is True
    assert resource['DeletionPolicy'] == resource['UpdateReplacePolicy'] == 'Retain'
    assert not p.get('GlobalSecondaryIndexes') and not p.get('LocalSecondaryIndexes')
    assert set(template['Outputs']) == {'SourceAccessTableName'}


if __name__ == '__main__':
    main(sys.argv[1])
