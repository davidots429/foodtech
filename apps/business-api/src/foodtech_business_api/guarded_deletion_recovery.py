"""Internal owner-bound recovery of a locked deletion request.

A trusted caller must authenticate the subject and hold a separately authorized
JIT recovery role. This component does not expose an HTTP endpoint or resume the
full deletion saga, and it never returns the decrypted identifiers.
"""
from uuid import UUID

from boto3.dynamodb.types import TypeSerializer


class GuardedDeletionRecovery:
    def __init__(self, *, store, recovery_cipher, client):
        self.store, self.recovery_cipher, self.client = store, recovery_cipher, client
        self.serializer = TypeSerializer()

    def _item(self, values):
        return {key: self.serializer.serialize(value) for key, value in values.items()}

    def recover(self, *, authenticated_subject: str, request_id: str) -> dict:
        try:
            if str(UUID(authenticated_subject)) != authenticated_subject or str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid guarded recovery identity') from None
        def get(table, key):
            return {'Get': {'TableName': table, 'Key': self._item(key)}}
        result = self.client.transact_get_items(TransactItems=[
            get(self.store.auth_table, {'pk': f'USER#{authenticated_subject}'}),
            get(self.store.registry_table, {'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
        ])
        try:
            rows = result['Responses']
            if not isinstance(rows, list) or len(rows) != 2:
                raise ValueError
            auth, registry = (row.get('Item') for row in rows)
            if not isinstance(auth, dict) or not isinstance(registry, dict) or \
                    auth.get('pk') != {'S': f'USER#{authenticated_subject}'} or \
                    auth.get('suspended') != {'BOOL': True} or auth.get('deletion_lock_request') != {'S': request_id} or \
                    registry.get('pk') != {'S': 'REGISTRY'} or registry.get('sk') != {'S': f'REQ#{request_id}'} or \
                    registry.get('request_id') != {'S': request_id} or registry.get('status') != {'S': 'DELETING'}:
                raise ValueError
            vault_id = auth['vault_id']['S']
            if str(UUID(vault_id)) != vault_id:
                raise ValueError
            user = registry['encrypted_user_id']['B']
            locator = registry['encrypted_capsule_locator']['B']
            if any(not isinstance(x, bytes) or not 1 <= len(x) <= 65536 for x in (user, locator)):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('guarded deletion recovery unavailable') from None
        def confirm(internal_id, capsule_locator):
            return self.store.recover_locked_request(subject=authenticated_subject, request_id=request_id,
                vault_id=vault_id, internal_user_id=internal_id.decode('ascii'),
                capsule_locator=capsule_locator.decode('ascii'))
        return self.recovery_cipher.use(request_id=request_id, encrypted_user_id=user,
                                        encrypted_capsule_locator=locator, callback=confirm)
