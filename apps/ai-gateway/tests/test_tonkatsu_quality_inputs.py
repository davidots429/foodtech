import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from foodtech_ai_gateway.tonkatsu_quality_inputs import observe_tonkatsu_quality
from foodtech_contracts.quality_score import SCORE_ITEMS
from foodtech_contracts.tonkatsu_observed_inputs import TonkatsuBatchFacts


class TonkatsuQualityInputsTests(unittest.TestCase):
    def setUp(self):
        self.table = Mock()
        self.job = {
            "pk": "scoped-job", "status": "COMPLETED", "evaluation_status": "COMPLETED",
            "evaluation_blocked": False, "safety_status": "PASSED", "safety_version": Decimal(3),
            "job_state_version": Decimal(2), "planned_portions": Decimal(2),
        }
        self.batch = SimpleNamespace(recorded_safety_status="PASSED", load_kg=Decimal(".32"),
            batch_facts=TonkatsuBatchFacts(Decimal("1.25"), (160,160), "PARTIAL", "tonkatsu-batch-v2",
                                         Decimal(4), Decimal(8)))
        self.revision = {
            "pk": self.job["pk"], "sk": "ASSESSMENT#REV#000000000004", "version": Decimal(4),
            "status": "COMPLETED", "menu": "TONKATSU", "equipment_profile": "synthetic-fryer-approved-v2",
            "safety_version": Decimal(3), "job_state_version": Decimal(2), "scale_version": "tonkatsu-common-v1",
            "formula_version": "quality-v1", "scores": dict.fromkeys(SCORE_ITEMS, Decimal(4)),
            "score_total": Decimal(28), "score_percent": "80.00",
            "quality_status": "QUALITY_PASSED", "quality_passed": True, "quality_target": True,
            "quality_decision": None, "planned_portions": Decimal(2), "actual_portions": Decimal(2),
            "actual_mass_kg": "0.32", "load_percent": "8.00", "load_band": "PARTIAL",
            "tonkatsu_batch_rule_version": "tonkatsu-batch-v2", "assessed_by": "private-assessor",
            "observations": {"taste": "private-observation"},
        }
        self.pointer = {
            "pk": self.job["pk"], "sk": "ASSESSMENT#CURRENT", "version": Decimal(4),
            "assessment_sk": self.revision["sk"], "safety_version": Decimal(3), "usable": True,
            "blocked_reason": None, "quality_passed": True, "quality_target": True, "score_percent": "80.00",
        }

    def observe(self, *rows):
        self.table.reset_mock()
        self.table.get_item.side_effect = [{"Item": row} if row is not None else {} for row in rows]
        return observe_tonkatsu_quality(self.table, self.job, self.batch)

    def test_reads_only_current_scoped_revision_and_returns_allowlisted_scores(self):
        result = self.observe(self.pointer, self.revision).model_context()
        self.assertEqual((result["status"], result["version"], result["quality_status"], result["score_percent"]),
                         ("CURRENT", 4, "QUALITY_PASSED", "80.00"))
        self.assertEqual(result["scores"], dict.fromkeys(SCORE_ITEMS, 4))
        self.assertNotIn("private-", str(result))
        requests = [call.kwargs for call in self.table.get_item.call_args_list]
        self.assertEqual([request["Key"] for request in requests],
                         [{"pk": self.job["pk"], "sk": key} for key in (self.pointer["sk"], self.revision["sk"])])
        for request in requests:
            self.assertTrue(request["ConsistentRead"])
            self.assertNotIn("assessed_by", request["ExpressionAttributeNames"].values())
            self.assertNotIn("observations", request["ExpressionAttributeNames"].values())

    def test_invalid_or_stale_quality_cannot_become_current(self):
        for pointer, revision in (
            (None, self.revision),
            ({**self.pointer, "safety_version": 2}, self.revision),
            ({**self.pointer, "assessment_sk": "META"}, self.revision),
            (self.pointer, {**self.revision, "job_state_version": 1}),
            (self.pointer, {**self.revision, "pk": "another-job"}),
            (self.pointer, {**self.revision, "score_total": 30}),
            (self.pointer, {**self.revision, "actual_mass_kg": "invalid"}),
        ):
            with self.subTest(pointer=pointer, revision=revision), self.assertRaises(ValueError):
                self.observe(pointer, revision)

    def test_safety_failure_never_loads_or_reuses_a_blocked_assessment(self):
        self.job.update(safety_status="FAILED", evaluation_blocked=True)
        self.batch.recorded_safety_status = "FAILED"
        for pointer in (None, {**self.pointer, "usable": False, "blocked_reason": "SAFETY_FAILED"}):
            self.assertEqual(self.observe(pointer).model_context(), {"status": "BLOCKED_BY_SAFETY"})
            self.table.get_item.assert_called_once()
        with self.assertRaises(ValueError):
            self.observe(self.pointer)
