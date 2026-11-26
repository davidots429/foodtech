import json
import sys
from pathlib import Path


def main(path):
    template = json.loads(Path(path).read_text())
    resources = template['Resources'].values()
    assert all(r['Type'] in {'AWS::DynamoDB::Table', 'AWS::SQS::Queue', 'AWS::SQS::QueuePolicy',
        'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::CDK::Metadata'} for r in resources)
    role, = [r for r in resources if r['Type'] == 'AWS::IAM::Role']
    assert 'standardization' in role['Properties']['Description']
    assert 'IngestionDlqBucketName' not in template['Outputs']
    assert 'CollectionAdapterTaskRoleArn' not in template['Outputs']
    assert 'StandardizationWorkerTaskRoleArn' in template['Outputs']
    assert len([r for r in resources if r['Type'] == 'AWS::DynamoDB::Table']) == 1
    assert len([r for r in resources if r['Type'] == 'AWS::SQS::Queue']) == 3


if __name__ == '__main__':
    main(sys.argv[1])
