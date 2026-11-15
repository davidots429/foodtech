import json
import unittest

from foodtech_standardization_worker.jeyuk_bridge import JeyukProcessStream


def sample(second: int, **changes) -> str:
    record = {
        "fixtureId": "jeyuk-small-process-v1", "provenance": "SYNTHETIC",
        "profileId": "mockprofile-110", "instanceId": "mockprofile-110-a-001",
        "profile": "SMALL", "actualLoadKg": 1.8, "second": second,
        "sampleIntervalSeconds": 1, "surfaceCelsius": 200,
        "actualRpm": 30, "targetRpm": 30, "motorLoadPercent": 50,
    }
    record.update(changes)
    return json.dumps(record)


class JeyukBridgeTests(unittest.TestCase):
    def test_rejects_gaps_and_source_changes(self):
        stream = JeyukProcessStream()
        self.assertEqual(stream.accept(sample(1)).status, "NORMAL")
        with self.assertRaisesRegex(ValueError, "consecutive"):
            stream.accept(sample(3))
        with self.assertRaisesRegex(ValueError, "identity changed"):
            stream.accept(sample(2, fixtureId="another-fixture"))
        self.assertEqual(stream.accept(sample(2)).status, "NORMAL")

    def test_rejects_ambiguous_or_averaged_samples(self):
        with self.assertRaisesRegex(ValueError, "second one"):
            JeyukProcessStream().accept(sample(2))
        with self.assertRaisesRegex(ValueError, "one-second"):
            JeyukProcessStream().accept(sample(1, sampleIntervalSeconds=5))
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            JeyukProcessStream().accept(sample(1)[:-1] + ',"second":1}')
        with self.assertRaisesRegex(ValueError, "unexpected Jeyuk Mock source"):
            JeyukProcessStream().accept(sample(1, instanceId="mockprofile-100-a-001"))
        with self.assertRaisesRegex(ValueError, "unexpected Jeyuk Mock source"):
            JeyukProcessStream().accept(sample(1, fixtureId="invalid#fixture"))


if __name__ == "__main__":
    unittest.main()
