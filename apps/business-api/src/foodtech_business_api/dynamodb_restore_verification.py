"""Verify DynamoDB restore provenance for a trusted recovery set.

This checks AWS restore metadata. The coordinator must establish isolation,
prevent writes and verify the other stores of the recovery set separately.
"""

from datetime import datetime


class DynamoRestoreVerificationError(ValueError):
    """The restored table does not prove its expected source and point in time."""


def aware_timestamp(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def verify_restored_table(client, *, source_arn: str, restored_arn: str, restore_at: datetime) -> None:
    """Require AWS to report the expected table, original table and restore point."""
    if not aware_timestamp(restore_at) or source_arn == restored_arn:
        raise ValueError("explicit restore point and distinct tables required")

    table = client.describe_table(TableName=restored_arn).get("Table")
    if not isinstance(table, dict) or table.get("TableArn") != restored_arn or table.get("TableStatus") != "ACTIVE":
        raise DynamoRestoreVerificationError("restored DynamoDB table is unavailable")
    restore = table.get("RestoreSummary")
    if not isinstance(restore, dict) or restore.get("RestoreInProgress") is not False:
        raise DynamoRestoreVerificationError("completed DynamoDB restore metadata is unavailable")
    actual_time = restore.get("RestoreDateTime")
    if restore.get("SourceTableArn") != source_arn or not aware_timestamp(actual_time) or actual_time != restore_at:
        raise DynamoRestoreVerificationError("DynamoDB restore origin or point does not match")
