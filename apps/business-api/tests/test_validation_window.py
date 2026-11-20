import unittest
from datetime import datetime, timedelta, timezone

from foodtech_contracts.validation_window import Invalidation, evaluate


class ValidationWindowTests(unittest.TestCase):
    def test_expiration_is_half_open_and_timezone_aware(self):
        start = datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=9)))
        self.assertEqual(evaluate(None, [], now=start).status, "VALIDATION_REQUIRED")
        self.assertEqual(evaluate(start, [], now=start + timedelta(hours=72, microseconds=-1)).status, "VALIDATED_ACTIVE")
        expired = evaluate(start, [], now=start + timedelta(hours=72))
        self.assertEqual(expired.status, "VALIDATION_EXPIRED")
        self.assertEqual(expired.expires_at, start.astimezone(timezone.utc) + timedelta(hours=72))

    def test_first_effective_invalidation_wins_and_future_event_does_not_apply_early(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        events = [Invalidation(start + timedelta(hours=8), "LOT_CHANGED"),
                  Invalidation(start + timedelta(hours=3), "SAFETY_ANOMALY")]
        self.assertEqual(evaluate(start, events, now=start + timedelta(hours=2)).status, "VALIDATED_ACTIVE")
        result = evaluate(start, events, now=start + timedelta(hours=10))
        self.assertEqual((result.status, result.expires_at, result.invalidation.reason),
                         ("REVALIDATION_REQUIRED", start + timedelta(hours=3), "SAFETY_ANOMALY"))
        self.assertEqual(evaluate(start, events, now=start + timedelta(days=5)).status, "REVALIDATION_REQUIRED")

    def test_invalid_or_ambiguous_evidence_is_rejected(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        for validated, events, now in ((start, [], start.replace(tzinfo=None)),
                (start, [Invalidation(start - timedelta(seconds=1), "LOT_CHANGED")], start),
                (start, [Invalidation(start, " ")], start), (start + timedelta(seconds=1), [], start),
                (None, [Invalidation(start, "LOT_CHANGED")], start)):
            with self.subTest(validated=validated, events=events, now=now), self.assertRaises(ValueError):
                evaluate(validated, events, now=now)

    def test_post_expiration_change_does_not_rewrite_earlier_expiry(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        later = Invalidation(start + timedelta(hours=80), "LOT_CHANGED")
        result = evaluate(start, [later], now=start + timedelta(hours=81))
        self.assertEqual((result.status, result.expires_at), ("VALIDATION_EXPIRED", start + timedelta(hours=72)))


if __name__ == "__main__":
    unittest.main()
