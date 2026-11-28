"""Check the version inventory of a scoped, isolated S3 restore destination.

The caller supplies the trusted destination manifest tied to the recovery
point and keeps its writers stopped. Listing verifies the destination state,
not the source history, object content, or the completeness of that manifest.
"""

import re
from dataclasses import dataclass, field


class S3RestoreVerificationError(ValueError):
    """The destination does not match its expected version inventory."""


@dataclass(frozen=True)
class RestoredS3Version:
    key: str = field(repr=False)
    version_id: str = field(repr=False)
    delete_marker: bool
    latest: bool


@dataclass(frozen=True)
class S3RestoreInventory:
    object_versions: int
    delete_markers: int


def verify_s3_restored_versions(client, *, source_bucket: str, restored_bucket: str,
                                expected_owner: str, prefix: str,
                                expected: tuple[RestoredS3Version, ...]) -> S3RestoreInventory:
    """Require an exact multiversion match, including which version is current."""
    if any(not isinstance(name, str) or not name for name in (source_bucket, restored_bucket)) or \
            source_bucket == restored_bucket or not isinstance(expected_owner, str) or \
            not re.fullmatch(r"[0-9]{12}", expected_owner) or not isinstance(prefix, str) or prefix == "/" or \
            not prefix.endswith("/") or not isinstance(expected, tuple):
        raise ValueError("explicit isolated bucket, owner, prefix and restore manifest required")

    targets = {}
    keys, latest_keys = set(), set()
    for entry in expected:
        if (not isinstance(entry, RestoredS3Version) or not isinstance(entry.key, str)
                or not entry.key.startswith(prefix) or not isinstance(entry.version_id, str)
                or not entry.version_id or entry.version_id == "null"
                or type(entry.delete_marker) is not bool or type(entry.latest) is not bool):
            raise ValueError("invalid restored S3 manifest entry")
        identity = (entry.key, entry.version_id)
        if identity in targets or (entry.latest and entry.key in latest_keys):
            raise ValueError("ambiguous restored S3 manifest")
        targets[identity] = entry
        keys.add(entry.key)
        if entry.latest:
            latest_keys.add(entry.key)
    if keys != latest_keys:
        raise ValueError("restore manifest must identify the current version of every key")

    bucket = {"Bucket": restored_bucket, "ExpectedBucketOwner": expected_owner}
    if client.get_bucket_versioning(**bucket).get("Status") != "Enabled":
        raise S3RestoreVerificationError("restored S3 bucket versioning is unavailable")

    complete = False
    paginator = client.get_paginator("list_object_versions")
    for page in paginator.paginate(**bucket, Prefix=prefix):
        truncated = page.get("IsTruncated")
        if (complete or type(truncated) is not bool or page.get("CommonPrefixes")
                or page.get("Name", restored_bucket) != restored_bucket or page.get("Prefix", prefix) != prefix
                or (truncated and not (page.get("NextKeyMarker") and page.get("NextVersionIdMarker")))):
            raise S3RestoreVerificationError("incomplete restored S3 version listing")
        complete = not truncated
        for label, delete_marker in (("Versions", False), ("DeleteMarkers", True)):
            entries = page.get(label, [])
            if not isinstance(entries, list):
                raise S3RestoreVerificationError("invalid restored S3 version listing")
            for row in entries:
                if not isinstance(row, dict):
                    raise S3RestoreVerificationError("invalid restored S3 version entry")
                key, version = row.get("Key"), row.get("VersionId")
                if not isinstance(key, str) or not isinstance(version, str):
                    raise S3RestoreVerificationError("invalid restored S3 version identity")
                entry = targets.pop((key, version), None)
                if entry is None or delete_marker is not entry.delete_marker or row.get("IsLatest") is not entry.latest:
                    raise S3RestoreVerificationError("restored S3 version differs from the manifest")

    if not complete or targets:
        raise S3RestoreVerificationError("restored S3 version inventory is incomplete")
    delete_markers = sum(entry.delete_marker for entry in expected)
    return S3RestoreInventory(len(expected) - delete_markers, delete_markers)
