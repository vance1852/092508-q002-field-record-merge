from __future__ import annotations

import json
import sqlite3
import unittest

from field_observations.api import JsonApplication
from field_observations.service import FieldObservationService


def make_report(report_id: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "report_id": report_id,
        "taxon_name": "Cypripedium tibeticum",
        "latitude": "31.04100",
        "longitude": "103.18200",
        "coordinate_uncertainty_m": "5",
        "observed_at": "2026-09-25T06:12:00+08:00",
        "visibility": "public",
        "protected": True,
        "media": [],
        "note": "",
    }
    base.update(overrides)
    return base


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(FieldObservationService(self.connection))
        for user_id, role in (("ranger", "reporter"), ("curator", "curator"), ("auditor", "auditor")):
            self.app.handle(
                "POST", "/users",
                body=json.dumps({"user_id": user_id, "display_name": user_id, "role": role}).encode(),
            )

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict[str, object], actor: str, key: str | None = None):
        headers = {"x-actor-id": actor}
        if key is not None:
            headers["idempotency-key"] = key
        return self.app.handle("POST", path, headers, json.dumps(payload).encode())

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_missing_actor_rejected(self) -> None:
        response = self.app.handle(
            "POST", "/reports", body=json.dumps(make_report("R1")).encode()
        )
        self.assertEqual(response.status, 422)
        self.assertIn("X-Actor-Id", response.body["error"]["message"])

    def test_full_merge_flow_over_http(self) -> None:
        first = self.post("/reports", make_report("R1"), "ranger")
        self.assertEqual(first.status, 201)
        second = self.post(
            "/reports",
            make_report("R2", latitude="31.04108", longitude="103.18212",
                        coordinate_uncertainty_m="30", observed_at="2026-09-25T06:47:00+08:00"),
            "ranger",
        )
        self.assertEqual(second.status, 201)
        self.assertEqual(len(second.body["candidates"]), 1)
        candidate_id = second.body["candidates"][0]["candidate_id"]

        candidates = self.app.handle("GET", "/candidates?status=open", {"x-actor-id": "curator"})
        self.assertEqual(candidates.status, 200)
        self.assertEqual(len(candidates.body["candidates"]), 1)

        decided = self.post(
            f"/candidates/{candidate_id}/decisions",
            {"action": "merge", "reason": "同一植株"}, "curator", key="k1",
        )
        self.assertEqual(decided.status, 201)
        replay = self.post(
            f"/candidates/{candidate_id}/decisions",
            {"action": "merge", "reason": "同一植株"}, "curator", key="k1",
        )
        self.assertEqual(replay.body, decided.body)

        unified_id = decided.body["unified_id"]
        unified = self.app.handle("GET", f"/unified_observations/{unified_id}", {"x-actor-id": "auditor"})
        self.assertEqual(unified.status, 200)
        self.assertEqual(len(unified.body["members"]), 2)

        detail = self.app.handle("GET", f"/candidates/{candidate_id}", {"x-actor-id": "auditor"})
        self.assertEqual(detail.body["status"], "merged")
        self.assertIn("决定合并", detail.body["explanation"])

        report = self.app.handle("GET", "/reports/R1", {"x-actor-id": "ranger"})
        self.assertEqual(report.body["unified"]["unified_id"], unified_id)

    def test_identification_requires_idempotency_key(self) -> None:
        self.post("/reports", make_report("R1"), "ranger")
        response = self.app.handle(
            "POST", "/reports/R1/identifications", {"x-actor-id": "curator"},
            json.dumps({"taxon_name": "Cypripedium sp.", "confidence": "low", "note": ""}).encode(),
        )
        self.assertEqual(response.status, 422)
        self.assertIn("Idempotency-Key", response.body["error"]["message"])

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"x-actor-id": "auditor"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")

    def test_forbidden_error_shape(self) -> None:
        response = self.app.handle("GET", "/candidates", {"x-actor-id": "ranger"})
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
