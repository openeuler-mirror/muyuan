import unittest

from harness_optimizer.diagnosis import (
    DIAGNOSIS_SCHEMA_VERSION,
    DIAGNOSIS_TYPE,
    normalize_diagnosis,
)


class DiagnosisTests(unittest.TestCase):
    def test_normalize_diagnosis_exposes_stable_fields(self):
        diagnosis = normalize_diagnosis(
            {
                "root_cause": "worker stopped before verification",
                "failure_mode": ["premature stop", "missing check"],
                "impact": {"cost": "low", "latency": "high"},
                "evidence": [{"id": "trace-7", "kind": "trace", "detail": "stop event"}],
                "recommended_focus": ["preserve final verification"],
                "confidence": 1.4,
            },
            task="audit the repository",
            stage="dev",
            candidate_id="candidate-1",
            metrics={"runner_ok": False, "time_s": 12.0},
            source="external_command",
            trigger="candidate",
        )

        self.assertEqual(diagnosis["type"], DIAGNOSIS_TYPE)
        self.assertEqual(diagnosis["schema_version"], DIAGNOSIS_SCHEMA_VERSION)
        self.assertEqual(diagnosis["status"], "failed")
        self.assertEqual(diagnosis["trigger"], "candidate")
        self.assertEqual(diagnosis["root_cause"], "worker stopped before verification")
        self.assertEqual(diagnosis["failure_modes"], ["premature stop", "missing check"])
        self.assertEqual(diagnosis["evidence_ids"], ["trace-7"])
        self.assertEqual(diagnosis["impact"]["cost"], "low")
        self.assertEqual(diagnosis["confidence"], 1.0)

    def test_normalize_diagnosis_bounds_unstructured_output(self):
        diagnosis = normalize_diagnosis(
            {"diagnosis": {"summary": "x" * 10000, "evidence_ids": ["e1", "e1", 2]}},
            task="task",
            stage="holdout",
            candidate_id="candidate",
            metrics={"runner_ok": True},
            source="external_command",
            trigger="candidate",
        )

        self.assertLessEqual(len(diagnosis["summary"]), 4000)
        self.assertEqual(diagnosis["evidence_ids"], ["e1", "2"])
        self.assertEqual(diagnosis["status"], "regressed")


if __name__ == "__main__":
    unittest.main()
