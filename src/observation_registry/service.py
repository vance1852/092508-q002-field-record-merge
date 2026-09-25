"""观察记录候选合并登记的领域用例。"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, isoformat, parse_iso
from .contracts import PRIVACY_RANK, ObservationRecord, TaxonomyOpinion, ValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .matching import (
    ALGORITHM_VERSION,
    DEFAULT_TIME_WINDOW_HOURS,
    RecordEvidence,
    OpinionEvidence,
    evaluate_pair,
)
from .storage import initialize, transaction


# 贡献者角色可以上报、补充鉴定意见与新材料；馆员决定合并/拆回；审计只读时间线。
ROLE_PERMISSIONS = {
    "ranger": {"observation.submit", "opinion.add", "evidence.add", "record.read", "canonical.read"},
    "volunteer": {"observation.submit", "opinion.add", "evidence.add", "record.read", "canonical.read"},
    "school": {"observation.submit", "opinion.add", "evidence.add", "record.read", "canonical.read"},
    "curator": {
        "observation.submit", "opinion.add", "evidence.add", "record.read", "canonical.read",
        "candidate.read", "candidate.decide", "audit.read",
    },
    "auditor": {"record.read", "canonical.read", "candidate.read", "audit.read"},
}

class ObservationRegistryService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _store_idempotent(self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    # ------------------------------------------------------------------ 用户

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------ 原始记录上报

    def submit_record(
        self, actor_id: str, raw: Mapping[str, Any], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "observation.submit")
        try:
            record = ObservationRecord.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        if record.contributor_id != actor_id and actor_id not in self._curator_ids():
            raise Forbidden("不能替其他贡献者上报原始记录")
        request_digest = content_digest([raw])
        scope = f"observation_submit:{record.record_id}"
        with transaction(self.connection, immediate=True):
            if idempotency_key:
                existing = self._idempotent_response(scope, idempotency_key, request_digest)
                if existing is not None:
                    return existing
            digest = content_digest([raw])
            try:
                self.connection.execute(
                    "INSERT INTO observation_records(record_id,contributor_id,observer_group,latitude,longitude,"
                    "accuracy_m,observed_at,privacy_level,protected,materials_json,note,content_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.record_id, record.contributor_id, record.observer_group,
                        format(record.coordinates.latitude, "f"), format(record.coordinates.longitude, "f"),
                        format(record.coordinates.accuracy_m, "f"), record.observed_at, record.privacy_level,
                        1 if record.protected else 0, canonical_json(list(record.materials)),
                        record.note, digest, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"观察记录已存在: {record.record_id}") from exc
            for opinion in record.taxonomy_opinions:
                self._insert_opinion(record.record_id, opinion)
            response = {"record_id": record.record_id, "content_sha256": digest, "opinions": len(record.taxonomy_opinions)}
            if idempotency_key:
                self._store_idempotent(scope, idempotency_key, request_digest, response)
            self._audit("observation_record", record.record_id, "record.submitted", actor_id, {
                "observer_group": record.observer_group,
                "privacy_level": record.privacy_level,
                "protected": record.protected,
                "sha256": digest,
            })
        return response

    def _curator_ids(self) -> set[str]:
        return {
            row["user_id"]
            for row in self.connection.execute("SELECT user_id FROM users WHERE role='curator'").fetchall()
        }

    def _insert_opinion(self, record_id: str, opinion: TaxonomyOpinion) -> None:
        digest = content_digest([opinion.as_dict()])
        self.connection.execute(
            "INSERT INTO taxonomy_opinions(opinion_id,record_id,taxon_id,taxon_name,confidence,reviewer_id,"
            "tags_json,content_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                opinion.opinion_id, record_id, opinion.taxon_id, opinion.taxon_name,
                format(opinion.confidence, "f"), opinion.reviewer_id, canonical_json(list(opinion.tags)),
                digest, opinion.created_at,
            ),
        )

    def add_taxonomy_opinion(self, actor_id: str, record_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """补充或追加后续鉴定意见；历史意见不覆盖。"""

        self._require(actor_id, "opinion.add")
        record = self._get_record_row(record_id)
        data = dict(raw)
        data.setdefault("opinion_id", f"{record_id}-opinion-{self.connection.execute('SELECT count(*) FROM taxonomy_opinions WHERE record_id=?', (record_id,)).fetchone()[0] + 1}")
        data.setdefault("reviewer_id", actor_id)
        data.setdefault("created_at", self._now())
        try:
            opinion = TaxonomyOpinion.from_dict(data, "taxonomy_opinion")
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        digest = content_digest([opinion.as_dict()])
        with transaction(self.connection, immediate=True):
            try:
                self._insert_opinion(record_id, opinion)
            except sqlite3.IntegrityError as exc:
                raise Conflict("鉴定意见编号重复") from exc
            self._audit("observation_record", record_id, "opinion.added", actor_id, {
                "opinion_id": opinion.opinion_id,
                "taxon_id": opinion.taxon_id,
                "confidence": format(opinion.confidence, "f"),
            })
        return {"opinion_id": opinion.opinion_id, "record_id": record_id, "content_sha256": digest}

    def add_evidence(
        self, actor_id: str, record_id: str, kind: str, ref: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """登记新证据（补拍照片、标本、分子报告等）；只追加，并使候选可重新判断。"""

        self._require(actor_id, "evidence.add")
        self._get_record_row(record_id)
        if not kind.strip() or not ref.strip():
            raise ValidationFailed("证据类型与引用不能为空")
        digest = content_digest([{"kind": kind, "ref": ref, "record_id": record_id}])
        request_digest = digest
        scope = f"record_evidence:{record_id}"
        with transaction(self.connection, immediate=True):
            if idempotency_key:
                existing = self._idempotent_response(scope, idempotency_key, request_digest)
                if existing is not None:
                    return existing
            try:
                cursor = self.connection.execute(
                    "INSERT INTO record_evidence(record_id,kind,ref,contributed_by,content_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (record_id, kind.strip(), ref.strip(), actor_id, digest, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该证据已经登记过") from exc
            evidence_id = cursor.lastrowid
            response = {"evidence_id": evidence_id, "record_id": record_id, "kind": kind.strip(), "ref": ref.strip()}
            if idempotency_key:
                self._store_idempotent(scope, idempotency_key, request_digest, response)
            self._audit("observation_record", record_id, "evidence.added", actor_id, {
                "evidence_id": evidence_id, "kind": kind.strip(), "ref": ref.strip(),
            })
        return response

    # ------------------------------------------------------------- 证据装配

    def _get_record_row(self, record_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM observation_records WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"观察记录不存在: {record_id}")
        return row

    def _load_evidence(self) -> dict[str, RecordEvidence]:
        rows = self.connection.execute("SELECT * FROM observation_records ORDER BY record_id").fetchall()
        evidence: dict[str, RecordEvidence] = {}
        for row in rows:
            opinions_rows = self.connection.execute(
                "SELECT * FROM taxonomy_opinions WHERE record_id=?", (row["record_id"],)
            ).fetchall()
            evidence_kinds = [
                item[0]
                for item in self.connection.execute(
                    "SELECT DISTINCT kind FROM record_evidence WHERE record_id=?", (row["record_id"],)
                ).fetchall()
            ]
            evidence_count = self.connection.execute(
                "SELECT count(*) FROM record_evidence WHERE record_id=?", (row["record_id"],)
            ).fetchone()[0]
            materials = tuple(sorted(set(json.loads(row["materials_json"])) | set(evidence_kinds)))
            opinions = tuple(
                OpinionEvidence(
                    taxon_id=item["taxon_id"],
                    confidence=Decimal(item["confidence"]),
                    protected="protected" in json.loads(item["tags_json"]),
                )
                for item in opinions_rows
            )
            evidence[row["record_id"]] = RecordEvidence(
                record_id=row["record_id"],
                latitude=Decimal(row["latitude"]),
                longitude=Decimal(row["longitude"]),
                accuracy_m=Decimal(row["accuracy_m"]),
                observed_at=parse_iso(row["observed_at"]),
                materials=materials,
                opinions=opinions,
                protected=bool(row["protected"]),
                opinion_count=len(opinions_rows),
                evidence_count=evidence_count,
            )
        return evidence

    def _active_memberships(self) -> dict[str, str]:
        return {
            row["record_id"]: row["canonical_id"]
            for row in self.connection.execute(
                "SELECT record_id,canonical_id FROM canonical_memberships WHERE ended_at IS NULL"
            ).fetchall()
        }

    # ------------------------------------------------------------- 候选生成

    def suggest_candidates(
        self, actor_id: str, *, time_window_hours: Decimal | str = DEFAULT_TIME_WINDOW_HOURS
    ) -> dict[str, Any]:
        """对全部记录重算候选对；证据指纹不变则不重复建单，已决定的旧单在新证据下重生。"""

        self._require(actor_id, "candidate.read")
        evidence = self._load_evidence()
        record_ids = sorted(evidence)
        active = self._active_memberships()
        created: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        with transaction(self.connection, immediate=True):
            for index, id_a in enumerate(record_ids):
                for id_b in record_ids[index + 1:]:
                    if active.get(id_a) is not None and active.get(id_a) == active.get(id_b):
                        continue  # 已经处在同一统一记录中
                    explanation = evaluate_pair(
                        evidence[id_a], evidence[id_b], time_window_hours=time_window_hours
                    )
                    fingerprint = explanation["fingerprint"]
                    latest = self.connection.execute(
                        "SELECT * FROM merge_candidates WHERE record_a=? AND record_b=? "
                        "ORDER BY candidate_id DESC LIMIT 1",
                        (id_a, id_b),
                    ).fetchone()
                    if latest is not None and latest["evidence_fingerprint"] == fingerprint:
                        if latest["status"] == "pending":
                            pending.append({"candidate_id": latest["candidate_id"], "reason": "unchanged"})
                        continue
                    actionable = explanation["recommendation"] in {"merge", "review"}
                    if latest is not None and latest["status"] == "pending":
                        # 证据已变化但馆员尚未决定：旧单留痕，换发新单（新证据不再支持时不挂单）。
                        self.connection.execute(
                            "UPDATE merge_candidates SET status='superseded' WHERE candidate_id=?",
                            (latest["candidate_id"],),
                        )
                        if not actionable:
                            continue
                        supersedes = latest["candidate_id"]
                        revived = False
                    elif latest is not None and latest["status"] in {"merged", "rejected"}:
                        # 旧决定遇到新证据指纹：无论新建议是什么都重新挂出，供馆员重新判断。
                        supersedes = latest["candidate_id"]
                        revived = True
                    elif latest is not None and latest["status"] == "superseded":
                        if not actionable:
                            continue
                        supersedes = latest["candidate_id"]
                        revived = False
                    else:
                        if not actionable:
                            continue  # 机器明确否决的对不建单；可随时用评估接口查看否决理由
                        supersedes = None
                        revived = False
                    cursor = self.connection.execute(
                        "INSERT INTO merge_candidates(record_a,record_b,evidence_fingerprint,algorithm_version,"
                        "confidence,recommendation,explanation_json,status,created_at,supersedes_candidate_id) "
                        "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                        (
                            id_a, id_b, fingerprint, ALGORITHM_VERSION,
                            explanation["confidence"], explanation["recommendation"],
                            canonical_json(explanation), self._now(), supersedes,
                        ),
                    )
                    candidate_id = cursor.lastrowid
                    if revived:
                        self.connection.execute(
                            "INSERT INTO candidate_revivals(record_a,record_b,old_candidate_id,new_candidate_id,"
                            "reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                            (id_a, id_b, latest["candidate_id"], candidate_id, "new_evidence", actor_id, self._now()),
                        )
                    entry = {
                        "candidate_id": candidate_id,
                        "record_a": id_a,
                        "record_b": id_b,
                        "confidence": explanation["confidence"],
                        "recommendation": explanation["recommendation"],
                        "revived_from": supersedes if revived else None,
                    }
                    created.append(entry)
                    pending.append({"candidate_id": candidate_id, "reason": "revived" if revived else "new"})
            self._audit("candidate_set", "current", "candidates.suggested", actor_id, {
                "created": len(created), "time_window_hours": str(time_window_hours),
            })
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "time_window_hours": str(time_window_hours),
            "created": created,
            "pending": [self.get_candidate(actor_id, item["candidate_id"]) for item in pending],
        }

    def list_candidates(self, actor_id: str, status: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "candidate.read")
        if status is not None and status not in {"pending", "merged", "rejected", "superseded"}:
            raise ValidationFailed("未知候选状态")
        sql = "SELECT candidate_id FROM merge_candidates"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY candidate_id"
        rows = self.connection.execute(sql, params).fetchall()
        return {"candidates": [self._candidate_summary(row["candidate_id"]) for row in rows]}

    def _candidate_summary(self, candidate_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT candidate_id,record_a,record_b,confidence,recommendation,status,decided_by,decided_at,"
            "decision_note,supersedes_candidate_id,algorithm_version FROM merge_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise NotFound("候选不存在")
        return dict(row)

    def get_candidate(self, actor_id: str, candidate_id: int) -> dict[str, Any]:
        """返回候选完整信息，含机器理由与馆员决定（拒绝时可据此回答“为何没有合并”）。"""

        self._require(actor_id, "candidate.read")
        row = self.connection.execute(
            "SELECT * FROM merge_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise NotFound("候选不存在")
        explanation = json.loads(row["explanation_json"])
        revivals = self.connection.execute(
            "SELECT revival_id,old_candidate_id,new_candidate_id,reason,created_by,created_at "
            "FROM candidate_revivals WHERE old_candidate_id=? OR new_candidate_id=? ORDER BY revival_id",
            (candidate_id, candidate_id),
        ).fetchall()
        return {
            "candidate_id": candidate_id,
            "record_a": row["record_a"],
            "record_b": row["record_b"],
            "status": row["status"],
            "confidence": row["confidence"],
            "recommendation": row["recommendation"],
            "algorithm_version": row["algorithm_version"],
            "explanation": explanation,
            "decision": None if row["decided_at"] is None else {
                "decision": row["status"],
                "decided_by": row["decided_by"],
                "decided_at": row["decided_at"],
                "note": row["decision_note"],
            },
            "supersedes_candidate_id": row["supersedes_candidate_id"],
            "revivals": [dict(item) for item in revivals],
        }

    def evaluate_pair_on_demand(
        self, actor_id: str, record_a: str, record_b: str,
        *, time_window_hours: Decimal | str = DEFAULT_TIME_WINDOW_HOURS,
    ) -> dict[str, Any]:
        """按需重算任意两条记录的匹配理由（不落单），用于解释“为何没有合并”。"""

        self._require(actor_id, "candidate.read")
        evidence = self._load_evidence()
        if record_a not in evidence or record_b not in evidence:
            raise NotFound("观察记录不存在")
        if record_a == record_b:
            raise ValidationFailed("必须提供两条不同的记录")
        left, right = sorted((record_a, record_b))
        explanation = evaluate_pair(
            evidence[left], evidence[right], time_window_hours=time_window_hours
        )
        latest = self.connection.execute(
            "SELECT candidate_id,status,decided_by,decided_at,decision_note FROM merge_candidates "
            "WHERE record_a=? AND record_b=? ORDER BY candidate_id DESC LIMIT 1",
            (left, right),
        ).fetchone()
        not_merged_reason: str
        if latest is None:
            not_merged_reason = "机器按现有证据给出否决建议，未生成合并候选"
        elif latest["status"] == "rejected":
            not_merged_reason = "馆员已拆回该候选"
        elif latest["status"] in {"pending", "superseded"}:
            not_merged_reason = "候选等待馆员决定"
        elif latest["status"] == "merged":
            not_merged_reason = "候选已合并"
        else:
            not_merged_reason = latest["status"]
        explanation["latest_candidate"] = None if latest is None else dict(latest)
        explanation["not_merged_reason"] = not_merged_reason
        return explanation

    # ------------------------------------------------------------- 馆员决策
    def decide_candidate(
        self,
        actor_id: str,
        candidate_id: int,
        decision: str,
        note: str,
        idempotency_key: str,
        canonical_id: str | None = None,
    ) -> dict[str, Any]:
        """馆员决定合并或拆回；相同决定内容凭幂等键回放原结果。"""

        self._require(actor_id, "candidate.decide")
        if decision not in {"merge", "reject"}:
            raise ValidationFailed("决定必须是 merge 或 reject")
        if not idempotency_key or not idempotency_key.strip():
            raise ValidationFailed("缺少幂等键")
        request_digest = content_digest([{
            "candidate_id": candidate_id, "decision": decision, "note": note, "canonical_id": canonical_id,
        }])
        scope = f"candidate_decision:{candidate_id}"
        with transaction(self.connection, immediate=True):
            existing = self._idempotent_response(scope, idempotency_key.strip(), request_digest)
            if existing is not None:
                return existing
            row = self.connection.execute(
                "SELECT * FROM merge_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise NotFound("候选不存在")
            if row["status"] != "pending":
                raise InvalidState(f"候选已处于 {row['status']} 状态，不能重复决定；如需改判请等待新证据")
            now = self._now()
            if decision == "reject":
                self.connection.execute(
                    "UPDATE merge_candidates SET status='rejected',decided_by=?,decided_at=?,decision_note=? "
                    "WHERE candidate_id=?",
                    (actor_id, now, note, candidate_id),
                )
                response = {"candidate_id": candidate_id, "decision": "reject", "result": "rejected"}
                self._audit("candidate", str(candidate_id), "candidate.rejected", actor_id, {"note": note})
            else:
                response = self._apply_merge(actor_id, row, note, now, canonical_id)
            self._store_idempotent(scope, idempotency_key.strip(), request_digest, response)
        return response

    def _apply_merge(
        self, actor_id: str, candidate: sqlite3.Row, note: str, now: str, canonical_id: str | None
    ) -> dict[str, Any]:
        id_a, id_b = candidate["record_a"], candidate["record_b"]
        active = self._active_memberships()
        canonical_a = active.get(id_a)
        canonical_b = active.get(id_b)
        if canonical_a is not None and canonical_a == canonical_b:
            raise InvalidState("两条记录已经属于同一统一记录")
        if canonical_a is not None and canonical_b is not None:
            raise InvalidState("两条记录分属不同统一记录，需先撤销其中一段关系后再合并")
        target_canonical = canonical_a or canonical_b
        if target_canonical is None:
            if canonical_id:
                target_canonical = canonical_id
            else:
                sequence = self.connection.execute(
                    "SELECT count(*) FROM canonical_observations"
                ).fetchone()[0] + 1
                target_canonical = f"canonical-{sequence}"
            self.connection.execute(
                "INSERT INTO canonical_observations(canonical_id,state,created_by,created_at) VALUES(?,'active',?,?)",
                (target_canonical, actor_id, now),
            )
        elif canonical_id is not None and canonical_id != target_canonical:
            raise Conflict("其中一条记录已属于另一个统一记录，不能指定新编号")
        joining = [
            record_id
            for record_id, canonical in ((id_a, canonical_a), (id_b, canonical_b))
            if canonical is None
        ]
        for record_id in joining:
            self.connection.execute(
                "INSERT INTO canonical_memberships(canonical_id,record_id,candidate_id,added_by,added_at) "
                "VALUES(?,?,?,?,?)",
                (target_canonical, record_id, candidate["candidate_id"], actor_id, now),
            )
        self.connection.execute(
            "UPDATE merge_candidates SET status='merged',decided_by=?,decided_at=?,decision_note=? "
            "WHERE candidate_id=?",
            (actor_id, now, note, candidate["candidate_id"]),
        )
        disclosure = self._disclosure_for_members(self._active_member_ids(target_canonical))
        self._audit("canonical", target_canonical, "candidate.merged", actor_id, {
            "candidate_id": candidate["candidate_id"],
            "records": [id_a, id_b],
            "joining": list(joining),
            "note": note,
            "effective_privacy_level": disclosure["effective_privacy_level"],
            "protected": disclosure["protected"],
        })
        return {
            "candidate_id": candidate["candidate_id"],
            "decision": "merge",
            "result": "merged",
            "canonical_id": target_canonical,
            "members": self._active_member_ids(target_canonical),
            "effective_privacy_level": disclosure["effective_privacy_level"],
        }

    def _active_member_ids(self, canonical_id: str) -> list[str]:
        return [
            row["record_id"]
            for row in self.connection.execute(
                "SELECT record_id FROM canonical_memberships WHERE canonical_id=? AND ended_at IS NULL "
                "ORDER BY membership_id",
                (canonical_id,),
            ).fetchall()
        ]

    def _disclosure_for_members(self, record_ids: Iterable[str]) -> dict[str, Any]:
        """合并后的可见范围取各来源最严隐私级别；任一来源受保护则降为仅馆员。"""

        record_ids = tuple(record_ids)
        if not record_ids:
            return {"effective_privacy_level": "public", "protected": False}
        placeholders = ",".join("?" for _ in record_ids)
        rows = self.connection.execute(
            f"SELECT privacy_level,protected FROM observation_records WHERE record_id IN ({placeholders})",
            record_ids,
        ).fetchall()
        strictest = max(PRIVACY_RANK[row["privacy_level"]] for row in rows)
        protected = any(bool(row["protected"]) for row in rows) or self._protected_by_opinion(record_ids)
        level = "curators" if protected else ("curators" if strictest >= PRIVACY_RANK["curators"] else
                                             ("observers" if strictest >= PRIVACY_RANK["observers"] else "public"))
        return {"effective_privacy_level": level, "protected": protected}

    def _protected_by_opinion(self, record_ids: tuple[str, ...]) -> bool:
        if not record_ids:
            return False
        placeholders = ",".join("?" for _ in record_ids)
        rows = self.connection.execute(
            f"SELECT tags_json FROM taxonomy_opinions WHERE record_id IN ({placeholders})", record_ids
        ).fetchall()
        return any("protected" in json.loads(row["tags_json"]) for row in rows)

    # ------------------------------------------------------------- 撤销与重判

    def undo_merge(
        self, actor_id: str, canonical_id: str, reason: str, record_ids: tuple[str, ...] | None = None
    ) -> dict[str, Any]:
        """出现新证据后撤销合并关系；旧关系统一终结留痕，受影响候选对自动重新生成。"""

        self._require(actor_id, "candidate.decide")
        canonical = self.connection.execute(
            "SELECT * FROM canonical_observations WHERE canonical_id=?", (canonical_id,)
        ).fetchone()
        if canonical is None:
            raise NotFound("统一记录不存在")
        if canonical["state"] != "active":
            raise InvalidState("统一记录已经解散")
        active_rows = self.connection.execute(
            "SELECT * FROM canonical_memberships WHERE canonical_id=? AND ended_at IS NULL ORDER BY membership_id",
            (canonical_id,),
        ).fetchall()
        if not active_rows:
            raise InvalidState("统一记录没有生效中的成员关系")
        targets = {row["record_id"] for row in active_rows}
        if record_ids is not None:
            requested = set(record_ids)
            missing = requested - targets
            if missing:
                raise ValidationFailed(f"这些记录不是当前成员: {sorted(missing)}")
            if not requested:
                raise ValidationFailed("撤销目标不能为空")
            targets = requested
        # 规则约束：统一记录中任一来源出现晚于关系建立的新证据（材料或鉴定）才允许撤销。
        all_active_ids = {row["record_id"] for row in active_rows}
        fresh = self._records_with_new_evidence(all_active_ids, active_rows)
        if not fresh:
            raise InvalidState("没有发现晚于合并决定的新证据，不能撤销旧关系")
        now = self._now()
        affected_pairs: set[tuple[str, str]] = set()
        with transaction(self.connection, immediate=True):
            for row in active_rows:
                if row["record_id"] not in targets:
                    continue
                self.connection.execute(
                    "UPDATE canonical_memberships SET ended_by=?,ended_at=?,end_reason=? WHERE membership_id=?",
                    (actor_id, now, reason, row["membership_id"]),
                )
            remaining = self._active_member_ids(canonical_id)
            if not remaining:
                self.connection.execute(
                    "UPDATE canonical_observations SET state='dissolved',dissolved_at=? WHERE canonical_id=?",
                    (now, canonical_id),
                )
            self._audit("canonical", canonical_id, "merge.undone", actor_id, {
                "reason": reason,
                "ended_records": sorted(targets),
                "remaining_members": remaining,
                "new_evidence_records": sorted(fresh),
            })
            # 受影响的已合并候选对：按新证据指纹重新挂出待决候选。
            reopened_ids = self._reopen_decided_pairs(actor_id, targets, now)
        reopened = [self.get_candidate(actor_id, item) for item in sorted(reopened_ids)]
        return {
            "canonical_id": canonical_id,
            "state": "dissolved" if not remaining else "active",
            "ended_records": sorted(targets),
            "remaining_members": remaining,
            "reopened_candidates": reopened,
        }

    def _records_with_new_evidence(
        self, targets: set[str], active_rows: list[sqlite3.Row]
    ) -> set[str]:
        added_at = {row["record_id"]: parse_iso(row["added_at"]) for row in active_rows}
        fresh: set[str] = set()
        for record_id in targets:
            threshold = added_at[record_id]
            newer_evidence = self.connection.execute(
                "SELECT created_at FROM record_evidence WHERE record_id=?", (record_id,)
            ).fetchall()
            newer_opinion = self.connection.execute(
                "SELECT created_at FROM taxonomy_opinions WHERE record_id=?", (record_id,)
            ).fetchall()
            if any(parse_iso(row["created_at"]) > threshold for row in newer_evidence) or any(
                parse_iso(row["created_at"]) > threshold for row in newer_opinion
            ):
                fresh.add(record_id)
        return fresh

    def _latest_candidate_id(self, id_a: str, id_b: str) -> int | None:
        row = self.connection.execute(
            "SELECT candidate_id FROM merge_candidates WHERE record_a=? AND record_b=? "
            "ORDER BY candidate_id DESC LIMIT 1",
            (id_a, id_b),
        ).fetchone()
        return None if row is None else row["candidate_id"]

    def _reopen_decided_pairs(
        self, actor_id: str, targets: set[str], now: str
    ) -> set[int]:
        evidence = self._load_evidence()
        merged_rows = self.connection.execute(
            "SELECT record_a,record_b FROM merge_candidates WHERE status IN ('merged','rejected')"
        ).fetchall()
        affected = {
            (row["record_a"], row["record_b"])
            for row in merged_rows
            if row["record_a"] in targets or row["record_b"] in targets
        }
        reopened: set[int] = set()
        for id_a, id_b in sorted(affected):
            explanation = evaluate_pair(evidence[id_a], evidence[id_b])
            latest = self.connection.execute(
                "SELECT * FROM merge_candidates WHERE record_a=? AND record_b=? ORDER BY candidate_id DESC LIMIT 1",
                (id_a, id_b),
            ).fetchone()
            if latest is None or latest["evidence_fingerprint"] == explanation["fingerprint"]:
                continue  # 指纹未变说明新证据不影响该对，不重复挂单
            new_id = self.connection.execute(
                "INSERT INTO merge_candidates(record_a,record_b,evidence_fingerprint,algorithm_version,"
                "confidence,recommendation,explanation_json,status,created_at,supersedes_candidate_id) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                (
                    id_a, id_b, explanation["fingerprint"], ALGORITHM_VERSION,
                    explanation["confidence"], explanation["recommendation"],
                    canonical_json(explanation), now, latest["candidate_id"],
                ),
            ).lastrowid
            self.connection.execute(
                "INSERT INTO candidate_revivals(record_a,record_b,old_candidate_id,new_candidate_id,"
                "reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (id_a, id_b, latest["candidate_id"], new_id, "merge_undone_new_evidence", actor_id, now),
            )
            self._audit("candidate", str(new_id), "candidate.reopened", actor_id, {
                "old_candidate_id": latest["candidate_id"], "records": [id_a, id_b],
            })
            reopened.add(int(new_id))
        return reopened

    # ------------------------------------------------------------- 统一记录查询

    def _can_see_record(self, viewer: sqlite3.Row, record_row: sqlite3.Row) -> bool:
        if viewer["role"] == "curator":
            return True
        if record_row["contributor_id"] == viewer["user_id"]:
            return True  # 贡献者始终可以回看自己的原始记录
        if bool(record_row["protected"]):
            return False
        if record_row["privacy_level"] == "public":
            return True
        if record_row["privacy_level"] == "observers":
            return viewer["role"] in ROLE_PERMISSIONS  # 任何已登记用户
        return False  # curators 级别仅馆员

    def get_canonical(self, actor_id: str, canonical_id: str) -> dict[str, Any]:
        """统一观察记录视图：全部来源（含已拆回）、贡献者可见范围、决策时间线。"""

        viewer = self._require(actor_id, "canonical.read")
        canonical = self.connection.execute(
            "SELECT * FROM canonical_observations WHERE canonical_id=?", (canonical_id,)
        ).fetchone()
        if canonical is None:
            raise NotFound("统一记录不存在")
        memberships = self.connection.execute(
            "SELECT * FROM canonical_memberships WHERE canonical_id=? ORDER BY membership_id", (canonical_id,)
        ).fetchall()
        active_rows = [row for row in memberships if row["ended_at"] is None]
        active_ids = [row["record_id"] for row in active_rows]
        disclosure = self._disclosure_for_members(active_ids)
        active_record_rows = [self._get_record_row(row["record_id"]) for row in active_rows]
        is_source_contributor = any(
            row["contributor_id"] == viewer["user_id"] for row in active_record_rows
        )
        is_historical_contributor = any(
            self._get_record_row(row["record_id"])["contributor_id"] == viewer["user_id"]
            for row in memberships
            if row["ended_at"] is not None
        )
        if active_rows:
            if not self._viewer_meets_level(viewer, disclosure["effective_privacy_level"]) and not is_source_contributor:
                raise Forbidden("统一记录的可见范围高于当前权限，合并不应扩大披露")
        elif viewer["role"] not in {"curator", "auditor"} and not is_historical_contributor:
            raise Forbidden("已解散的统一记录只能由馆员、审计或历史来源贡献者追溯")
        members: list[dict[str, Any]] = []
        visible_coordinates = True
        for membership in memberships:
            record_row = self._get_record_row(membership["record_id"])
            can_see = self._can_see_record(viewer, record_row)
            if membership["ended_at"] is None and not can_see:
                visible_coordinates = False
            members.append(self._member_view(membership, record_row, can_see))
        timeline = self._canonical_timeline(canonical_id, memberships)
        return {
            "canonical_id": canonical_id,
            "state": canonical["state"],
            "created_by": canonical["created_by"],
            "created_at": canonical["created_at"],
            "dissolved_at": canonical["dissolved_at"],
            "active_sources": active_ids,
            "all_sources": [row["record_id"] for row in memberships],
            "location_disclosure": {
                "effective_privacy_level": disclosure["effective_privacy_level"],
                "protected": disclosure["protected"],
                "coordinates_visible": self._viewer_meets_level(viewer, disclosure["effective_privacy_level"]) and visible_coordinates,
                "rule": "合并后位置只按最严隐私级别披露；受保护物种位置仅馆员可见",
            },
            "members": members,
            "timeline": timeline,
        }

    def _viewer_meets_level(self, viewer: sqlite3.Row, level: str) -> bool:
        # 审计角色可以打开记录追溯时间线；具体坐标仍按各来源逐条脱敏。
        if viewer["role"] in {"curator", "auditor"}:
            return True
        if level == "public":
            return True
        if level == "observers":
            return True  # 已登记用户
        return False

    def _member_view(
        self, membership: sqlite3.Row, record_row: sqlite3.Row, can_see: bool
    ) -> dict[str, Any]:
        return {
            "record_id": record_row["record_id"],
            "contributor_id": record_row["contributor_id"],
            "observer_group": record_row["observer_group"],
            "privacy_level": record_row["privacy_level"],
            "protected": bool(record_row["protected"]),
            "observed_at": record_row["observed_at"],
            "coordinates": None if not can_see else {
                "latitude": record_row["latitude"],
                "longitude": record_row["longitude"],
                "accuracy_m": record_row["accuracy_m"],
            },
            "coordinates_redacted": not can_see,
            "membership": {
                "status": "active" if membership["ended_at"] is None else "ended",
                "candidate_id": membership["candidate_id"],
                "added_by": membership["added_by"],
                "added_at": membership["added_at"],
                "ended_by": membership["ended_by"],
                "ended_at": membership["ended_at"],
                "end_reason": membership["end_reason"],
            },
        }

    def _canonical_timeline(
        self, canonical_id: str, memberships: list[sqlite3.Row]
    ) -> list[dict[str, Any]]:
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='canonical' AND entity_id=? ORDER BY event_id",
            (canonical_id,),
        ).fetchall()
        timeline = [
            {
                "kind": row["event_type"],
                "actor_id": row["actor_id"],
                "at": row["created_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in events
        ]
        candidate_ids = sorted({row["candidate_id"] for row in memberships if row["candidate_id"] is not None})
        decisions: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            row = self.connection.execute(
                "SELECT status,decided_by,decided_at,decision_note,record_a,record_b FROM merge_candidates "
                "WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is not None and row["decided_at"] is not None:
                decisions.append({
                    "candidate_id": candidate_id,
                    "decision": row["status"],
                    "records": [row["record_a"], row["record_b"]],
                    "decided_by": row["decided_by"],
                    "decided_at": row["decided_at"],
                    "note": row["decision_note"],
                })
        return {"decisions": decisions, "events": timeline}

    # ------------------------------------------------------------- 来源反向追溯

    def get_record(self, actor_id: str, record_id: str) -> dict[str, Any]:
        """从一条原始记录追溯其所属统一记录、关系时间线和全部候选判断。"""

        viewer = self._require(actor_id, "record.read")
        row = self._get_record_row(record_id)
        can_see = self._can_see_record(viewer, row)
        memberships = self.connection.execute(
            "SELECT * FROM canonical_memberships WHERE record_id=? ORDER BY membership_id", (record_id,)
        ).fetchall()
        candidates = self.connection.execute(
            "SELECT candidate_id,record_a,record_b,status,recommendation,confidence,decided_by,decided_at,"
            "decision_note,supersedes_candidate_id FROM merge_candidates WHERE record_a=? OR record_b=? "
            "ORDER BY candidate_id",
            (record_id, record_id),
        ).fetchall()
        opinions = self.connection.execute(
            "SELECT opinion_id,taxon_id,taxon_name,confidence,reviewer_id,created_at,tags_json "
            "FROM taxonomy_opinions WHERE record_id=? ORDER BY rowid",
            (record_id,),
        ).fetchall()
        evidence = self.connection.execute(
            "SELECT evidence_id,kind,ref,contributed_by,created_at FROM record_evidence WHERE record_id=? ORDER BY evidence_id",
            (record_id,),
        ).fetchall()
        return {
            "record_id": record_id,
            "contributor_id": row["contributor_id"],
            "observer_group": row["observer_group"],
            "privacy_level": row["privacy_level"],
            "protected": bool(row["protected"]),
            "observed_at": row["observed_at"],
            "coordinates": None if not can_see else {
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "accuracy_m": row["accuracy_m"],
            },
            "coordinates_redacted": not can_see,
            "materials": json.loads(row["materials_json"]),
            "note": row["note"],
            "taxonomy_opinions": [
                {
                    "opinion_id": item["opinion_id"], "taxon_id": item["taxon_id"],
                    "taxon_name": item["taxon_name"], "confidence": item["confidence"],
                    "reviewer_id": item["reviewer_id"], "created_at": item["created_at"],
                    "tags": json.loads(item["tags_json"]),
                }
                for item in opinions
            ],
            "evidence": [dict(item) for item in evidence],
            "memberships": [
                {
                    "canonical_id": item["canonical_id"],
                    "status": "active" if item["ended_at"] is None else "ended",
                    "added_by": item["added_by"],
                    "added_at": item["added_at"],
                    "ended_by": item["ended_by"],
                    "ended_at": item["ended_at"],
                    "end_reason": item["end_reason"],
                }
                for item in memberships
            ],
            "candidates": [
                {
                    "candidate_id": item["candidate_id"],
                    "other_record": item["record_b"] if item["record_a"] == record_id else item["record_a"],
                    "status": item["status"],
                    "recommendation": item["recommendation"],
                    "confidence": item["confidence"],
                    "decided_by": item["decided_by"],
                    "decided_at": item["decided_at"],
                    "decision_note": item["decision_note"],
                    "supersedes_candidate_id": item["supersedes_candidate_id"],
                }
                for item in candidates
            ],
        }

    def audit_timeline(self, actor_id: str, entity_type: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        sql = "SELECT entity_type,entity_id,event_type,actor_id,payload_json,created_at FROM audit_events"
        params: tuple[Any, ...] = ()
        if entity_type:
            sql += " WHERE entity_type=?"
            params = (entity_type,)
        sql += " ORDER BY event_id"
        rows = self.connection.execute(sql, params).fetchall()
        return {"events": [
            {
                "entity_type": row["entity_type"], "entity_id": row["entity_id"],
                "event_type": row["event_type"], "actor_id": row["actor_id"],
                "at": row["created_at"], "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]}
