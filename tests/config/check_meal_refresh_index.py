"""Verify the synthesized retained table and sparse meal refresh index."""
import json
import sys
from pathlib import Path


def main(path: str) -> None:
    template = json.loads(Path(path).read_text())
    tables = [resource for resource in template['Resources'].values()
              if resource['Type'] == 'AWS::DynamoDB::Table']
    if len(tables) != 1:
        raise AssertionError('Expected one cooking jobs table')
    table = tables[0]
    if table.get('DeletionPolicy') != 'Retain' or table.get('UpdateReplacePolicy') != 'Retain':
        raise AssertionError('Cooking jobs table must be retained')
    properties = table['Properties']
    if {key['KeyType']: key['AttributeName'] for key in properties['KeySchema']} != {'HASH': 'pk', 'RANGE': 'sk'}:
        raise AssertionError('Cooking jobs table key schema changed')
    indexes = [index for index in properties.get('GlobalSecondaryIndexes', [])
               if index['IndexName'] == 'MealAggregatePending']
    if len(indexes) != 1:
        raise AssertionError('Expected one pending aggregate index')
    index = indexes[0]
    if {key['KeyType']: key['AttributeName'] for key in index['KeySchema']} != \
            {'HASH': 'aggregate_refresh_shard', 'RANGE': 'pk'}:
        raise AssertionError('Pending aggregate index key schema changed')
    if index['Projection'] != {'ProjectionType': 'KEYS_ONLY'}:
        raise AssertionError('Pending aggregate index must not project non-key attributes')


if __name__ == '__main__':
    main(sys.argv[1])
