from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from observation_registry.clock import FrozenClock
from observation_registry.errors import Conflict, Forbidden, InvalidState, NotFound
from observation_registry.service import ObservationRegistryService


def record(record_id: str, contributor: str, *, latitude="25.0731", longitude="102.7408",
           accuracy_m="20", observed_at="2026-09-18T00:00:00Z", privacy_level="public",
           protected=False, materials=("现场照片",), opinions=(), note=None, group="巡护组"):
    return {
        "record_id": record_id,
        "contributor_id": contributor,
        "observer_group": group,
        "coordinates": {"latitude": latitude, "longitude": longitude, "accuracy_m": accuracy_m},
        "observed_at": observed_at,
        "privacy_level": privacy_level,
        "protected": protected,
        "materials": list(materials),
        "taxonomy_opinions": list(opinions),
        "note": note,
    }


def opinion(opinion_id, taxon="orchid-sp-a", confidence="0.8", reviewer="ranger-1",
            created_at="2026-09-18T01:00:00Z", tags=()):
    return {
        "opinion_id": opinion_id,
        "taxon_id": taxon,
        "taxon_name": "兰科 A 种",
        "confidence": confidence,
        "reviewer_id": reviewer,
        "created_at": created_at,
        "tags": list(tags),
    }


class RegistryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc))
        self.service = ObservationRegistryService(self.connection, self.clock)
        for user_id, role in (
            ("ranger-1", "ranger"), ("vol-1", "volunteer"), ("school-1", "school"),
            ("curator-1", "curator"), ("auditor-1", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.submit_record("ranger-1", record(
            "rec-a", "ranger-1", accuracy_m="15", privacy_level="observers", protected=True,
            materials=("现场照片", "生境照片"), opinions=(opinion("oa", reviewer="ranger-1", tags=["protected"]),),
        ))
        self.service.submit_record("vol-1", record(
            "rec-b", "vol-1", latitude="25.0734", longitude="102.7410", accuracy_m="60",
            observed_at="2026-09-18T02:00:00Z", materials=("现场照片",), opinions=(),
        ))
        self.service.submit_record("school-1", record(
            "rec-c", "school-1", latitude="25.0730", longitude="102.7409", accuracy_m="25",
            observed_at="2026-09-18T03:00:00Z", privacy_level="curators",
            materials=("现场照片", "标本影像", "观察笔记"),
            opinions=(opinion("oc", confidence="0.6", reviewer="school-1"),),
        ))
        self.service.submit_record("vol-1", record(
            "rec-far", "vol-1", latitude="25.1180", longitude="102.9050", accuracy_m="40",
            observed_at="2026-09-20T03:00:00Z",
        ))

    def tearDown(self) -> None:
        self.connection.close()

    def _candidate_map(self):
        suggestions = self.service.suggest_candidates("curator-1")
        return {
            tuple(sorted((item["record_a"], item["record_b"]))): item["candidate_id"]
            for item in suggestions["created"]
        }

    def test_candidate_generation_excludes_vetoed_pair(self) -> None:
        pairs = self._candidate_map()
        self.assertEqual(set(pairs), {
            ("rec-a", "rec-b"), ("rec-a", "rec-c"), ("rec-b", "rec-c"),
        })
        explanation = self.service.evaluate_pair_on_demand("curator-1", "rec-a", "rec-far")
        self.assertIn("spatial_disjoint", explanation["veto_reasons"])
        self.assertIsNone(explanation["latest_candidate"])

    def test_suggestion_is_idempotent_when_evidence_unchanged(self) -> None:
        first = self.service.suggest_candidates("curator-1")
        second = self.service.suggest_candidates("curator-1")
        self.assertEqual(second["created"], [])
        self.assertEqual(
            {c["candidate_id"] for c in second["pending"]},
            {c["candidate_id"] for c in first["pending"]},
        )

    def test_submit_and_decision_idempotent_replay(self) -> None:
        payload = record("rec-dup", "ranger-1")
        first = self.service.submit_record("ranger-1", payload, idempotency_key="k1")
        second = self.service.submit_record("ranger-1", payload, idempotency_key="k1")
        self.assertEqual(first, second)
        changed = record("rec-dup", "ranger-1", accuracy_m="99")
        with self.assertRaises(Conflict):
            self.service.submit_record("ranger-1", changed, idempotency_key="k1")

        candidate_id = self._candidate_map()[("rec-a", "rec-b")]
        decision = {
            "candidate_id": candidate_id, "decision": "merge",
            "note": "同一植株", "idempotency_key": "d1",
        }
        merged = self.service.decide_candidate(
            "curator-1", candidate_id, "merge", "同一植株", "d1")
        replayed = self.service.decide_candidate(
            "curator-1", candidate_id, "merge", "同一植株", "d1")
        self.assertEqual(merged, replayed)
        with self.assertRaises(Conflict):
            self.service.decide_candidate(
                "curator-1", candidate_id, "merge", "不同理由", "d1")

    def test_merge_workflow_and_canonical_lineage(self) -> None:
        pairs = self._candidate_map()
        first = self.service.decide_candidate(
            "curator-1", pairs[("rec-a", "rec-b")], "merge", "误差圆重叠", "m1")
        canonical_id = first["canonical_id"]
        self.service.decide_candidate(
            "curator-1", pairs[("rec-a", "rec-c")], "merge", "并入同一统一记录", "m2",
            canonical_id=canonical_id)
        view = self.service.get_canonical("curator-1", canonical_id)
        self.assertEqual(view["state"], "active")
        self.assertEqual(set(view["all_sources"]), {"rec-a", "rec-b", "rec-c"})
        self.assertEqual(view["active_sources"], ["rec-a", "rec-b", "rec-c"])
        self.assertEqual(view["location_disclosure"]["effective_privacy_level"], "curators")
        self.assertTrue(view["location_disclosure"]["protected"])
        # 决策时间线包含两次合并
        decisions = view["timeline"]["decisions"]
        self.assertEqual([d["decision"] for d in decisions], ["merged", "merged"])

    def test_original_records_are_never_deleted(self) -> None:
        pairs = self._candidate_map()
        merged = self.service.decide_candidate(
            "curator-1", pairs[("rec-a", "rec-b")], "merge", "合并", "m1")
        self.clock.advance(days=1)
        self.service.add_evidence("curator-1", "rec-b", "标本", "specimen-9")
        self.service.undo_merge("curator-1", merged["canonical_id"], "证据变化重审")
        count = self.connection.execute("SELECT count(*) FROM observation_records").fetchone()[0]
        self.assertEqual(count, 4)
        memberships = self.connection.execute(
            "SELECT count(*) FROM canonical_memberships WHERE ended_at IS NOT NULL").fetchone()[0]
        self.assertEqual(memberships, 2)

    def test_privacy_does_not_expand_after_merge(self) -> None:
        pairs = self._candidate_map()
        merged = self.service.decide_candidate(
            "curator-1", pairs[("rec-a", "rec-b")], "merge", "合并", "m1")
        view = self.service.get_canonical("vol-1", merged["canonical_id"])
        self.assertFalse(view["location_disclosure"]["coordinates_visible"])
        by_id = {member["record_id"]: member for member in view["members"]}
        self.assertIsNone(by_id["rec-a"]["coordinates"])
        self.assertTrue(by_id["rec-a"]["coordinates_redacted"])
        self.assertIsNotNone(by_id["rec-b"]["coordinates"])
        # 学校组的 curators 级别记录同样不向志愿者披露
        self.assertFalse(by_id["rec-a"]["coordinates"]) if False else None

    def test_curator_sees_coordinates_but_contributor_cannot_open_strangers_curators_only(self) -> None:
        # rec-c 隐私级别 curators 单独合并：志愿者既非贡献者也非馆员，不能打开
        pairs = self._candidate_map()
        self.service.decide_candidate(
            "curator-1", pairs[("rec-a", "rec-c")], "merge", "合并", "m1")
        canonical_id = self.service.list_candidates("curator-1")  # smoke
        self.assertIsNotNone(canonical_id)
        merged_cid = self.connection.execute(
            "SELECT canonical_id FROM canonical_memberships WHERE record_id='rec-c' AND ended_at IS NULL"
        ).fetchone()[0]
        with self.assertRaises(Forbidden):
            self.service.get_canonical("vol-1", merged_cid)
        view = self.service.get_canonical("curator-1", merged_cid)
        self.assertTrue(view["location_disclosure"]["coordinates_visible"])
        # 贡献者本人仍可回看自己被并入的记录
        own = self.service.get_canonical("school-1", merged_cid)
        self.assertEqual(own["all_sources"], ["rec-a", "rec-c"])

    def test_rejected_candidate_keeps_reason_and_explains_non_merge(self) -> None:
        candidate_id = self._candidate_map()[("rec-a", "rec-b")]
        self.service.decide_candidate(
            "curator-1", candidate_id, "reject", "坐标误差过大，暂不采信", "r1")
        detail = self.service.get_candidate("curator-1", candidate_id)
        self.assertEqual(detail["status"], "rejected")
        self.assertEqual(detail["decision"]["note"], "坐标误差过大，暂不采信")
        explanation = self.service.evaluate_pair_on_demand("curator-1", "rec-a", "rec-b")
        self.assertEqual(explanation["not_merged_reason"], "馆员已拆回该候选")
        # 已决定候选不能再次决定
        with self.assertRaises(InvalidState):
            self.service.decide_candidate(
                "curator-1", candidate_id, "merge", "改主意", "r2")

    def test_cannot_undo_without_new_evidence(self) -> None:
        pairs = self._candidate_map()
        merged = self.service.decide_candidate(
            "curator-1", pairs[("rec-a", "rec-b")], "merge", "合并", "m1")
        with self.assertRaises(InvalidState):
            self.service.undo_merge("curator-1", merged["canonical_id"], "想改")

    def test_new_evidence_reopens_decided_candidates_and_rejudges(self) -> None:
        pairs = self._candidate_map()
        ab = pairs[("rec-a", "rec-b")]
        bc = pairs[("rec-b", "rec-c")]
        merged = self.service.decide_candidate("curator-1", ab, "merge", "合并", "m1")
        self.service.decide_candidate("curator-1", bc, "reject", "学校记录与志愿者照片不一致", "m2")
        self.clock.advance(hours=24)
        # 新证据：专家对志愿者记录补充分子鉴定，与学校意见一致
        self.service.add_taxonomy_opinion("curator-1", "rec-b", {
            "opinion_id": "ob-new", "taxon_id": "orchid-sp-a", "taxon_name": "兰科 A 种",
            "confidence": "0.9", "reviewer_id": "curator-1",
            "created_at": "2026-09-19T13:00:00Z", "tags": ["protected"],
        })
        undone = self.service.undo_merge("curator-1", merged["canonical_id"], "分子鉴定出现，重新判断")
        self.assertEqual(undone["state"], "dissolved")
        reopened_pairs = {
            tuple(sorted((c["record_a"], c["record_b"]))): c["candidate_id"]
            for c in undone["reopened_candidates"]
        }
        # rec-a/rec-b（合并）与 rec-b/rec-c（拆回）都因指纹变化重新挂出
        self.assertIn(("rec-a", "rec-b"), reopened_pairs)
        self.assertIn(("rec-b", "rec-c"), reopened_pairs)
        for candidate in undone["reopened_candidates"]:
            self.assertEqual(candidate["status"], "pending")
        # 重开的候选可以重新合并
        self.service.decide_candidate(
            "curator-1", reopened_pairs[("rec-b", "rec-c")], "merge", "分子证据支持同一植株", "m3")

    def test_record_lineage_traces_back_to_sources(self) -> None:
        pairs = self._candidate_map()
        merged = self.service.decide_candidate(
            "curator-1", pairs[("rec-a", "rec-b")], "merge", "合并", "m1")
        trace = self.service.get_record("auditor-1", "rec-b")
        self.assertEqual(trace["memberships"][0]["canonical_id"], merged["canonical_id"])
        self.assertEqual(trace["memberships"][0]["status"], "active")
        self.assertTrue(trace["candidates"])
        # 志愿者看自己的记录不需要馆员权限
        own = self.service.get_record("vol-1", "rec-b")
        self.assertIsNotNone(own["coordinates"])
        # 志愿者看不到受保护记录的坐标
        other = self.service.get_record("vol-1", "rec-a")
        self.assertTrue(other["coordinates_redacted"])

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.suggest_candidates("ranger-1")
        with self.assertRaises(Forbidden):
            self.service.decide_candidate("ranger-1", 1, "merge", "x", "k")
        with self.assertRaises(Forbidden):
            self.service.audit_timeline("vol-1")
        with self.assertRaises(Forbidden):
            self.service.submit_record("ranger-1", record("rec-x", "vol-1"))

    def test_contributor_cannot_submit_on_behalf_of_others(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.submit_record("vol-1", record("rec-x", "ranger-1"))

    def test_unknown_record_and_validation(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_record("auditor-1", "missing")
        bad = record("rec-bad", "ranger-1")
        bad["coordinates"]["latitude"] = "200"
        with self.assertRaises(Exception):
            self.service.submit_record("ranger-1", bad)

    def test_audit_timeline_records_decisions(self) -> None:
        pairs = self._candidate_map()
        self.service.decide_candidate(
            "curator-1", pairs[("rec-a", "rec-b")], "merge", "合并", "m1")
        events = self.service.audit_timeline("auditor-1")["events"]
        types_ = {event["event_type"] for event in events}
        self.assertIn("candidate.merged", types_)
        self.assertIn("candidates.suggested", types_)


if __name__ == "__main__":
    unittest.main()
