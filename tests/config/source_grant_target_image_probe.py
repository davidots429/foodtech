"""Offline contract against the worker installed inside a runtime image."""
from unittest.mock import MagicMock, patch
from uuid import uuid4

from foodtech_business_api import source_grant_deletion_worker as worker


def main():
    table, deletion = MagicMock(), MagicMock()
    row = {'pk': 'USER#image-probe-target', 'epoch': 1, 'withdrawal_request_id': str(uuid4())}
    table.scan.return_value = {'Items': [row]}
    deletion.step.return_value = {
        'state': 'CATALOG_EMPTY_OBSERVED', 'remaining_upper_bound': 0, 'deletion_complete': False}
    with patch.object(worker.boto3, 'Session', side_effect=AssertionError('AWS is forbidden')):
        result = worker.sweep_grants(table, deletion, clock=lambda: 1000, target_subject='image-probe-target')
        assert result['empty_observed'] == 1 and result['deletion_complete'] is False
        assert deletion.step.call_args.kwargs['subject'] == 'image-probe-target'
        deletion.reset_mock()
        table.scan.return_value = {'Items': [{**row, 'pk': 'USER#image-probe-foreign'}]}
        try:
            worker.sweep_grants(table, deletion, clock=lambda: 1000, target_subject='image-probe-target')
        except ValueError:
            pass
        else:
            raise AssertionError('foreign principal was not rejected')
        deletion.step.assert_not_called()
    print('Installed source grant worker target isolation verified without AWS access.')


if __name__ == '__main__':
    main()
