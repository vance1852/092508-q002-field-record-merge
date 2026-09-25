"""野外观察上报去重合并流程的离线验收入口。"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from .errors import Forbidden
from .jsonio import iter_jsonl
from .service import FieldObservationService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    reports = list(iter_jsonl(workspace / "fixtures" / "demo_observation_reports.jsonl"))
    with tempfile.TemporaryDirectory(prefix="field-observations-") as temporary:
        database = Path(temporary) / "field-observations.sqlite3"
        connection = connect(database)
        try:
            service = FieldObservationService(connection)
            service.create_user("ranger-1", "护林员", "reporter")
            service.create_user("volunteer-1", "志愿者", "reporter")
            service.create_user("school-1", "学校观察小组", "reporter")
            service.create_user("curator-1", "馆藏馆员", "curator")
            service.create_user("auditor-1", "审计人员", "auditor")
            for raw in reports:
                service.submit_report(raw["reporter_id"], raw)
            candidates = service.list_candidates("curator-1")
            if len(candidates) != 3:
                raise RuntimeError("三条兰科上报应两两形成候选，远处记录不应形成候选")
            by_pair = {(c["left_report_id"], c["right_report_id"]): c for c in candidates}
            first_pair = by_pair[("R-001", "R-002")]
            second_pair = by_pair[("R-001", "R-003")]
            third_pair = by_pair[("R-002", "R-003")]
            first = service.decide(
                "curator-1", first_pair["candidate_id"], "merge",
                "误差圆相交且分类意见一致，判断为同一植株", "merge-r1-r2",
            )
            replay = service.decide(
                "curator-1", first_pair["candidate_id"], "merge",
                "误差圆相交且分类意见一致，判断为同一植株", "merge-r1-r2",
            )
            if replay != first:
                raise RuntimeError("相同决定的重复提交应回放原结果")
            second = service.decide(
                "curator-1", second_pair["candidate_id"], "merge",
                "学校小组记录与馆藏位置一致，并入同一植株", "merge-r1-r3",
            )
            merged = service.get_unified("auditor-1", second["unified_id"])
            if merged["visibility"] != "sensitive" or len(merged["members"]) != 3:
                raise RuntimeError("合并后可见范围必须取成员中最严格级别")
            absorbed = service.get_candidate("auditor-1", third_pair["candidate_id"])
            if absorbed["status"] != "merged" or absorbed["absorbed_by_decision_id"] is None:
                raise RuntimeError("第三条候选应随簇合并自动归入")
            # 新证据：学校小组的记录经复核并非同一物种，候选被重新评估。
            service.add_identification(
                "curator-1", "R-003", "ident-r3-1",
                {"taxon_name": "Bulbophyllum sp.", "confidence": "high", "note": "复核照片实为石豆兰属"},
            )
            reevaluated = service.get_candidate("curator-1", second_pair["candidate_id"])
            if len(reevaluated["evaluations"]) < 2:
                raise RuntimeError("新鉴定意见应触发候选重新评估")
            service.decide(
                "curator-1", second_pair["candidate_id"], "split",
                "新鉴定表明学校记录并非同一物种，拆回合并", "split-r1-r3",
            )
            rejected = service.decide(
                "curator-1", second_pair["candidate_id"], "reject",
                "分类意见更新后不再满足合并条件", "reject-r1-r3",
            )
            restored = service.get_unified("auditor-1", first["unified_id"])
            if restored["status"] != "active" or len(restored["members"]) != 2:
                raise RuntimeError("拆回后原统一观察记录应恢复为两条成员")
            reopened = service.get_candidate("auditor-1", third_pair["candidate_id"])
            if reopened["status"] != "open":
                raise RuntimeError("被吸收的候选应在拆回后重新打开")
            explanation = service.get_candidate("auditor-1", second_pair["candidate_id"])["explanation"]
            if "分类意见更新后不再满足合并条件" not in explanation:
                raise RuntimeError("查询接口应能说明为何没有合并某个候选")
            try:
                service.get_report("volunteer-1", "R-003")
            except Forbidden:
                sensitive_hidden = True
            else:
                sensitive_hidden = False
            if not sensitive_hidden:
                raise RuntimeError("受保护上报的位置不得向无关贡献者披露")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "reports": len(reports),
        "candidates": len(candidates),
        "merged_unified_id": second["unified_id"],
        "restored_unified_id": first["unified_id"],
        "restored_members": [member["report_id"] for member in restored["members"]],
        "restored_visibility": restored["visibility"],
        "idempotent_replay": replay == first,
        "rejected_decision_id": rejected["decision_id"],
        "explanation": explanation,
        "sensitive_hidden": sensitive_hidden,
        "timeline_events": len(restored["timeline"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行野外观察上报去重合并流程的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    import json

    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
