import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from botocore.exceptions import EndpointConnectionError

from foodtech_ai_gateway.policy import Budget, Caller, Execution, Feature, Model, Tier
from foodtech_ai_gateway.service import GatewayService
from foodtech_ai_gateway.tonkatsu_read_handoff import ReceivedTonkatsuReads
from foodtech_contracts.tonkatsu_controller_samples import TonkatsuControllerSummary
from foodtech_contracts.tonkatsu_observed_inputs import ObservedTonkatsuBatch


class GuardedTonkatsuServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        now = datetime(2026, 11, 28, tzinfo=timezone.utc)
        zero, temperature = Decimal(0), Decimal(170)
        self.execution = Execution("execution", "customer", Feature.RECIPE_CORRECTION,
                                   "local-model", None, "SYNTHETIC", zero,
                                   job_id="private-current-job", prior_job_id="private-prior-job")
        self.registry = Mock()
        self.registry.tonkatsu_execution.return_value = self.execution
        self.registry.verify_tonkatsu_execution.return_value = True
        self.registry.model.return_value = Model("local-model", "ollama", Tier.TIER_1, "ACTIVE", True, True, now, now)
        self.registry.local_available.return_value = True
        self.registry.budget.return_value = Budget(zero, zero, zero, 3)
        self.service = GatewayService(self.registry)
        self.guard = object.__new__(ReceivedTonkatsuReads)
        self.ollama = AsyncMock()
        self.acceptable = SimpleNamespace(text='{"temperature_c":170,"duration_seconds":300}')
        self.ollama.structured_chat.return_value = self.acceptable
        summary = TonkatsuControllerSummary(
            sample_count=301, calibration_all_ok=True, minimum_sample_c=temperature,
            maximum_sample_c=temperature, preplacement_sample_c=temperature, preplacement_age_us=1000000,
            preplacement_calibration_ok=True, below_160_sample_span_seconds=0, above_180_sample_span_seconds=0,
            longest_above_180_sample_span_seconds=0, recoveries=(),
        )
        self.current = ObservedTonkatsuBatch(
            job_id=self.execution.job_id, profile="STANDARD", cooking_seconds=300, load_kg=Decimal("0.32"),
            sample_count=63, minimum_sample_c=temperature, maximum_sample_c=temperature,
            last_sample_c=temperature, preplacement_sample_c=temperature, preplacement_age_us=5000000,
            ingredient_temperature_c=Decimal(4), storage_state="CHILLED", oil_capacity_l=Decimal(10),
            oil_batches_since_change=1, oil_cooked_mass_kg=Decimal(2), recorded_safety_status="PASSED",
            core_series_consistent=True, controller_summary=summary, unresolved=(),
        )
        self.prior = replace(self.current, job_id=self.execution.prior_job_id, recorded_safety_status="FAILED")

    async def execute(self):
        with patch.object(ReceivedTonkatsuReads, "observe_inputs", return_value=(self.current, self.prior)):
            return await self.service.execute_observed_tonkatsu_recipe(
                Caller("business-api", "customer"), self.execution.execution_id,
                tonkatsu_source_guard=self.guard, ollama=self.ollama,
            )

    async def test_retry_reserves_each_call_and_only_persists_candidate_after_revalidation(self):
        self.ollama.structured_chat.side_effect = [SimpleNamespace(text="invalid"), self.acceptable]
        outcome = await self.execute()
        self.assertEqual(outcome.state, "SUCCEEDED")
        self.assertEqual(json.loads(outcome.response_text)["status"], "NEEDS_REVIEW")
        self.registry.reserve.assert_called_once_with(self.execution, "ollama", tonkatsu_source_guard=self.guard)
        self.registry.reserve_local_retry.assert_called_once_with(self.execution, 2, tonkatsu_source_guard=self.guard)
        self.assertEqual([call.kwargs["completed_calls"] for call in self.registry.verify_tonkatsu_execution.call_args_list],
                         [0, 1, 1, 2, 2])
        settlement = self.registry.finish.call_args.kwargs
        self.assertTrue(settlement["succeeded"])
        self.assertEqual(settlement["response_text"], outcome.response_text)
        self.assertIs(settlement["tonkatsu_source_guard"], self.guard)
        for call in self.ollama.structured_chat.await_args_list:
            self.assertNotIn("private-current-job", str(call))
            self.assertNotIn("private-prior-job", str(call))

    async def test_incomplete_observation_denied_before_reservation(self):
        self.current = replace(self.current, unresolved=("REHEAT_TIMELINE_REQUIRES_SEPARATE_OBSERVATION",))
        outcome = await self.execute()
        self.assertEqual((outcome.state, outcome.error_code), ("DENIED", "RECOMMENDATION_EVIDENCE_UNVERIFIED"))
        self.registry.reserve.assert_not_called()
        self.registry.finish.assert_not_called()
        self.ollama.structured_chat.assert_not_awaited()

    async def test_source_change_after_model_call_closes_failure_without_candidate(self):
        self.registry.verify_tonkatsu_execution.side_effect = [True, True, False]
        outcome = await self.execute()
        self.assertEqual((outcome.state, outcome.error_code), ("FAILED", "RECIPE_SOURCE_CHANGED_OR_UNAVAILABLE"))
        self.ollama.structured_chat.assert_awaited_once()
        self.assertIsNone(outcome.response_text)
        settlement = self.registry.finish.call_args.kwargs
        self.assertFalse(settlement["succeeded"])
        self.assertIsNone(settlement["response_text"])
        self.assertIs(settlement["tonkatsu_source_guard"], self.guard)

    async def test_unknown_reservation_outcome_never_calls_model_or_guesses_settlement(self):
        self.registry.reserve.side_effect = EndpointConnectionError(endpoint_url="https://invalid.example")
        outcome = await self.execute()
        self.assertEqual((outcome.state, outcome.error_code), ("INDETERMINATE", "RESERVATION_RECONCILIATION_REQUIRED"))
        self.registry.finish.assert_not_called()
        self.ollama.structured_chat.assert_not_awaited()
