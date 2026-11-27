"""Meaningful safety mutations against the synthesized template."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from check_combi_execution_access import check


def main(path,account):
    original=json.loads(Path(path).read_text())
    check(original,account)
    def policy(t): return next(v for v in t['Resources'].values() if v['Type']=='AWS::IAM::Policy')['Properties']
    changes=[lambda p:p.update(Roles=['unreviewed-role']),
        lambda p:p['PolicyDocument']['Statement'][0].update(Action='dynamodb:*'),
        lambda p:p['PolicyDocument']['Statement'][0].update(Resource='*'),
        lambda p:p['PolicyDocument']['Statement'][0]['Condition']['Null'].pop('dynamodb:Attributes'),
        lambda p:p['PolicyDocument']['Statement'][0]['Condition']['Null'].pop('dynamodb:LeadingKeys'),
        lambda p:p['PolicyDocument']['Statement'][0]['Condition']['Null'].pop('dynamodb:EnclosingOperation'),
        lambda p:p['PolicyDocument']['Statement'][0]['Condition']['StringEquals'].pop('dynamodb:Select'),
        lambda p:p['PolicyDocument']['Statement'][0]['Condition']['ForAllValues:StringEquals']['dynamodb:Attributes'].append('credential_id'),
        lambda p:p['PolicyDocument']['Statement'][0]['Condition']['ForAllValues:StringLike'].update({'dynamodb:LeadingKeys':['*']})]
    for number,change in enumerate(changes):
        value=deepcopy(original);change(policy(value))
        try: check(value,account)
        except AssertionError: pass
        else: raise AssertionError('widening mutation accepted: '+str(number))
    print('rejected 9 privilege-widening mutations')


if __name__=='__main__': main(sys.argv[1],sys.argv[2])
