"""Offline target-aware registration and invalid-input checks; no AWS calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from check_meal_refresh_runtime import check


def main():
    root = Path(__file__).resolve().parents[2]
    cwd = root / 'infra/cdk'
    targets = json.dumps([{'subject': 'synthetic-customer',
        'meal_id': '00000000-0000-4000-8000-000000000001'}], separators=(',', ':'))
    image = '123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/maintenance-dev@sha256:' + 'a' * 64
    env = {**os.environ, 'CDK_DEFAULT_ACCOUNT': '123456789012'}
    with tempfile.TemporaryDirectory(prefix='foodtech-meal-targets-') as output:
        args = ['npx', 'cdk', '--app', 'node --import tsx bin/app.ts', 'synth',
            'FoodtechMealRefreshRuntime-dev', '--strict', '--no-lookups', '--output', str(Path(output) / 'rejected'),
            '-c', 'provisionMealRefreshRuntime=true', '-c', 'principalProvisioningEnabled=true',
            '-c', 'mealRefreshLogGroupMode=existing', '-c', 'mealRefreshTargets=' + targets]
        rejected = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
        assert rejected.returncode != 0 and 'exact reviewed target-aware mealRefreshImageUri' in rejected.stderr, rejected.stderr
        subprocess.run(['npx', 'cdk', '--app', 'node --import tsx bin/app.ts', 'synth',
            'FoodtechMealRefreshRuntime-dev', '--strict', '--no-lookups', '--output', output,
            '-c', 'provisionMealRefreshRuntime=true', '-c', 'principalProvisioningEnabled=true',
            '-c', 'mealRefreshLogGroupMode=existing', '-c', 'mealRefreshTargets=' + targets,
            '-c', 'mealRefreshImageUri=' + image], cwd=cwd, env=env, check=True, stdout=subprocess.DEVNULL)
        check(Path(output) / 'FoodtechMealRefreshRuntime-dev.template.json', 'existing', targets, image)
    script = '''
import assert from 'node:assert/strict';
import {reviewedMealRefreshTargets as parse} from './bin/meal-refresh-runtime.ts';
const valid = process.argv[1];
assert.equal(parse(valid),valid);
for (const value of [undefined,'','[]','{}',valid+' ',valid.replace('synthetic-customer','bad#subject'),
    JSON.stringify([{subject:'synthetic-customer\\n',meal_id:'00000000-0000-4000-8000-000000000001'}]),
    valid.replace('000000000001','00000000000Z'),valid.slice(0,-1)+','+valid.slice(1),
    '[{"subject":"first","subject":"second","meal_id":"00000000-0000-4000-8000-000000000001"}]',
    JSON.stringify(Array(17).fill(JSON.parse(valid)[0]))]) assert.throws(()=>parse(value));
'''
    subprocess.run(['node', '--import', 'tsx', '--input-type=module', '-e', script, targets], cwd=cwd, check=True)
    print('Exact targets/image, strict mode, registration-only resources and invalid target rejection verified.')


if __name__ == '__main__':
    main()
