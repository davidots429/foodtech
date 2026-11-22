"""Internal seven-day withdrawal transitions on the account authorization item.

An authenticated controller supplies the subject, request ID and server time.
This module never provisions a missing account, authenticates a cancellation,
deletes Cognito users, or claims that business-data erasure has completed.
"""
from decimal import Decimal
from uuid import UUID

GRACE_SECONDS = 7 * 24 * 60 * 60


class WithdrawalConflict(Exception):
    """The account or withdrawal changed, or this transition is unavailable."""


def _integer(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or \
            (isinstance(value, Decimal) and not value.is_finite()) or \
            not minimum <= value < 2**63 - 1 or value != int(value):
        raise WithdrawalConflict('invalid withdrawal state')
    return int(value)


def _identity(subject, request_id, now):
    try:
        if str(UUID(subject)) != subject or str(UUID(request_id)) != request_id or \
                type(now) is not int or not 0 < now < 2**63 - 1 - GRACE_SECONDS:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid withdrawal identity or server time') from None


def _pending(record, request_id):
    if record.get('withdrawal_request_id') != request_id or record.get('withdrawal_status') != 'PENDING' or \
            record.get('suspended') is not True or 'deletion_lock_request' in record:
        raise WithdrawalConflict('withdrawal is not pending')
    requested = _integer(record.get('withdrawal_requested_at'), 1)
    deadline = _integer(record.get('withdrawal_deadline'), 1)
    _integer(record.get('withdrawal_epoch'), 1)
    if deadline - requested != GRACE_SECONDS:
        raise WithdrawalConflict('invalid withdrawal deadline')
    return deadline


def deleting_withdrawal(record, *, request_id, now):
    """Validate an expired withdrawal before an internal worker locks its data."""
    if not isinstance(record, dict) or record.get('withdrawal_status') != 'DELETING' or \
            record.get('withdrawal_request_id') != request_id or record.get('suspended') is not True or \
            'deletion_lock_request' in record:
        raise WithdrawalConflict('withdrawal is not ready for deletion')
    requested = _integer(record.get('withdrawal_requested_at'), 1)
    deadline = _integer(record.get('withdrawal_deadline'), 1)
    epoch, withdrawal_epoch = _integer(record.get('epoch'), 1), _integer(record.get('withdrawal_epoch'), 1)
    if type(now) is not int or deadline - requested != GRACE_SECONDS or now < deadline or \
            epoch <= withdrawal_epoch:
        raise WithdrawalConflict('withdrawal grace period has not expired')
    return requested, deadline


def withdrawal_change(record, *, subject, request_id, now, action, expected_epoch=None):
    """Return an explicit change, or None for a retry of an existing transition.

    Storage must compare the observed account epoch and withdrawal fields in
    the same conditional write. Callers cannot treat this plan as authorization.
    """
    _identity(subject, request_id, now)
    if not isinstance(record, dict) or record.get('pk') != f'USER#{subject}':
        raise WithdrawalConflict('account is not provisioned')
    epoch = _integer(record.get('epoch'))
    if expected_epoch is not None and epoch != _integer(expected_epoch):
        raise WithdrawalConflict('session authority changed')
    state = record.get('withdrawal_status')
    same = record.get('withdrawal_request_id') == request_id
    if action == 'request':
        if same and state == 'PENDING':
            _pending(record, request_id)
            return None
        if state not in (None, 'CANCELED') or same or record.get('suspended', False) is not False or \
                'deletion_lock_request' in record:
            raise WithdrawalConflict('account cannot enter withdrawal')
        return {'epoch': epoch + 1, 'suspended': True, 'withdrawal_status': 'PENDING',
                'withdrawal_request_id': request_id, 'withdrawal_requested_at': now,
                'withdrawal_deadline': now + GRACE_SECONDS, 'withdrawal_epoch': epoch + 1}
    if action == 'cancel':
        if same and state == 'CANCELED':
            return None
        deadline = _pending(record, request_id)
        if now >= deadline or epoch != _integer(record['withdrawal_epoch'], 1):
            raise WithdrawalConflict('withdrawal cannot be canceled')
        return {'epoch': epoch + 1, 'suspended': False, 'withdrawal_status': 'CANCELED'}
    if action == 'expire':
        if same and state == 'DELETING' and record.get('suspended') is True:
            return None
        deadline = _pending(record, request_id)
        if now < deadline:
            raise WithdrawalConflict('withdrawal grace period has not expired')
        return {'epoch': epoch + 1, 'suspended': True, 'withdrawal_status': 'DELETING'}
    raise ValueError('invalid withdrawal action')


class DynamoWithdrawalStore:
    """Apply one transition; no writes or retries on a missing account.

    The supplied Table must be the trusted account authorization table. Time is
    sampled by the controller immediately before this call, never from a body.
    A condition failure requires a fresh authenticated request or worker retry.
    """
    def __init__(self, *, table):
        self.table = table

    def transition(self, *, subject, request_id, now, action, expected_epoch=None):
        _identity(subject, request_id, now)
        key = {'pk': f'USER#{subject}'}
        record = self.table.get_item(Key=key, ConsistentRead=True).get('Item')
        changes = withdrawal_change(record, subject=subject, request_id=request_id, now=now,
                                    action=action, expected_epoch=expected_epoch)
        if changes is None:
            return record
        names = {'#epoch': 'epoch'}
        values = {':observed_epoch': record['epoch']}
        conditions = ['attribute_exists(pk)', '#epoch = :observed_epoch',
                      'attribute_not_exists(deletion_lock_request)']
        # Compare lifecycle and suspension fields as well as the authorization
        # epoch; never remove an administrative suspension observed concurrently.
        for index, field in enumerate(('suspended', 'withdrawal_status', 'withdrawal_request_id',
                                        'withdrawal_requested_at', 'withdrawal_deadline', 'withdrawal_epoch')):
            alias = f'#old{index}'
            names[alias] = field
            if field in record:
                value = f':old{index}'
                values[value] = record[field]
                conditions.append(f'{alias} = {value}')
            else:
                conditions.append(f'attribute_not_exists({alias})')
        updates = []
        for index, (field, value) in enumerate(changes.items()):
            name, token = f'#new{index}', f':new{index}'
            names[name], values[token] = field, value
            updates.append(f'{name} = {token}')
        try:
            return self.table.update_item(Key=key, UpdateExpression='SET ' + ', '.join(updates),
                ConditionExpression=' AND '.join(conditions), ExpressionAttributeNames=names,
                ExpressionAttributeValues=values, ReturnValues='ALL_NEW')['Attributes']
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            raise WithdrawalConflict('account changed during withdrawal') from None
