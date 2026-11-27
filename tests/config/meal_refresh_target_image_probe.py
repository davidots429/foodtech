"""Prove target-aware dispatch exists in the installed image without AWS."""
import io
import json
import os
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from foodtech_business_api import meal_refresh_runtime as runtime
from foodtech_business_api.combi_refresh_worker import parse_refresh_targets, refresh_targets


def main():
    raw = '[{"subject":"synthetic-probe","meal_id":"00000000-0000-4000-8000-000000000001"}]'
    targets = parse_refresh_targets(raw)
    env = {'FOODTECH_MEAL_REFRESH_MODE': 'targets', 'FOODTECH_MEAL_REFRESH_TARGETS': raw,
           'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true'}
    counts = {'processed': 1, 'partial': 0, 'failed': 0, 'pages': 1}
    with patch.object(runtime.boto3, 'Session', side_effect=AssertionError('AWS is forbidden')), \
            patch.dict(os.environ, env, clear=True), \
            patch.object(runtime, 'configured_worker', return_value=('store', 'auth')), \
            patch.object(runtime, 'refresh_targets', return_value=counts) as selected, \
            patch.object(runtime, 'sweep_pending', side_effect=AssertionError('global sweep is forbidden')), \
            redirect_stdout(io.StringIO()) as output:
        assert runtime.main() == 0
        selected.assert_called_once_with('store', 'auth', targets)
        assert json.loads(output.getvalue()) == counts
    store, auth = MagicMock(), MagicMock()
    store.table.query.return_value = {'Items': [{'pk': 'foreign', 'sk': 'MEAL_AGGREGATE#DIRTY'}]}
    assert refresh_targets(store, auth, targets) == {'processed': 0, 'partial': 0, 'failed': 1, 'pages': 1}
    auth.get_item.assert_not_called()
    store.read_meal_aggregate_dirty.assert_not_called()
    print('Installed target-aware meal refresh dispatch and foreign selection rejection verified offline.')


if __name__ == '__main__':
    main()
