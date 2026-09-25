"""野外观察上报去重合并服务的领域用例。"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .matching import ALGORITHM_VERSION, CANDIDATE_THRESHOLD, ReportView, evaluate_pair, haversine_m
from .models import Identification, ObservationReport
from .permissions import CANDIDATE_STATUSES, DECISION_ACTIONS, READER_ROLES, ROLE_PERMISSIONS, VISIBILITY_LEVELS
from .storage import initialize, transaction


_CENTROID_PLACES = Decimal("0.0000001")
_UNCERTAINTY_PLACES = Decimal("0.001")


class FieldObservationService:
    """在单个 SQLite 连接上提供野外观察上报与合并决策操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

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

    def _require_reader(self, user_id: str) -> sqlite3.Row:
        user = self._user(user_id)
        if user["role"] not in READER_ROLES:
            raise Forbidden(f"角色 {user['role']} 无权查看合并工作区")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 幂等辅助
    # ------------------------------------------------------------------

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

    def _store_idempotent_response(
        self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    # ------------------------------------------------------------------
    # 上报与鉴定
    # ------------------------------------------------------------------

    def _report_row(self, report_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM observation_reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"上报记录不存在: {report_id}")
        return row

    def _report_view(self, row: sqlite3.Row) -> ReportView:
        latest = self.connection.execute(
            "SELECT taxon_name FROM identifications WHERE report_id=? "
            "ORDER BY identified_at DESC, identification_id DESC LIMIT 1",
            (row["report_id"],),
        ).fetchone()
        media = json.loads(row["media_json"])
        return ReportView(
            report_id=row["report_id"],
            effective_taxon=latest["taxon_name"] if latest else row["taxon_name"],
            latitude=Decimal(row["latitude"]),
            longitude=Decimal(row["longitude"]),
            coordinate_uncertainty_m=Decimal(row["coordinate_uncertainty_m"]),
            observed_at=parse_utc(row["observed_at"]),
            media_hashes=frozenset(item["sha256"] for item in media),
        )

    @staticmethod
    def _view_fingerprint(view: ReportView) -> dict[str, Any]:
        return {
            "report_id": view.report_id,
            "effective_taxon": view.effective_taxon,
            "latitude": format(view.latitude, "f"),
            "longitude": format(view.longitude, "f"),
            "coordinate_uncertainty_m": format(view.coordinate_uncertainty_m, "f"),
            "observed_at": utc_text(view.observed_at),
            "media_hashes": sorted(view.media_hashes),
        }

    def _record_evaluation(
        self, left: ReportView, right: ReportView, actor_id: str
    ) -> dict[str, Any] | None:
        evaluation = evaluate_pair(left, right)
        evidence = content_digest([
            ALGORITHM_VERSION,
            self._view_fingerprint(left),
            self._view_fingerprint(right),
        ])
        candidate = self.connection.execute(
            "SELECT candidate_id FROM merge_candidates WHERE left_report_id=? AND right_report_id=?",
            (left.report_id, right.report_id),
        ).fetchone()
        if candidate is None:
            if evaluation.score < CANDIDATE_THRESHOLD:
                return None
            cursor = self.connection.execute(
                "INSERT INTO merge_candidates(left_report_id,right_report_id,status,created_at) "
                "VALUES(?,?,'open',?)",
                (left.report_id, right.report_id, self._now()),
            )
            candidate_id = int(cursor.lastrowid)
            self._audit(
                "candidate", str(candidate_id), "candidate.raised", actor_id,
                {
                    "left_report_id": left.report_id,
                    "right_report_id": right.report_id,
                    "score": format(evaluation.score, "f"),
                    "confidence": evaluation.confidence,
                },
            )
        else:
            candidate_id = candidate["candidate_id"]
            latest = self.connection.execute(
                "SELECT evidence_sha256 FROM candidate_evaluations WHERE candidate_id=? "
                "ORDER BY evaluation_id DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if latest is not None and latest["evidence_sha256"] == evidence:
                return None
        self.connection.execute(
            "INSERT INTO candidate_evaluations(candidate_id,algorithm_version,score,confidence,"
            "reasons_json,evidence_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                candidate_id,
                ALGORITHM_VERSION,
                format(evaluation.score, "f"),
                evaluation.confidence,
                canonical_json(list(evaluation.reasons)),
                evidence,
                self._now(),
            ),
        )
        return {
            "candidate_id": candidate_id,
            "left_report_id": left.report_id,
            "right_report_id": right.report_id,
            "score": format(evaluation.score, "f"),
            "confidence": evaluation.confidence,
        }

    def _evaluate_pairs(self, report_ids: set[str], actor_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM observation_reports ORDER BY report_id"
        ).fetchall()
        views = {row["report_id"]: self._report_view(row) for row in rows}
        ordered = sorted(views)
        made: list[dict[str, Any]] = []
        for index, left_id in enumerate(ordered):
            for right_id in ordered[index + 1:]:
                if left_id not in report_ids and right_id not in report_ids:
                    continue
                recorded = self._record_evaluation(views[left_id], views[right_id], actor_id)
                if recorded is not None:
                    made.append(recorded)
        return made

    def submit_report(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "observation.submit")
        report = ObservationReport.from_dict(raw)
        request_digest = content_digest([raw])
        scope = "observation_report"
        existing = self._idempotent_response(scope, report.report_id, request_digest)
        if existing is not None:
            return existing
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO observation_reports(report_id,reporter_id,taxon_name,latitude,longitude,"
                    "coordinate_uncertainty_m,observed_at,visibility,protected,media_json,note,"
                    "content_sha256,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.report_id,
                        actor_id,
                        report.taxon_name,
                        format(report.latitude, "f"),
                        format(report.longitude, "f"),
                        format(report.coordinate_uncertainty_m, "f"),
                        report.observed_at,
                        report.visibility,
                        1 if report.protected else 0,
                        canonical_json([
                            {"sha256": item.sha256, "captured_at": item.captured_at} for item in report.media
                        ]),
                        report.note,
                        request_digest,
                        self._now(),
                    ),
                )
                candidates = self._evaluate_pairs({report.report_id}, actor_id)
                response = {
                    "report_id": report.report_id,
                    "created": True,
                    "visibility": report.visibility,
                    "candidates": candidates,
                }
                self._store_idempotent_response(scope, report.report_id, request_digest, response)
                self._audit(
                    "report", report.report_id, "report.submitted", actor_id,
                    {
                        "taxon_name": report.taxon_name,
                        "visibility": report.visibility,
                        "protected": report.protected,
                    },
                )
        except sqlite3.IntegrityError as exc:
            replay = self._idempotent_response(scope, report.report_id, request_digest)
            if replay is not None:
                return replay
            raise Conflict(f"上报编号已存在且内容不同: {report.report_id}") from exc
        return response

    def add_identification(
        self, actor_id: str, report_id: str, idempotency_key: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "identification.add")
        self._report_row(report_id)
        identification = Identification.from_dict(raw)
        if not idempotency_key.strip():
            raise ValidationFailed("缺少幂等键")
        request_digest = content_digest([raw])
        scope = f"identification:{report_id}"
        existing = self._idempotent_response(scope, idempotency_key.strip(), request_digest)
        if existing is not None:
            return existing
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO identifications(report_id,taxon_name,confidence,note,identified_by,identified_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        report_id,
                        identification.taxon_name,
                        identification.confidence,
                        identification.note,
                        actor_id,
                        self._now(),
                    ),
                )
                reevaluated = self._evaluate_pairs({report_id}, actor_id)
                response = {
                    "identification_id": int(cursor.lastrowid),
                    "report_id": report_id,
                    "reevaluated": reevaluated,
                }
                self._store_idempotent_response(scope, idempotency_key.strip(), request_digest, response)
                self._audit(
                    "report", report_id, "report.identification_added", actor_id,
                    {
                        "identification_id": response["identification_id"],
                        "taxon_name": identification.taxon_name,
                        "confidence": identification.confidence,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("鉴定意见幂等键并发冲突") from exc
        return response

    # ------------------------------------------------------------------
    # 合并决策
    # ------------------------------------------------------------------

    def _effective_decision(self, candidate_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM merge_decisions WHERE candidate_id=? AND superseded_by_decision_id IS NULL "
            "ORDER BY decision_id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()

    @staticmethod
    def _check_transition(effective: sqlite3.Row | None, action: str) -> None:
        if effective is None:
            if action == "split":
                raise InvalidState("候选尚未合并，不能拆回")
            return
        if effective["action"] == action:
            raise Conflict("相同决定已生效；如需回放请使用原幂等键")
        if effective["action"] == "merge" and action != "split":
            raise InvalidState("候选已合并，请先拆回再重新判断")
        if effective["action"] == "reject" and action != "merge":
            raise InvalidState("候选已判定不合并，只能在新证据下改为合并")

    def _active_unified_for(self, report_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT u.* FROM unified_observations u "
            "JOIN unified_members m ON m.unified_id=u.unified_id AND m.removed_by_decision_id IS NULL "
            "WHERE m.report_id=? AND u.status='active'",
            (report_id,),
        ).fetchone()

    def _derive_unified(self, member_ids: list[str]) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in member_ids)
        rows = self.connection.execute(
            f"SELECT * FROM observation_reports WHERE report_id IN ({placeholders}) ORDER BY report_id",
            tuple(member_ids),
        ).fetchall()
        latitudes = [Decimal(row["latitude"]) for row in rows]
        longitudes = [Decimal(row["longitude"]) for row in rows]
        centroid_latitude = (sum(latitudes) / len(latitudes)).quantize(
            _CENTROID_PLACES, rounding=ROUND_HALF_UP
        )
        centroid_longitude = (sum(longitudes) / len(longitudes)).quantize(
            _CENTROID_PLACES, rounding=ROUND_HALF_UP
        )
        published_uncertainty = max(
            haversine_m(
                centroid_latitude, centroid_longitude,
                Decimal(row["latitude"]), Decimal(row["longitude"]),
            ) + Decimal(row["coordinate_uncertainty_m"])
            for row in rows
        ).quantize(_UNCERTAINTY_PLACES, rounding=ROUND_HALF_UP)
        visibility = max(rows, key=lambda row: VISIBILITY_LEVELS[row["visibility"]])["visibility"]
        latest_identification = self.connection.execute(
            f"SELECT taxon_name FROM identifications WHERE report_id IN ({placeholders}) "
            "ORDER BY identified_at DESC, identification_id DESC LIMIT 1",
            tuple(member_ids),
        ).fetchone()
        if latest_identification is not None:
            representative_taxon = latest_identification["taxon_name"]
        else:
            representative_taxon = min(rows, key=lambda row: (row["submitted_at"], row["report_id"]))["taxon_name"]
        return {
            "visibility": visibility,
            "protected": 1 if any(row["protected"] for row in rows) else 0,
            "representative_taxon": representative_taxon,
            "centroid_latitude": format(centroid_latitude, "f"),
            "centroid_longitude": format(centroid_longitude, "f"),
            "published_uncertainty_m": format(published_uncertainty, "f"),
        }

    def _apply_merge(self, candidate: sqlite3.Row, decision_id: int, actor_id: str) -> int:
        left_id = candidate["left_report_id"]
        right_id = candidate["right_report_id"]
        left_unified = self._active_unified_for(left_id)
        right_unified = self._active_unified_for(right_id)
        if (
            left_unified is not None
            and right_unified is not None
            and left_unified["unified_id"] == right_unified["unified_id"]
        ):
            raise InvalidState("两条记录已在同一统一观察记录中")
        member_ids = {left_id, right_id}
        superseded_ids: list[int] = []
        for unified in (left_unified, right_unified):
            if unified is None:
                continue
            superseded_ids.append(unified["unified_id"])
            rows = self.connection.execute(
                "SELECT report_id FROM unified_members WHERE unified_id=? AND removed_by_decision_id IS NULL",
                (unified["unified_id"],),
            ).fetchall()
            member_ids.update(row["report_id"] for row in rows)
            self.connection.execute(
                "UPDATE unified_members SET removed_by_decision_id=? "
                "WHERE unified_id=? AND removed_by_decision_id IS NULL",
                (decision_id, unified["unified_id"]),
            )
            self.connection.execute(
                "UPDATE unified_observations SET status='superseded',superseded_by_decision_id=?,closed_at=? "
                "WHERE unified_id=?",
                (decision_id, self._now(), unified["unified_id"]),
            )
            self._audit(
                "unified", str(unified["unified_id"]), "unified.superseded", actor_id,
                {"decision_id": decision_id},
            )
        ordered_members = sorted(member_ids)
        derived = self._derive_unified(ordered_members)
        cursor = self.connection.execute(
            "INSERT INTO unified_observations(visibility,protected,representative_taxon,centroid_latitude,"
            "centroid_longitude,published_uncertainty_m,status,created_by_decision_id,created_at) "
            "VALUES(?,?,?,?,?,?, 'active', ?,?)",
            (
                derived["visibility"],
                derived["protected"],
                derived["representative_taxon"],
                derived["centroid_latitude"],
                derived["centroid_longitude"],
                derived["published_uncertainty_m"],
                decision_id,
                self._now(),
            ),
        )
        unified_id = int(cursor.lastrowid)
        for report_id in ordered_members:
            self.connection.execute(
                "INSERT INTO unified_members(unified_id,report_id,added_by_decision_id) VALUES(?,?,?)",
                (unified_id, report_id, decision_id),
            )
        self._audit(
            "unified", str(unified_id), "unified.created", actor_id,
            {
                "decision_id": decision_id,
                "members": ordered_members,
                "superseded_unified_ids": superseded_ids,
                "visibility": derived["visibility"],
                "published_uncertainty_m": derived["published_uncertainty_m"],
            },
        )
        return unified_id

    def _apply_split(self, effective: sqlite3.Row, decision_id: int, actor_id: str) -> None:
        created = self.connection.execute(
            "SELECT * FROM unified_observations WHERE created_by_decision_id=?",
            (effective["decision_id"],),
        ).fetchone()
        if created is None:
            raise InvalidState("合并决定缺少对应的统一观察记录")
        if created["status"] != "active":
            raise InvalidState("该合并已被后续决定取代，请先拆回更新的决定")
        self.connection.execute(
            "UPDATE unified_members SET removed_by_decision_id=? "
            "WHERE unified_id=? AND removed_by_decision_id IS NULL",
            (decision_id, created["unified_id"]),
        )
        self.connection.execute(
            "UPDATE unified_observations SET status='revoked',revoked_by_decision_id=?,closed_at=? "
            "WHERE unified_id=?",
            (decision_id, self._now(), created["unified_id"]),
        )
        self._audit(
            "unified", str(created["unified_id"]), "unified.revoked", actor_id,
            {"decision_id": decision_id},
        )
        restored = self.connection.execute(
            "SELECT unified_id FROM unified_observations WHERE superseded_by_decision_id=?",
            (effective["decision_id"],),
        ).fetchall()
        self.connection.execute(
            "UPDATE unified_members SET removed_by_decision_id=NULL WHERE removed_by_decision_id=?",
            (effective["decision_id"],),
        )
        self.connection.execute(
            "UPDATE unified_observations SET status='active',superseded_by_decision_id=NULL,closed_at=NULL "
            "WHERE superseded_by_decision_id=?",
            (effective["decision_id"],),
        )
        for row in restored:
            self._audit(
                "unified", str(row["unified_id"]), "unified.restored", actor_id,
                {"decision_id": decision_id},
            )

    def _co_members(self, left_report_id: str, right_report_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM unified_members ma "
            "JOIN unified_members mb ON mb.unified_id=ma.unified_id "
            "JOIN unified_observations u ON u.unified_id=ma.unified_id AND u.status='active' "
            "WHERE ma.report_id=? AND mb.report_id=? "
            "AND ma.removed_by_decision_id IS NULL AND mb.removed_by_decision_id IS NULL LIMIT 1",
            (left_report_id, right_report_id),
        ).fetchone()
        return row is not None

    def _sync_candidates(self, decision_id: int, actor_id: str) -> None:
        open_candidates = self.connection.execute(
            "SELECT * FROM merge_candidates WHERE status='open'"
        ).fetchall()
        for candidate in open_candidates:
            if self._co_members(candidate["left_report_id"], candidate["right_report_id"]):
                self.connection.execute(
                    "UPDATE merge_candidates SET status='merged',absorbed_by_decision_id=? "
                    "WHERE candidate_id=?",
                    (decision_id, candidate["candidate_id"]),
                )
                self._audit(
                    "candidate", str(candidate["candidate_id"]), "candidate.absorbed", actor_id,
                    {"decision_id": decision_id},
                )
        absorbed = self.connection.execute(
            "SELECT * FROM merge_candidates WHERE status='merged' AND absorbed_by_decision_id IS NOT NULL"
        ).fetchall()
        for candidate in absorbed:
            if not self._co_members(candidate["left_report_id"], candidate["right_report_id"]):
                self.connection.execute(
                    "UPDATE merge_candidates SET status='open',absorbed_by_decision_id=NULL "
                    "WHERE candidate_id=?",
                    (candidate["candidate_id"],),
                )
                self._audit(
                    "candidate", str(candidate["candidate_id"]), "candidate.reopened", actor_id,
                    {"decision_id": decision_id},
                )

    def decide(
        self,
        actor_id: str,
        candidate_id: int,
        action: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "merge.decide")
        if action not in DECISION_ACTIONS:
            raise ValidationFailed("未知合并决定类型，必须是 merge、reject 或 split")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("决定理由不能为空")
        reason = reason.strip()
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationFailed("缺少幂等键")
        idempotency_key = idempotency_key.strip()
        candidate = self.connection.execute(
            "SELECT * FROM merge_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            raise NotFound("合并候选不存在")
        request_digest = content_digest([{"action": action, "reason": reason}])
        scope = f"merge_decision:{candidate_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        try:
            with transaction(self.connection, immediate=True):
                effective = self._effective_decision(candidate_id)
                self._check_transition(effective, action)
                supersedes = None if effective is None else effective["decision_id"]
                cursor = self.connection.execute(
                    "INSERT INTO merge_decisions(candidate_id,action,reason,decided_by,decided_at,"
                    "supersedes_decision_id) VALUES(?,?,?,?,?,?)",
                    (candidate_id, action, reason, actor_id, self._now(), supersedes),
                )
                decision_id = int(cursor.lastrowid)
                if effective is not None:
                    self.connection.execute(
                        "UPDATE merge_decisions SET superseded_by_decision_id=? WHERE decision_id=?",
                        (decision_id, effective["decision_id"]),
                    )
                unified_id = None
                if action == "merge":
                    unified_id = self._apply_merge(candidate, decision_id, actor_id)
                elif action == "split":
                    self._apply_split(effective, decision_id, actor_id)
                new_status = {"merge": "merged", "reject": "rejected", "split": "open"}[action]
                self.connection.execute(
                    "UPDATE merge_candidates SET status=?,absorbed_by_decision_id=NULL WHERE candidate_id=?",
                    (new_status, candidate_id),
                )
                self._sync_candidates(decision_id, actor_id)
                self._audit(
                    "candidate", str(candidate_id), "candidate.decision_recorded", actor_id,
                    {
                        "decision_id": decision_id,
                        "action": action,
                        "reason": reason,
                        "supersedes_decision_id": supersedes,
                    },
                )
                response = {
                    "decision_id": decision_id,
                    "candidate_id": candidate_id,
                    "action": action,
                    "candidate_status": new_status,
                    "unified_id": unified_id,
                    "supersedes_decision_id": supersedes,
                }
                self._store_idempotent_response(scope, idempotency_key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            replay = self._idempotent_response(scope, idempotency_key, request_digest)
            if replay is not None:
                return replay
            raise Conflict("合并决定并发冲突") from exc
        return response

    # ------------------------------------------------------------------
    # 查询与追溯
    # ------------------------------------------------------------------

    @staticmethod
    def _report_payload(row: sqlite3.Row, *, masked: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "report_id": row["report_id"],
            "reporter_id": row["reporter_id"],
            "taxon_name": row["taxon_name"],
            "observed_at": row["observed_at"],
            "visibility": row["visibility"],
            "protected": bool(row["protected"]),
            "note": row["note"],
            "submitted_at": row["submitted_at"],
            "media": json.loads(row["media_json"]),
        }
        if masked:
            payload["location"] = {"masked": True}
        else:
            payload["location"] = {
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "coordinate_uncertainty_m": row["coordinate_uncertainty_m"],
            }
        return payload

    def _identifications_of(self, report_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM identifications WHERE report_id=? ORDER BY identification_id", (report_id,)
        ).fetchall()
        return [
            {
                "identification_id": row["identification_id"],
                "taxon_name": row["taxon_name"],
                "confidence": row["confidence"],
                "note": row["note"],
                "identified_by": row["identified_by"],
                "identified_at": row["identified_at"],
            }
            for row in rows
        ]

    def get_report(self, actor_id: str, report_id: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        row = self._report_row(report_id)
        owner = row["reporter_id"] == actor_id
        privileged = actor["role"] in READER_ROLES
        if row["visibility"] == "sensitive" and not (owner or privileged):
            raise Forbidden("无权查看该受保护上报记录")
        masked = not (owner or privileged) and row["visibility"] != "public"
        payload = self._report_payload(row, masked=masked)
        if not masked:
            payload["identifications"] = self._identifications_of(report_id)
            candidates = self.connection.execute(
                "SELECT * FROM merge_candidates WHERE left_report_id=? OR right_report_id=? "
                "ORDER BY candidate_id",
                (report_id, report_id),
            ).fetchall()
            payload["candidates"] = [
                {
                    "candidate_id": candidate["candidate_id"],
                    "other_report_id": (
                        candidate["right_report_id"]
                        if candidate["left_report_id"] == report_id
                        else candidate["left_report_id"]
                    ),
                    "status": candidate["status"],
                }
                for candidate in candidates
            ]
            unified = self._active_unified_for(report_id)
            payload["unified"] = (
                None if unified is None
                else {"unified_id": unified["unified_id"], "status": unified["status"]}
            )
        return payload

    def _latest_evaluation(self, candidate_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM candidate_evaluations WHERE candidate_id=? "
            "ORDER BY evaluation_id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()

    def list_candidates(self, actor_id: str, status: str | None = None) -> list[dict[str, Any]]:
        self._require_reader(actor_id)
        if status is not None and status not in CANDIDATE_STATUSES:
            raise ValidationFailed("未知候选状态")
        if status is None:
            rows = self.connection.execute(
                "SELECT * FROM merge_candidates ORDER BY candidate_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM merge_candidates WHERE status=? ORDER BY candidate_id", (status,)
            ).fetchall()
        result = []
        for row in rows:
            latest = self._latest_evaluation(row["candidate_id"])
            result.append({
                "candidate_id": row["candidate_id"],
                "left_report_id": row["left_report_id"],
                "right_report_id": row["right_report_id"],
                "status": row["status"],
                "score": None if latest is None else latest["score"],
                "confidence": None if latest is None else latest["confidence"],
                "created_at": row["created_at"],
            })
        return result

    @staticmethod
    def _explain(candidate: sqlite3.Row, decisions: list[sqlite3.Row]) -> str:
        effective = next(
            (row for row in reversed(decisions) if row["superseded_by_decision_id"] is None), None
        )
        if candidate["status"] == "merged":
            if effective is None and candidate["absorbed_by_decision_id"] is not None:
                return (
                    f"两条记录已随第 {candidate['absorbed_by_decision_id']} 号合并决定"
                    "归入同一统一观察记录，未单独复核"
                )
            if effective is not None:
                return (
                    f"已于 {effective['decided_at']} 由 {effective['decided_by']} 决定合并："
                    f"{effective['reason']}"
                )
        if candidate["status"] == "rejected" and effective is not None:
            return (
                f"未合并：{effective['decided_by']} 于 {effective['decided_at']} "
                f"判定两条记录并非同一对象，理由：{effective['reason']}"
            )
        if effective is not None and effective["action"] == "split":
            return (
                f"此前合并已于 {effective['decided_at']} 由 {effective['decided_by']} 拆回："
                f"{effective['reason']}；候选等待重新判断"
            )
        return "候选尚未由馆员复核，等待合并或不合并决定"

    def get_candidate(self, actor_id: str, candidate_id: int) -> dict[str, Any]:
        self._require_reader(actor_id)
        row = self.connection.execute(
            "SELECT * FROM merge_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise NotFound("合并候选不存在")
        evaluations = self.connection.execute(
            "SELECT * FROM candidate_evaluations WHERE candidate_id=? ORDER BY evaluation_id",
            (candidate_id,),
        ).fetchall()
        decisions = self.connection.execute(
            "SELECT * FROM merge_decisions WHERE candidate_id=? ORDER BY decision_id",
            (candidate_id,),
        ).fetchall()
        latest = evaluations[-1] if evaluations else None
        return {
            "candidate_id": row["candidate_id"],
            "left_report_id": row["left_report_id"],
            "right_report_id": row["right_report_id"],
            "status": row["status"],
            "algorithm_version": ALGORITHM_VERSION,
            "absorbed_by_decision_id": row["absorbed_by_decision_id"],
            "latest_evaluation": None if latest is None else {
                "score": latest["score"],
                "confidence": latest["confidence"],
                "reasons": json.loads(latest["reasons_json"]),
                "created_at": latest["created_at"],
            },
            "evaluations": [
                {
                    "evaluation_id": evaluation["evaluation_id"],
                    "score": evaluation["score"],
                    "confidence": evaluation["confidence"],
                    "created_at": evaluation["created_at"],
                }
                for evaluation in evaluations
            ],
            "decisions": [
                {
                    "decision_id": decision["decision_id"],
                    "action": decision["action"],
                    "reason": decision["reason"],
                    "decided_by": decision["decided_by"],
                    "decided_at": decision["decided_at"],
                    "supersedes_decision_id": decision["supersedes_decision_id"],
                    "superseded_by_decision_id": decision["superseded_by_decision_id"],
                }
                for decision in decisions
            ],
            "explanation": self._explain(row, decisions),
        }

    def reevaluate_candidate(self, actor_id: str, candidate_id: int) -> dict[str, Any]:
        self._require(actor_id, "candidate.evaluate")
        row = self.connection.execute(
            "SELECT * FROM merge_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise NotFound("合并候选不存在")
        with transaction(self.connection, immediate=True):
            left = self._report_view(self._report_row(row["left_report_id"]))
            right = self._report_view(self._report_row(row["right_report_id"]))
            self._record_evaluation(left, right, actor_id)
        return self.get_candidate(actor_id, candidate_id)

    def _unified_row(self, unified_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM unified_observations WHERE unified_id=?", (unified_id,)
        ).fetchone()
        if row is None:
            raise NotFound("统一观察记录不存在")
        return row

    def get_unified(self, actor_id: str, unified_id: int) -> dict[str, Any]:
        actor = self._user(actor_id)
        unified = self._unified_row(unified_id)
        memberships = self.connection.execute(
            "SELECT m.*,r.reporter_id FROM unified_members m "
            "JOIN observation_reports r ON r.report_id=m.report_id "
            "WHERE m.unified_id=? ORDER BY m.report_id",
            (unified_id,),
        ).fetchall()
        privileged = actor["role"] in READER_ROLES
        contributor = any(membership["reporter_id"] == actor_id for membership in memberships)
        if unified["visibility"] == "sensitive" and not (privileged or contributor):
            raise Forbidden("无权查看该受保护统一观察记录")
        location_masked = not (privileged or contributor) and unified["visibility"] != "public"
        if location_masked:
            location: dict[str, Any] = {"masked": True}
        else:
            location = {
                "latitude": unified["centroid_latitude"],
                "longitude": unified["centroid_longitude"],
                "published_uncertainty_m": unified["published_uncertainty_m"],
            }
        members = []
        for membership in memberships:
            report_row = self._report_row(membership["report_id"])
            member_masked = (
                not privileged
                and report_row["reporter_id"] != actor_id
                and report_row["visibility"] != "public"
            )
            entry = self._report_payload(report_row, masked=member_masked)
            entry["membership"] = {
                "added_by_decision_id": membership["added_by_decision_id"],
                "removed_by_decision_id": membership["removed_by_decision_id"],
            }
            if not member_masked:
                entry["identifications"] = self._identifications_of(report_row["report_id"])
            members.append(entry)
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='unified' AND entity_id=? ORDER BY event_id",
            (str(unified_id),),
        ).fetchall()
        return {
            "unified_id": unified["unified_id"],
            "status": unified["status"],
            "visibility": unified["visibility"],
            "protected": bool(unified["protected"]),
            "representative_taxon": unified["representative_taxon"],
            "location": location,
            "members": members,
            "created_by_decision_id": unified["created_by_decision_id"],
            "superseded_by_decision_id": unified["superseded_by_decision_id"],
            "revoked_by_decision_id": unified["revoked_by_decision_id"],
            "created_at": unified["created_at"],
            "closed_at": unified["closed_at"],
            "timeline": [
                {
                    "event_type": event["event_type"],
                    "actor_id": event["actor_id"],
                    "payload": json.loads(event["payload_json"]),
                    "created_at": event["created_at"],
                }
                for event in events
            ],
        }

    def list_unified(self, actor_id: str) -> list[dict[str, Any]]:
        actor = self._user(actor_id)
        if actor["role"] in READER_ROLES:
            rows = self.connection.execute(
                "SELECT * FROM unified_observations ORDER BY unified_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT DISTINCT u.* FROM unified_observations u "
                "JOIN unified_members m ON m.unified_id=u.unified_id "
                "JOIN observation_reports r ON r.report_id=m.report_id "
                "WHERE r.reporter_id=? ORDER BY u.unified_id",
                (actor_id,),
            ).fetchall()
        result = []
        for row in rows:
            member_count = self.connection.execute(
                "SELECT count(*) FROM unified_members WHERE unified_id=? AND removed_by_decision_id IS NULL",
                (row["unified_id"],),
            ).fetchone()[0]
            result.append({
                "unified_id": row["unified_id"],
                "status": row["status"],
                "visibility": row["visibility"],
                "protected": bool(row["protected"]),
                "representative_taxon": row["representative_taxon"],
                "member_count": member_count,
                "created_at": row["created_at"],
            })
        return result

    def list_audit(self, actor_id: str, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        self._require_reader(actor_id)
        rows = self.connection.execute(
            "SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY event_id",
            (entity_type, entity_id),
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
