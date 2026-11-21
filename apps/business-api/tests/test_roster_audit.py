import json
import unittest
from uuid import uuid4

from foodtech_business_api.roster_audit import audit


class FakeTable:
    name = "foodtech-cooking-jobs-dev"
    def __init__(self):
        self.meta = self; self.client = self; self.pages = []
    def get_paginator(self, operation):
        assert operation == "scan"
        return self
    def paginate(self, **kwargs):
        assert kwargs["TableName"] == self.name and kwargs["ConsistentRead"]
        assert kwargs["ProjectionExpression"] == "#pk, #sk"
        assert kwargs["FilterExpression"] is not None
        return iter(self.pages)


class FakeStore:
    def __init__(self, subject, ids):
        self.table = FakeTable(); self.revisions = [3, 3]
        self.subject = subject; self.rows = {}
        for job_id in ids:
            self.rows[job_id] = {"job_id": job_id, "owner_subject": subject, "version": 1,
                                 "created_at": "2026-01-01T00:00:00Z"}
        self.listed = [row.copy() for row in self.rows.values()]
        self.table.pages = [{"ScannedCount": len(ids), "Items": [
            {"pk": f"SCOPE#USER#{subject}#JOB#{job_id}", "sk": "META"} for job_id in ids]}]
    def history_revision(self, _subject): return self.revisions.pop(0)
    def list_jobs(self, subject, limit, cursor):
        assert subject == self.subject and limit == 50 and cursor is None
        return {"items": self.listed, "next_cursor": None}
    def read_meta(self, pk): return self.rows.get(pk.rsplit("#JOB#", 1)[-1])


class RosterAuditTests(unittest.TestCase):
    def setUp(self):
        self.subject = "audit-owner"; self.ids = [str(uuid4()), str(uuid4())]
        self.store = FakeStore(self.subject, self.ids)

    def test_matches_are_observations_not_authorization_or_identifier_logs(self):
        result = audit(self.store, self.subject)
        self.assertEqual(result["status"], "MATCHED_IN_OBSERVATION")
        self.assertEqual((result["record_count"], result["listed_count"]), (2, 2))
        self.assertFalse(result["immediately_previous_verified"])
        self.assertNotIn(self.subject, json.dumps(result))
        for job_id in self.ids: self.assertNotIn(job_id, json.dumps(result))

    def test_missing_dangling_and_inconsistent_records_are_distinct(self):
        self.store.listed.pop()
        missing = audit(self.store, self.subject)
        self.assertEqual((missing["status"], missing["missing_count"]), ("MISMATCH", 1))
        self.store.revisions = [3, 3]; self.store.listed.append({"job_id": str(uuid4()), "created_at": "x"})
        self.store.rows[self.ids[0]]["created_at"] = "changed"
        result = audit(self.store, self.subject)
        self.assertEqual((result["missing_count"], result["dangling_count"], result["inconsistent_count"]), (1, 1, 1))

    def test_changing_or_untracked_revision_cannot_certify(self):
        self.store.revisions = [3, 4]
        self.assertEqual(audit(self.store, self.subject)["status"], "CHANGED_DURING_AUDIT")
        self.store.revisions = [None, None]
        self.assertEqual(audit(self.store, self.subject)["status"], "UNTRACKED")

    def test_scan_limit_and_foreign_key_fail_closed(self):
        self.store.table.pages = [{"ScannedCount": 2, "Items": [], "LastEvaluatedKey": {"pk": "x"}}]
        self.assertEqual(audit(self.store, self.subject, max_scanned=2)["status"], "SCAN_LIMIT")
        self.store.revisions = [3, 3]
        self.store.table.pages = [{"ScannedCount": 1, "Items": [{"pk": "SCOPE#USER#other#JOB#" + self.ids[0], "sk": "META"}]}]
        self.assertEqual(audit(self.store, self.subject)["status"], "INVALID_RECORD")

    def test_refuses_other_table_or_malformed_scope(self):
        self.store.table.name = "credential-vault-dev"
        with self.assertRaises(ValueError): audit(self.store, self.subject)
        self.store.table.name = "foodtech-cooking-jobs-dev"
        with self.assertRaises(ValueError): audit(self.store, "owner#JOB#other")
