from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from observation_registry.matching import (
    RecordEvidence,
    OpinionEvidence,
    evaluate_pair,
    haversine_m,
)


def make_evidence(
    record_id: str,
    *,
    latitude: str = "25.0731",
    longitude: str = "102.7408",
    accuracy_m: str = "20",
    observed_at: str = "2026-09-18T00:00:00Z",
    materials: tuple[str, ...] = ("现场照片",),
    opinions: tuple[OpinionEvidence, ...] = (),
    protected: bool = False,
) -> RecordEvidence:
    return RecordEvidence(
        record_id=record_id,
        latitude=Decimal(latitude),
        longitude=Decimal(longitude),
        accuracy_m=Decimal(accuracy_m),
        observed_at=datetime.fromisoformat(observed_at.replace("Z", "+00:00")).astimezone(timezone.utc),
        materials=materials,
        opinions=opinions,
        protected=protected,
    )


class HaversineTests(unittest.TestCase):
    def test_known_distance_band(self) -> None:
        # 昆明附近两点：约 40 米
        distance = haversine_m(Decimal("25.0731"), Decimal("102.7408"),
                               Decimal("25.0734"), Decimal("102.7410"))
        self.assertAlmostEqual(float(distance), 39.0, delta=3.0)

    def test_same_point_is_zero(self) -> None:
        self.assertEqual(haversine_m(Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2")), Decimal("0.0"))


class MatchingTests(unittest.TestCase):
    def test_overlapping_circles_same_time_same_taxon_recommends_merge(self) -> None:
        a = make_evidence(
            "a", accuracy_m="60",
            opinions=(OpinionEvidence("orchid-sp-a", Decimal("0.8")),),
            materials=("现场照片", "生境照片"),
        )
        b = make_evidence(
            "b", latitude="25.0734", longitude="102.7410", accuracy_m="60",
            observed_at="2026-09-18T02:00:00Z",
            opinions=(OpinionEvidence("orchid-sp-a", Decimal("0.7")),),
            materials=("现场照片",),
        )
        result = evaluate_pair(a, b)
        self.assertEqual(result["recommendation"], "merge")
        self.assertGreaterEqual(Decimal(result["confidence"]), Decimal("0.70"))
        self.assertTrue(result["factors"]["spatial"]["overlap"])
        self.assertEqual(result["factors"]["taxonomy"]["relation"], "same")
        self.assertFalse(result["vetoed"])
        self.assertEqual(len(result["fingerprint"]), 64)

    def test_disjoint_circles_are_vetoed_regardless_of_other_evidence(self) -> None:
        a = make_evidence("a", accuracy_m="10")
        b = make_evidence(
            "b", latitude="25.1180", longitude="102.9050", accuracy_m="40",
            opinions=(OpinionEvidence("orchid-sp-a", Decimal("0.99")),),
            materials=("现场照片",),
        )
        result = evaluate_pair(a, b)
        self.assertEqual(result["recommendation"], "no_match")
        self.assertTrue(result["vetoed"])
        self.assertIn("spatial_disjoint", result["veto_reasons"])
        self.assertEqual(result["factors"]["spatial"]["score"], "0.000")

    def test_time_gap_outside_window_is_vetoed(self) -> None:
        a = make_evidence("a", observed_at="2026-09-18T00:00:00Z")
        b = make_evidence("b", observed_at="2026-09-22T00:00:00Z", accuracy_m="60")
        result = evaluate_pair(a, b, time_window_hours="72")
        self.assertEqual(result["recommendation"], "no_match")
        self.assertIn("outside_time_window", result["veto_reasons"])
        # 放宽窗口后不再被时间否决
        wider = evaluate_pair(a, b, time_window_hours="120")
        self.assertNotIn("outside_time_window", wider["veto_reasons"])

    def test_conflicting_taxonomy_caps_score_and_needs_review(self) -> None:
        a = make_evidence("a", accuracy_m="100", opinions=(
            OpinionEvidence("orchid-sp-a", Decimal("0.9")),))
        b = make_evidence(
            "b", latitude="25.0732", longitude="102.7409", accuracy_m="100",
            observed_at="2026-09-18T01:00:00Z",
            opinions=(OpinionEvidence("common-weed", Decimal("0.9")),),
        )
        result = evaluate_pair(a, b)
        self.assertEqual(result["factors"]["taxonomy"]["relation"], "conflicting")
        self.assertEqual(result["factors"]["taxonomy"]["score"], "0.150")
        self.assertNotEqual(result["recommendation"], "merge")

    def test_missing_opinion_is_neutral(self) -> None:
        a = make_evidence("a", accuracy_m="100")
        b = make_evidence(
            "b", latitude="25.0732", longitude="102.7409", accuracy_m="100",
            observed_at="2026-09-18T01:00:00Z",
        )
        result = evaluate_pair(a, b)
        self.assertEqual(result["factors"]["taxonomy"]["relation"], "unassessed")
        self.assertEqual(result["factors"]["taxonomy"]["score"], "0.500")

    def test_materials_jaccard_and_empty_neutrality(self) -> None:
        a = make_evidence("a", materials=())
        b = make_evidence("b", materials=())
        result = evaluate_pair(a, b)
        self.assertEqual(result["factors"]["materials"]["score"], "0.500")

        a2 = make_evidence("a2", materials=("照片", "标本", "笔记"))
        b2 = make_evidence("b2", materials=("照片", "笔记"))
        result2 = evaluate_pair(a2, b2)
        self.assertEqual(result2["factors"]["materials"]["score"], "0.667")
        self.assertEqual(result2["factors"]["materials"]["shared_materials"], ["照片", "笔记"])

    def test_deterministic_fingerprint_for_same_evidence(self) -> None:
        a = make_evidence("a")
        b = make_evidence("b", latitude="25.0734", longitude="102.7410")
        first = evaluate_pair(a, b)
        second = evaluate_pair(a, b)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        c = make_evidence("b", latitude="25.0735", longitude="102.7410")
        changed = evaluate_pair(a, c)
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])

    def test_protection_flag_aggregates_from_opinions(self) -> None:
        a = make_evidence("a")
        b = make_evidence(
            "b", opinions=(OpinionEvidence("taxon", Decimal("0.5"), protected=True),)
        )
        self.assertTrue(evaluate_pair(a, b)["protected_any"])

    def test_invalid_window_rejected(self) -> None:
        a = make_evidence("a")
        b = make_evidence("b")
        with self.assertRaises(ValueError):
            evaluate_pair(a, b, time_window_hours="0")


if __name__ == "__main__":
    unittest.main()
