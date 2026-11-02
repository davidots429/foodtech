"""One-profile read-only REST poller."""

import argparse
import os
import sys
import time

from foodtech_contracts import decode_record, stream_key

from .aws_flow import DynamoOrderedIngestor
from .rest import RestMockClient

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--polls", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--cursor-key", default="default", help="separate cursor for each Mock fixture")
    args = parser.parse_args()
    if args.polls < 1 or args.interval < 0:
        parser.error("polls must be positive and interval must be nonnegative")
    if not args.cursor_key.replace("-", "").isalnum() or len(args.cursor_key) > 80:
        parser.error("cursor-key must contain only letters, digits or hyphens (max 80)")
    required = (
        "FOODTECH_MOCK_BASE_URL", "FOODTECH_MOCK_CLIENT_ID", "FOODTECH_MOCK_CLIENT_SECRET",
        "FOODTECH_MOCK_CA_FILE", "FOODTECH_DATA_TABLE", "FOODTECH_ORDERED_QUEUE_URL",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        parser.error("missing configuration: " + ", ".join(missing))
    client = RestMockClient(
        os.environ["FOODTECH_MOCK_BASE_URL"], os.environ["FOODTECH_MOCK_CLIENT_ID"],
        os.environ["FOODTECH_MOCK_CLIENT_SECRET"], os.environ["FOODTECH_MOCK_CA_FILE"],
    )
    ingestor = DynamoOrderedIngestor(os.environ["FOODTECH_DATA_TABLE"], os.environ["FOODTECH_ORDERED_QUEUE_URL"])
    try:
        cursor, streams = ingestor.cursor_state(args.cursor_key)
        for index in range(args.polls):
            result = client.poll(cursor)
            released = []
            for raw in result.records:
                streams.add(stream_key(decode_record(raw)))
                released.extend(ingestor.ingest(raw))
            for stream in sorted(streams):
                released.extend(ingestor.drain(stream))
            ingestor.save_cursor(result.next_cursor, streams, args.cursor_key)
            cursor = result.next_cursor
            print(f"poll={index + 1} received={len(result.records)} released={len(released)} cursor={cursor}")
            if index + 1 < args.polls:
                time.sleep(args.interval)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
