from __future__ import annotations

import unittest
from pathlib import Path

from observation_registry.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class ObservationAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["submitted_records"], 4)
        self.assertEqual(result["candidate_count"], 3)
        self.assertEqual(result["effective_privacy_level"], "curators")
        self.assertEqual(
            set(result["all_sources_preserved"]),
            {"obs-ranger-001", "obs-volunteer-014", "obs-school-207"},
        )
        self.assertIn("spatial_disjoint", result["veto_reasons"])
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertTrue(result["reopened_candidate_pairs"])


if __name__ == "__main__":
    unittest.main()
