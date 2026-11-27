"""One-profile read-only REST poller."""

import argparse
import os
import sys
import time

import jsonschema
from lxml import etree

from foodtech_contracts import decode_record, stream_key

from .aws_flow import DynamoOrderedIngestor
from .jeyuk_aws import DynamoJeyukIngestor
from .rest import RestMockClient
from .ingestion_quarantine import IngestionQuarantine, failure
from foodtech_contracts.collection_evidence import CompletionTracker
from foodtech_contracts.jeyuk_receipt import JeyukCompletionTracker

def main(argv=None, *, session=None, expected_trial_payload=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--polls", type=int, default=1)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--until-complete", action="store_true", help="poll Jeyuk until its explicit completion marker")
    parser.add_argument("--max-runtime", type=int, default=1800, help="maximum seconds for --until-complete")
    parser.add_argument("--cursor-key", default="default", help="separate cursor for each Mock fixture")
    parser.add_argument("--profile", choices=("mockprofile-100", "mockprofile-101", "mockprofile-110", "mockprofile-111", "mockprofile-120", "mockprofile-121"),
                        default="mockprofile-100")
    parser.add_argument("--instance", choices=tuple(f"mockprofile-{profile}-{vendor}-001" for profile in ("100", "101", "111", "121") for vendor in "abc") + tuple(f"mockprofile-{profile}-{vendor}-001" for profile in ("110", "120") for vendor in "bc"),
                        default="mockprofile-100-a-001")
    args = parser.parse_args(argv)
    if args.polls < 1 or args.interval < 0 or args.max_runtime < 1:
        parser.error("polls must be positive and interval must be nonnegative")
    jeyuk_simulation = args.profile == "mockprofile-110" and args.instance == "mockprofile-100-a-001"
    if args.until_complete and not jeyuk_simulation:
        parser.error("--until-complete requires the Jeyuk profile")
    if not args.cursor_key.replace("-", "").isalnum() or len(args.cursor_key) > 80:
        parser.error("cursor-key must contain only letters, digits or hyphens (max 80)")
    required = (
        "FOODTECH_MOCK_BASE_URL", "FOODTECH_MOCK_CLIENT_ID", "FOODTECH_MOCK_CLIENT_SECRET",
        "FOODTECH_MOCK_CA_FILE", "FOODTECH_DATA_TABLE", "FOODTECH_ORDERED_QUEUE_URL",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        parser.error("missing configuration: " + ", ".join(missing))
    if not jeyuk_simulation and not args.instance.startswith(args.profile + "-"):
        parser.error("instance does not match the requested profile")
    if expected_trial_payload is not None and (args.profile != "mockprofile-100" or
            args.instance != "mockprofile-100-a-001" or args.polls != 1 or args.until_complete):
        raise ValueError("private REST trial requires one basic-profile poll")
    client_kwargs = {"profile_id": args.profile} if args.profile != "mockprofile-100" else {}
    if not jeyuk_simulation and args.profile in {"mockprofile-110", "mockprofile-120"}:
        client_kwargs["preserve_invalid_utf8"] = True
    client = RestMockClient(
        os.environ["FOODTECH_MOCK_BASE_URL"], os.environ["FOODTECH_MOCK_CLIENT_ID"],
        os.environ["FOODTECH_MOCK_CLIENT_SECRET"], os.environ["FOODTECH_MOCK_CA_FILE"],
        **client_kwargs,
    )
    if jeyuk_simulation:
        if session is not None:
            raise ValueError("session-injected REST trial requires the basic profile")
        ingestor = DynamoJeyukIngestor(os.environ["FOODTECH_DATA_TABLE"],
                                       os.environ["FOODTECH_ORDERED_QUEUE_URL"])
        try:
            cursor = ingestor.cursor(args.cursor_key)
            completion = JeyukCompletionTracker(cursor)
            deadline = time.monotonic() + args.max_runtime
            index = 0
            while args.until_complete or index < args.polls:
                if args.until_complete and time.monotonic() >= deadline:
                    raise TimeoutError("Jeyuk collection deadline reached before explicit completion")
                result = client.poll(cursor)
                for raw in result.records:
                    ingestor.ingest(raw)
                    completion.record(raw)
                receipt = completion.finish(cursor=result.next_cursor, complete=result.complete,
                                            explicit=result.completion_explicit)
                if receipt is not None:
                    ingestor.save_completion_receipt(receipt, args.cursor_key)
                ingestor.save_cursor(result.next_cursor, args.cursor_key)
                cursor = result.next_cursor
                print(f"poll={index + 1} received={len(result.records)} cursor={cursor}")
                index += 1
                if args.until_complete and result.complete and result.completion_explicit:
                    break
                if args.until_complete or index < args.polls:
                    delay = min(args.interval, max(0, deadline - time.monotonic())) if args.until_complete else args.interval
                    time.sleep(delay)
            return 0
        finally:
            client.close()
    sdk = {} if session is None else {"session": session}
    ingestor = DynamoOrderedIngestor(os.environ["FOODTECH_DATA_TABLE"], os.environ["FOODTECH_ORDERED_QUEUE_URL"], **sdk)
    quarantine = None
    cursor_key = (args.cursor_key if args.instance == "mockprofile-100-a-001" else
                  (f"xml-{args.instance.split('-')[2]}#" if args.profile == "mockprofile-101" else
                   f"{args.instance}#" if args.profile in {"mockprofile-110", "mockprofile-111", "mockprofile-120", "mockprofile-121"} else
                   "vendor-b#" if args.instance == "mockprofile-100-b-001" else "vendor-c#") + args.cursor_key)
    try:
        cursor, streams = ingestor.cursor_state(cursor_key)
        completion = CompletionTracker(cursor, args.profile, args.instance)
        for index in range(args.polls):
            result = client.poll(cursor)
            if expected_trial_payload is not None and (cursor != 0 or result.records != (expected_trial_payload,) or
                    result.next_cursor != 1 or not result.complete or not result.completion_explicit):
                raise ValueError("private REST trial response differs from the reviewed single-record fault")
            released = []
            for offset, raw in enumerate(result.records):
                record = None
                if args.profile in {"mockprofile-101", "mockprofile-111", "mockprofile-121"}:
                    try:
                        if isinstance(raw, bytes):
                            raw.decode("utf-8", errors="strict")
                        record = decode_record(raw)
                        invalid = None
                    except UnicodeDecodeError:
                        invalid = ("UNSUPPORTED_ENCODING", [])
                    except (ValueError, etree.Error, jsonschema.ValidationError):
                        invalid = ("SCHEMA_VIOLATION", [])
                else:
                    invalid = failure(raw)
                    if invalid is None:
                        try:
                            record = decode_record(raw)
                        except (ValueError, etree.Error, jsonschema.ValidationError):
                            invalid = ("SCHEMA_VIOLATION", [])
                if invalid is not None:
                    completion.reject()
                    bucket = os.getenv("FOODTECH_INGESTION_DLQ_BUCKET")
                    if not bucket:
                        raise ValueError("ingestion quarantine bucket is required for malformed input")
                    if quarantine is None:
                        quarantine = IngestionQuarantine(bucket, os.environ["FOODTECH_DATA_TABLE"], **sdk)
                    quarantine.preserve(raw, cursor_key=cursor_key, position=cursor + offset,
                                        reason=invalid[0], missing=invalid[1], instance_id=args.instance, profile_id=args.profile)
                    continue
                record = record if record is not None else decode_record(raw)
                if record["deviceId"] != args.instance:
                    raise ValueError("unexpected REST Mock device")
                completion.record(raw, record)
                streams.add(stream_key(record))
                released.extend(ingestor.ingest(raw))
            for stream in sorted(streams):
                released.extend(ingestor.drain(stream))
            ingestor.save_cursor(result.next_cursor, streams, cursor_key)
            cursor = result.next_cursor
            receipt = completion.finish(next_cursor=cursor, complete=result.complete,
                                        completion_explicit=result.completion_explicit)
            if receipt is not None:
                ingestor.save_completion_receipt(receipt, cursor_key)
            print(f"poll={index + 1} received={len(result.records)} released={len(released)} cursor={cursor}")
            if index + 1 < args.polls:
                time.sleep(args.interval)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
