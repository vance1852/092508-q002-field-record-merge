from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from field_observations.acceptance import run
from field_observations.clock import FrozenClock
from field_observations.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from field_observations.matching import ReportView, evaluate_pair, haversine_m
from field_observations.service import FieldObservationService


ROOT = Path(__file__).resolve().parents[1]


def make_report(report_id: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "report_id": report_id,
        "taxon_name": "Cypripedium tibeticum",
        "latitude": "31.04100",
        "longitude": "103.18200",
        "coordinate_uncertainty_m": "5",
        "observed_at": "2026-09-25T06:12:00+08:00",
        "visibility": "restricted",
        "protected": True,
        "media": [{"sha256": "1" * 64, "captured_at": "2026-09-25T06:12:00+08:00"}],
        "note": "",
    }
    base.update(overrides)
    return base


class MatchingTests(unittest.TestCase):
    def view(self, report_id: str, **overrides: object) -> ReportView:
        base: dict[str, object] = {
            "report_id": report_id,
            "effective_taxon": "Cypripedium tibeticum",
            "latitude": Decimal("31.04100"),
            "longitude": Decimal("103.18200"),
            "coordinate_uncertainty_m": Decimal("5"),
            "observed_at": datetime(2026, 9, 24, 22, 12, tzinfo=timezone.utc),
            "media_hashes": frozenset({"a" * 64}),
        }
        base.update(overrides)
        return ReportView(**base)  # type: ignore[arg-type]

    def test_haversine_is_deterministic(self) -> None:
        first = haversine_m(Decimal("31.041"), Decimal("103.182"), Decimal("31.0411"), Decimal("103.1821"))
        second = haversine_m(Decimal("31.041"), Decimal("103.182"), Decimal("31.0411"), Decimal("103.1821"))
        self.assertEqual(first, second)
        self.assertGreater(first, Decimal(0))

    def test_identical_reports_score_high(self) -> None:
        evaluation = evaluate_pair(self.view("a"), self.view("b"))
        self.assertEqual(evaluation.confidence, "high")
        self.assertEqual(len(evaluation.reasons), 4)
        factors = {reason["factor"] for reason in evaluation.reasons}
        self.assertEqual(factors, {"distance", "time", "taxonomy", "media"})

    def test_distant_pair_scores_low(self) -> None:
        far = self.view(
            "b",
            latitude=Decimal("31.07200"),
            longitude=Decimal("103.20100"),
            effective_taxon="Paphiopedilum sp.",
            media_hashes=frozenset(),
        )
        evaluation = evaluate_pair(self.view("a"), far)
        self.assertEqual(evaluation.confidence, "low")
        taxonomy = next(r for r in evaluation.reasons if r["factor"] == "taxonomy")
        self.assertEqual(taxonomy["component"], "0")
        self.assertIn("不一致", taxonomy["explanation"])

    def test_same_genus_partial_taxonomy_credit(self) -> None:
        other = self.view("b", effective_taxon="Cypripedium sp.")
        evaluation = evaluate_pair(self.view("a"), other)
        taxonomy = next(r for r in evaluation.reasons if r["factor"] == "taxonomy")
        self.assertEqual(taxonomy["component"], "0.6")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))
        self.service = FieldObservationService(self.connection, self.clock)
        for user_id, role in (
            ("ranger", "reporter"),
            ("volunteer", "reporter"),
            ("school", "reporter"),
            ("curator", "curator"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def submit_three_reports(self) -> None:
        self.service.submit_report("ranger", make_report("R1"))
        self.service.submit_report("volunteer", make_report(
            "R2",
            latitude="31.04108", longitude="103.18212",
            coordinate_uncertainty_m="30", observed_at="2026-09-25T06:47:00+08:00",
            visibility="public", media=[{"sha256": "2" * 64}],
        ))
        self.service.submit_report("school", make_report(
            "R3",
            taxon_name="Cypripedium sp.",
            latitude="31.04092", longitude="103.18230",
            coordinate_uncertainty_m="120", observed_at="2026-09-25T07:30:00+08:00",
            visibility="sensitive", media=[{"sha256": "2" * 64}, {"sha256": "3" * 64}],
        ))

    def candidate_by_pair(self, left: str, right: str) -> dict[str, object]:
        for candidate in self.service.list_candidates("curator"):
            if {candidate["left_report_id"], candidate["right_report_id"]} == {left, right}:
                return candidate
        raise AssertionError(f"候选不存在: {left}/{right}")

    def test_candidates_created_with_explainable_reasons(self) -> None:
        self.submit_three_reports()
        self.service.submit_report("volunteer", make_report(
            "R4",
            taxon_name="Paphiopedilum sp.",
            latitude="31.07200", longitude="103.20100",
            coordinate_uncertainty_m="50", observed_at="2026-09-25T09:05:00+08:00",
            visibility="public", protected=False, media=[],
        ))
        candidates = self.service.list_candidates("curator")
        self.assertEqual(len(candidates), 3)
        detail = self.service.get_candidate("curator", self.candidate_by_pair("R1", "R2")["candidate_id"])
        evaluation = detail["latest_evaluation"]
        self.assertEqual(detail["status"], "open")
        self.assertEqual(len(evaluation["reasons"]), 4)
        factors = {reason["factor"] for reason in evaluation["reasons"]}
        self.assertEqual(factors, {"distance", "time", "taxonomy", "media"})
        self.assertIn(evaluation["confidence"], {"medium", "high"})
        self.assertIn("复核", detail["explanation"])

    def test_merge_preserves_sources_and_covers_member_circles(self) -> None:
        self.submit_three_reports()
        candidate = self.candidate_by_pair("R1", "R2")
        decided = self.service.decide("curator", candidate["candidate_id"], "merge", "同一植株", "k1")
        unified = self.service.get_unified("auditor", decided["unified_id"])
        self.assertEqual(unified["status"], "active")
        self.assertEqual(unified["visibility"], "restricted")
        self.assertTrue(unified["protected"])
        self.assertEqual({m["report_id"] for m in unified["members"]}, {"R1", "R2"})
        centroid_lat = Decimal(unified["location"]["latitude"])
        centroid_lon = Decimal(unified["location"]["longitude"])
        published = Decimal(unified["location"]["published_uncertainty_m"])
        for member in unified["members"]:
            distance = haversine_m(
                centroid_lat, centroid_lon,
                Decimal(member["location"]["latitude"]), Decimal(member["location"]["longitude"]),
            )
            self.assertGreaterEqual(
                published, distance + Decimal(member["location"]["coordinate_uncertainty_m"])
            )
        event_types = [event["event_type"] for event in unified["timeline"]]
        self.assertIn("unified.created", event_types)
        report = self.service.get_report("ranger", "R1")
        self.assertEqual(report["unified"]["unified_id"], decided["unified_id"])

    def test_second_merge_absorbs_remaining_pair(self) -> None:
        self.submit_three_reports()
        first = self.service.decide(
            "curator", self.candidate_by_pair("R1", "R2")["candidate_id"], "merge", "同一植株", "k1"
        )
        second = self.service.decide(
            "curator", self.candidate_by_pair("R1", "R3")["candidate_id"], "merge", "并入同一植株", "k2"
        )
        merged = self.service.get_unified("auditor", second["unified_id"])
        self.assertEqual(len(merged["members"]), 3)
        self.assertEqual(merged["visibility"], "sensitive")
        superseded = self.service.get_unified("auditor", first["unified_id"])
        self.assertEqual(superseded["status"], "superseded")
        absorbed = self.service.get_candidate(
            "auditor", self.candidate_by_pair("R2", "R3")["candidate_id"]
        )
        self.assertEqual(absorbed["status"], "merged")
        self.assertIsNotNone(absorbed["absorbed_by_decision_id"])
        self.assertIn("归入同一统一观察记录", absorbed["explanation"])
        with self.assertRaises(InvalidState):
            self.service.decide(
                "curator", absorbed["candidate_id"], "merge", "已在同一统一记录中", "k3"
            )

    def test_decision_idempotent_replay_and_conflict(self) -> None:
        self.submit_three_reports()
        candidate_id = self.candidate_by_pair("R1", "R2")["candidate_id"]
        first = self.service.decide("curator", candidate_id, "merge", "同一植株", "k1")
        replay = self.service.decide("curator", candidate_id, "merge", "同一植株", "k1")
        self.assertEqual(first, replay)
        count = self.connection.execute("SELECT count(*) FROM merge_decisions").fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaises(Conflict):
            self.service.decide("curator", candidate_id, "reject", "不同内容", "k1")
        with self.assertRaises(Conflict):
            self.service.decide("curator", candidate_id, "merge", "同一植株", "k2")

    def test_split_restores_previous_cluster_and_reopens_absorbed(self) -> None:
        self.submit_three_reports()
        first = self.service.decide(
            "curator", self.candidate_by_pair("R1", "R2")["candidate_id"], "merge", "同一植株", "k1"
        )
        second = self.service.decide(
            "curator", self.candidate_by_pair("R1", "R3")["candidate_id"], "merge", "并入", "k2"
        )
        split = self.service.decide(
            "curator", self.candidate_by_pair("R1", "R3")["candidate_id"], "split",
            "新证据表明并非同一植株", "k3",
        )
        self.assertEqual(split["supersedes_decision_id"], second["decision_id"])
        restored = self.service.get_unified("auditor", first["unified_id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual({m["report_id"] for m in restored["members"]}, {"R1", "R2"})
        revoked = self.service.get_unified("auditor", second["unified_id"])
        self.assertEqual(revoked["status"], "revoked")
        reopened = self.service.get_candidate(
            "auditor", self.candidate_by_pair("R2", "R3")["candidate_id"]
        )
        self.assertEqual(reopened["status"], "open")
        rejected = self.service.decide(
            "curator", self.candidate_by_pair("R1", "R3")["candidate_id"], "reject",
            "分类意见更新后不满足合并条件", "k4",
        )
        self.assertEqual(rejected["candidate_status"], "rejected")
        detail = self.service.get_candidate("auditor", self.candidate_by_pair("R1", "R3")["candidate_id"])
        self.assertIn("分类意见更新后不满足合并条件", detail["explanation"])
        self.assertEqual([d["action"] for d in detail["decisions"]], ["merge", "split", "reject"])

    def test_split_must_follow_decision_stack_order(self) -> None:
        self.submit_three_reports()
        self.service.decide("curator", self.candidate_by_pair("R1", "R2")["candidate_id"], "merge", "并", "k1")
        self.service.decide("curator", self.candidate_by_pair("R1", "R3")["candidate_id"], "merge", "并", "k2")
        with self.assertRaises(InvalidState):
            self.service.decide(
                "curator", self.candidate_by_pair("R1", "R2")["candidate_id"], "split", "先拆旧的", "k3"
            )

    def test_merge_after_reject_when_new_evidence_arrives(self) -> None:
        self.submit_three_reports()
        candidate_id = self.candidate_by_pair("R1", "R3")["candidate_id"]
        self.service.decide("curator", candidate_id, "reject", "学校记录只鉴定到属", "k1")
        added = self.service.add_identification(
            "curator", "R3", "ident-1",
            {"taxon_name": "Cypripedium tibeticum", "confidence": "high", "note": "复核确认到种"},
        )
        self.assertTrue(any(item["candidate_id"] == candidate_id for item in added["reevaluated"]))
        merged = self.service.decide("curator", candidate_id, "merge", "新证据支持合并", "k2")
        self.assertEqual(merged["candidate_status"], "merged")
        detail = self.service.get_candidate("auditor", candidate_id)
        self.assertEqual([d["action"] for d in detail["decisions"]], ["reject", "merge"])
        self.assertEqual(detail["decisions"][0]["superseded_by_decision_id"], merged["decision_id"])
        scores = [evaluation["score"] for evaluation in detail["evaluations"]]
        self.assertEqual(len(scores), 2)
        self.assertLess(Decimal(scores[0]), Decimal(scores[1]))

    def test_identification_idempotent_replay(self) -> None:
        self.submit_three_reports()
        payload = {"taxon_name": "Cypripedium tibeticum", "confidence": "medium", "note": ""}
        first = self.service.add_identification("curator", "R1", "ident-1", payload)
        replay = self.service.add_identification("curator", "R1", "ident-1", payload)
        self.assertEqual(first, replay)
        with self.assertRaises(Conflict):
            self.service.add_identification(
                "curator", "R1", "ident-1",
                {"taxon_name": "Cypripedium sp.", "confidence": "low", "note": ""},
            )

    def test_submit_report_replays_same_content(self) -> None:
        first = self.service.submit_report("ranger", make_report("R1"))
        replay = self.service.submit_report("ranger", make_report("R1"))
        self.assertEqual(first, replay)
        changed = make_report("R1", note="改动后的内容")
        with self.assertRaises(Conflict):
            self.service.submit_report("ranger", changed)

    def test_privacy_not_widened_by_merge(self) -> None:
        self.submit_three_reports()
        self.service.decide("curator", self.candidate_by_pair("R1", "R3")["candidate_id"], "merge", "并", "k1")
        unified_id = self.connection.execute("SELECT max(unified_id) FROM unified_observations").fetchone()[0]
        unified = self.service.get_unified("auditor", unified_id)
        self.assertEqual(unified["visibility"], "sensitive")
        with self.assertRaises(Forbidden):
            self.service.get_unified("volunteer", unified_id)
        contributor_view = self.service.get_unified("ranger", unified_id)
        self.assertIn("latitude", contributor_view["location"])
        member_locations = {
            member["report_id"]: member["location"] for member in contributor_view["members"]
        }
        self.assertEqual(member_locations["R3"], {"masked": True})
        self.assertIn("latitude", member_locations["R1"])

    def test_restricted_unified_masks_location_for_outsiders(self) -> None:
        self.submit_three_reports()
        decided = self.service.decide(
            "curator", self.candidate_by_pair("R1", "R2")["candidate_id"], "merge", "并", "k1"
        )
        outsider = self.service.get_unified("school", decided["unified_id"])
        self.assertEqual(outsider["location"], {"masked": True})
        member_locations = {m["report_id"]: m["location"] for m in outsider["members"]}
        self.assertEqual(member_locations["R1"], {"masked": True})
        self.assertIn("latitude", member_locations["R2"])

    def test_report_visibility_rules(self) -> None:
        self.submit_three_reports()
        with self.assertRaises(Forbidden):
            self.service.get_report("volunteer", "R3")
        masked = self.service.get_report("volunteer", "R1")
        self.assertEqual(masked["location"], {"masked": True})
        own = self.service.get_report("school", "R3")
        self.assertIn("latitude", own["location"])
        public = self.service.get_report("ranger", "R2")
        self.assertIn("latitude", public["location"])

    def test_role_separation(self) -> None:
        self.submit_three_reports()
        candidate_id = self.candidate_by_pair("R1", "R2")["candidate_id"]
        with self.assertRaises(Forbidden):
            self.service.decide("ranger", candidate_id, "merge", "越权", "k1")
        with self.assertRaises(Forbidden):
            self.service.decide("auditor", candidate_id, "merge", "越权", "k1")
        with self.assertRaises(Forbidden):
            self.service.submit_report("curator", make_report("R9"))
        with self.assertRaises(Forbidden):
            self.service.list_candidates("ranger")
        with self.assertRaises(Forbidden):
            self.service.reevaluate_candidate("ranger", candidate_id)

    def test_reevaluate_skips_unchanged_evidence(self) -> None:
        self.submit_three_reports()
        candidate_id = self.candidate_by_pair("R1", "R2")["candidate_id"]
        before = self.service.get_candidate("curator", candidate_id)
        self.assertEqual(len(before["evaluations"]), 1)
        self.service.reevaluate_candidate("curator", candidate_id)
        after = self.service.get_candidate("curator", candidate_id)
        self.assertEqual(len(after["evaluations"]), 1)
        self.service.add_identification(
            "school", "R2", "ident-9",
            {"taxon_name": "Cypripedium flavum", "confidence": "high", "note": "补充鉴定改判"},
        )
        updated = self.service.get_candidate("curator", candidate_id)
        self.assertEqual(len(updated["evaluations"]), 2)

    def test_merge_same_unified_rejected(self) -> None:
        self.submit_three_reports()
        candidate_id = self.candidate_by_pair("R1", "R2")["candidate_id"]
        self.service.decide("curator", candidate_id, "merge", "并", "k1")
        with self.assertRaises(Conflict):
            self.service.decide("curator", candidate_id, "merge", "重复", "k2")

    def test_missing_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_report("curator", "R-x")
        with self.assertRaises(NotFound):
            self.service.get_candidate("curator", 999)
        with self.assertRaises(NotFound):
            self.service.get_unified("curator", 999)
        with self.assertRaises(NotFound):
            self.service.decide("curator", 999, "merge", "无候选", "k1")

    def test_validation_errors(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.submit_report("ranger", make_report("R1", latitude="91"))
        with self.assertRaises(ValidationFailed):
            self.service.submit_report("ranger", make_report("R1", visibility="secret"))
        with self.assertRaises(ValidationFailed):
            self.service.submit_report("ranger", make_report("R1", observed_at="不是时间"))
        self.submit_three_reports()
        candidate_id = self.candidate_by_pair("R1", "R2")["candidate_id"]
        with self.assertRaises(ValidationFailed):
            self.service.decide("curator", candidate_id, "hold", "未知动作", "k1")
        with self.assertRaises(ValidationFailed):
            self.service.decide("curator", candidate_id, "merge", "", "k1")
        with self.assertRaises(ValidationFailed):
            self.service.decide("curator", candidate_id, "merge", "缺键", "")

    def test_unified_listing_scoped_for_reporters(self) -> None:
        self.submit_three_reports()
        self.service.decide("curator", self.candidate_by_pair("R1", "R2")["candidate_id"], "merge", "并", "k1")
        ranger_list = self.service.list_unified("ranger")
        self.assertEqual(len(ranger_list), 1)
        school_list = self.service.list_unified("school")
        self.assertEqual(school_list, [])
        auditor_list = self.service.list_unified("auditor")
        self.assertEqual(len(auditor_list), 1)

    def test_audit_trail_covers_decision_timeline(self) -> None:
        self.submit_three_reports()
        candidate_id = self.candidate_by_pair("R1", "R2")["candidate_id"]
        self.service.decide("curator", candidate_id, "merge", "并", "k1")
        events = self.service.list_audit("auditor", "candidate", str(candidate_id))
        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types, ["candidate.raised", "candidate.decision_recorded"])
        with self.assertRaises(Forbidden):
            self.service.list_audit("ranger", "candidate", str(candidate_id))


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"], 3)
        self.assertEqual(result["restored_members"], ["R-001", "R-002"])
        self.assertEqual(result["restored_visibility"], "restricted")
        self.assertTrue(result["idempotent_replay"])
        self.assertTrue(result["sensitive_hidden"])
        self.assertEqual(result["schema"]["missing_tables"], [])


if __name__ == "__main__":
    unittest.main()
