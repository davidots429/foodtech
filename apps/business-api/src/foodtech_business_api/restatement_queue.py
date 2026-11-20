"""Send idempotent customer restatement requests to the internal FIFO worker."""
import hashlib
import json
from functools import lru_cache

import boto3


@lru_cache(maxsize=1)
def _clients():
    session = boto3.Session(region_name="ap-southeast-2")
    return session.resource("dynamodb"), session.client("sqs")


def source_revision(table_name: str, stream_pk: str) -> int | None:
    resource, _ = _clients()
    item = resource.Table(table_name).get_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"},
                                               ConsistentRead=True).get("Item")
    if not item:
        return None
    value = item.get("revision")
    if isinstance(value, bool) or value is None or value != int(value) or int(value) < 1:
        raise ValueError("invalid source revision")
    return int(value)


def send(queue_url: str, job_pk: str, request_id: str) -> None:
    _, sqs = _clients()
    group = "restatement-" + hashlib.sha256(job_pk.encode("utf-8")).hexdigest()[:40]
    dedup = hashlib.sha256(f"{job_pk}:{request_id}".encode("utf-8")).hexdigest()
    sqs.send_message(QueueUrl=queue_url, MessageGroupId=group, MessageDeduplicationId=dedup,
                     MessageBody=json.dumps({"kind": "CUSTOMER_RESTATEMENT", "job_pk": job_pk,
                                             "request_id": request_id}, separators=(",", ":")))
