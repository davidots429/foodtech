import unittest
from datetime import datetime, timezone

import boto3
from botocore.stub import Stubber

from foodtech_business_api.s3_restore_verification import (
    RestoredS3Version, S3RestoreInventory, S3RestoreVerificationError, verify_s3_restored_versions,
)


class S3RestoreVerificationTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client("s3", region_name="ap-southeast-2",
                                   aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
        self.buckets = {"source_bucket": "foodtech-prompts-dev",
                        "restored_bucket": "foodtech-prompts-restore-dev", "expected_owner": "123456789012"}
        self.bucket = {"Bucket": self.buckets["restored_bucket"], "ExpectedBucketOwner": self.buckets["expected_owner"]}
        self.scope = {**self.bucket, "Prefix": "account/"}
        self.key = "account/object"
        self.versions = (RestoredS3Version(self.key, "old", False, False),
                         RestoredS3Version(self.key, "tomb", True, True))
        stamp = datetime(2026, 11, 28, tzinfo=timezone.utc)
        self.old = {"Key": self.key, "VersionId": "old", "IsLatest": False, "LastModified": stamp}
        self.marker = {"Key": self.key, "VersionId": "tomb", "IsLatest": True, "LastModified": stamp}

    def verify(self, expected):
        return verify_s3_restored_versions(self.client, prefix=self.scope["Prefix"], expected=expected, **self.buckets)

    def enable(self, stub):
        stub.add_response("get_bucket_versioning", {"Status": "Enabled"}, self.bucket)

    def test_exact_object_versions_and_delete_markers_across_both_markers(self):
        with Stubber(self.client) as stub:
            self.enable(stub)
            stub.add_response("list_object_versions", {"IsTruncated": True, "NextKeyMarker": self.key,
                "NextVersionIdMarker": "tomb", "DeleteMarkers": [self.marker]}, self.scope)
            stub.add_response("list_object_versions", {"IsTruncated": False, "Versions": [self.old]},
                              {**self.scope, "KeyMarker": self.key, "VersionIdMarker": "tomb"})
            self.assertEqual(self.verify(self.versions), S3RestoreInventory(1, 1))
            stub.assert_no_pending_responses()

    def test_missing_wrong_current_extra_or_duplicate_version_fails(self):
        for changes in ({"DeleteMarkers": []},
                        {"DeleteMarkers": [{**self.marker, "IsLatest": False}]},
                        {"Versions": [self.old, self.old]},
                        {"Versions": [self.old, {**self.old, "VersionId": "foreign"}]}):
            with self.subTest(changes=changes), Stubber(self.client) as stub:
                self.enable(stub)
                page = {"IsTruncated": False, "Versions": [self.old], "DeleteMarkers": [self.marker]}
                stub.add_response("list_object_versions", {**page, **changes}, self.scope)
                with self.assertRaises(S3RestoreVerificationError):
                    self.verify(self.versions)
                stub.assert_no_pending_responses()

    def test_empty_scope_requires_terminal_page_and_enabled_versioning(self):
        with Stubber(self.client) as stub:
            self.enable(stub)
            stub.add_response("list_object_versions", {"IsTruncated": False}, self.scope)
            self.assertEqual(self.verify(()), S3RestoreInventory(0, 0))
            stub.add_response("get_bucket_versioning", {"Status": "Suspended"}, self.bucket)
            with self.assertRaises(S3RestoreVerificationError):
                self.verify(())
            self.enable(stub)
            stub.add_response("list_object_versions", {"IsTruncated": True}, self.scope)
            with self.assertRaises(S3RestoreVerificationError):
                self.verify(())
            stub.assert_no_pending_responses()

    def test_unscoped_duplicate_or_missing_latest_manifest_rejected_before_aws(self):
        with Stubber(self.client) as stub:
            for manifest in ((self.versions[0],), self.versions * 2,
                             (RestoredS3Version("account-other/object", "old", False, True),),
                             (RestoredS3Version(self.key, "null", False, True),)):
                with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                    self.verify(manifest)
            stub.assert_no_pending_responses()
