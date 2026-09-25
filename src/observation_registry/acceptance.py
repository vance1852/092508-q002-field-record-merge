"""雨后疑似珍稀兰科记录候选合并的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .errors import InvalidState
from .clock import FrozenClock
from .service import ObservationRegistryService
from .storage import connect, inspect_schema

import json as _json
from datetime import datetime, timezone


def _load(path: Path) -> dict:
    return _json.loads(path.read_text(encoding="utf-8"))


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    ranger_record = _load(fixtures / "demo_observation_ranger.json")
    volunteer_record = _load(fixtures / "demo_observation_volunteer.json")
    school_record = _load(fixtures / "demo_observation_school.json")
    other_record = _load(fixtures / "demo_observation_other_site.json")

    with tempfile.TemporaryDirectory(prefix="observation-registry-") as temporary:
        database = Path(temporary) / "registry.sqlite3"
        connection = connect(database)
        try:
            clock = FrozenClock(datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc))
            service = ObservationRegistryService(connection, clock)
            service.create_user("ranger-li", "护林员老李", "ranger")
            service.create_user("volunteer-wang", "志愿者小王", "volunteer")
            service.create_user("school-chen", "学校观察小组小陈", "school")
            service.create_user("curator-zhao", "馆员赵老师", "curator")
            service.create_user("auditor-sun", "审计孙老师", "auditor")

            # 三方先后上报同一处疑似兰科，精度、时间、隐私级别各不相同。
            service.submit_record("ranger-li", ranger_record, idempotency_key="submit-ranger")
            service.submit_record("volunteer-wang", volunteer_record, idempotency_key="submit-volunteer")
            service.submit_record("school-chen", school_record, idempotency_key="submit-school")
            service.submit_record("volunteer-wang", other_record, idempotency_key="submit-other")

            # 相同提交幂等回放。
            replayed = service.submit_record("ranger-li", ranger_record, idempotency_key="submit-ranger")

            # 馆员让系统重算候选；远处样线记录被地点误差直接否决，不落单。
            suggestions = service.suggest_candidates("curator-zhao")
            created = suggestions["created"]
            pair_keys = {tuple(sorted((item["record_a"], item["record_b"]))) for item in created}
            other_site_pairs = [pair for pair in pair_keys if "obs-other-site-309" in pair]
            if len(created) != 3 or other_site_pairs:
                raise RuntimeError(f"候选生成数量异常: {created}")
            recommendations = {item["recommendation"] for item in created}
            if "merge" not in recommendations:
                raise RuntimeError("缺少高置信度合并建议")

            candidate_ids = {
                tuple(sorted((item["record_a"], item["record_b"]))): item["candidate_id"]
                for item in created
            }
            rv = candidate_ids[tuple(sorted(("obs-ranger-001", "obs-volunteer-014")))]
            rs = candidate_ids[tuple(sorted(("obs-ranger-001", "obs-school-207")))]
            vs = candidate_ids[tuple(sorted(("obs-volunteer-014", "obs-school-207")))]

            # 馆员先合并护林员与志愿者记录，再把学校记录并入同一统一记录。
            merged_first = service.decide_candidate(
                "curator-zhao", rv, "merge", "误差圆重叠、时间同窗，采信同一植株",
                idempotency_key="decide-rv-1",
            )
            canonical_id = merged_first["canonical_id"]
            merged_replay = service.decide_candidate(
                "curator-zhao", rv, "merge", "误差圆重叠、时间同窗，采信同一植株",
                idempotency_key="decide-rv-1",
            )
            if merged_replay != merged_first:
                raise RuntimeError("相同决定没有回放原结果")
            merged_school = service.decide_candidate(
                "curator-zhao", rs, "merge", "学校组材料更完整，鉴定类群一致，并入同一统一记录",
                idempotency_key="decide-rs-1", canonical_id=canonical_id,
            )
            if merged_school["canonical_id"] != canonical_id:
                raise RuntimeError("学校记录应并入已有统一记录")
            # 第三条候选涉及的两条记录已通过其他候选归并，馆员拆回并写明原因。
            rejected = service.decide_candidate(
                "curator-zhao", vs, "reject", "两条记录已分别经其他候选并入同一统一记录，不重复处理",
                idempotency_key="decide-vs-1",
            )

            # 隐私不扩大：受保护的护林员记录使统一记录降为仅馆员，志愿者贡献者只能看自己的坐标。
            canonical_view_volunteer = service.get_canonical("volunteer-wang", canonical_id)
            canonical_view_curator = service.get_canonical("curator-zhao", canonical_id)
            disclosure = canonical_view_curator["location_disclosure"]
            if disclosure["effective_privacy_level"] != "curators" or not disclosure["protected"]:
                raise RuntimeError("受保护物种合并后位置披露级别不正确")
            if not disclosure["coordinates_visible"]:
                raise RuntimeError("馆员应能看到精确坐标")
            if canonical_view_volunteer["location_disclosure"]["coordinates_visible"]:
                raise RuntimeError("合并不应向志愿者扩大受保护位置")
            redacted_members = [m for m in canonical_view_volunteer["members"] if m["coordinates_redacted"]]
            if {m["record_id"] for m in redacted_members} != {"obs-ranger-001", "obs-school-207"}:
                raise RuntimeError("志愿者只能看到自己来源的坐标")

            # 没有新证据时不允许撤销旧关系。
            try:
                service.undo_merge("curator-zhao", canonical_id, "误操作测试")
            except InvalidState:
                pass
            else:
                raise RuntimeError("缺少新证据时不应允许撤销合并")

            # 新证据：馆员对志愿者照片补一条受保护物种的专家确认意见。
            service.add_taxonomy_opinion("curator-zhao", "obs-volunteer-014", {
                "opinion_id": "op-volunteer-expert-1",
                "taxon_id": "orchid-sp-a",
                "taxon_name": "珍稀兰科 A 种（专家复核确认）",
                "confidence": "0.88",
                "created_at": "2026-09-19T09:30:00+08:00",
                "tags": ["protected"],
            })

            # 出现新证据：撤销合并，成员关系全部留痕，受影响候选自动重新挂出。
            undone = service.undo_merge("curator-zhao", canonical_id, "专家新鉴定改变证据，重新组织判断")
            if undone["state"] != "dissolved" or len(undone["ended_records"]) != 3:
                raise RuntimeError("撤销结果不符合预期")
            if not undone["reopened_candidates"]:
                raise RuntimeError("新证据应重新打开候选")
            reopened_pairs = {
                tuple(sorted((item["record_a"], item["record_b"])))
                for item in undone["reopened_candidates"]
            }
            if tuple(sorted(("obs-ranger-001", "obs-school-207"))) in reopened_pairs:
                raise RuntimeError("证据未变化的候选对不应重新挂单")

            # 反向追溯：统一记录的全部来源与完整决策时间线仍可查。
            history = service.get_canonical("auditor-sun", canonical_id)
            if set(history["all_sources"]) != {"obs-ranger-001", "obs-volunteer-014", "obs-school-207"}:
                raise RuntimeError("统一记录必须保留全部原始来源")
            kinds = {event["kind"] for event in history["timeline"]["events"]}
            if "candidate.merged" not in kinds or "merge.undone" not in kinds:
                raise RuntimeError("决策时间线不完整")
            ranger_trace = service.get_record("auditor-sun", "obs-ranger-001")
            if not ranger_trace["memberships"] or ranger_trace["memberships"][0]["status"] != "ended":
                raise RuntimeError("原始记录应能追溯到已终结的合并关系")

            # 解释“为什么没有合并”远处候选：机器按误差圆不相交否决，且从未建单。
            explanation = service.evaluate_pair_on_demand(
                "curator-zhao", "obs-ranger-001", "obs-other-site-309"
            )
            if explanation["recommendation"] != "no_match" or "spatial_disjoint" not in explanation["veto_reasons"]:
                raise RuntimeError("远处记录应被地点因子否决")
            if explanation["latest_candidate"] is not None:
                raise RuntimeError("被否决的记录对不应生成候选单")

            schema = inspect_schema(connection)
        finally:
            connection.close()

    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "submitted_records": 4,
        "idempotent_replay_record_id": replayed["record_id"],
        "candidate_count": len(created),
        "recommendations": sorted(recommendations),
        "canonical_id": canonical_id,
        "effective_privacy_level": disclosure["effective_privacy_level"],
        "rejected_candidate_id": rejected["candidate_id"],
        "reopened_candidate_pairs": [list(pair) for pair in sorted(reopened_pairs)],
        "all_sources_preserved": history["all_sources"],
        "veto_reasons": explanation["veto_reasons"],
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行观察记录候选合并登记的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
