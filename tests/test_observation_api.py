from __future__ import annotations

import json
import sqlite3
import unittest

from observation_registry.api import JsonApplication
from observation_registry.service import ObservationRegistryService


class ObservationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(ObservationRegistryService(self.connection))
        self._post("/users", {"user_id": "curator-1", "display_name": "馆员", "role": "curator"})
        self._post("/users", {"user_id": "vol-1", "display_name": "志愿者", "role": "volunteer"})

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, headers: dict | None = None):
        body = json.dumps(payload).encode("utf-8")
        return self.app.handle("POST", path, headers or {}, body)

    def _get(self, path: str, headers: dict | None = None):
        return self.app.handle("GET", path, headers or {})

    def _record(self, record_id: str, *, contributor="vol-1", **overrides) -> dict:
        payload = {
            "record_id": record_id,
            "contributor_id": contributor,
            "observer_group": "巡护队",
            "coordinates": {"latitude": "25.0731", "longitude": "102.7408", "accuracy_m": "50"},
            "observed_at": "2026-09-18T00:00:00Z",
            "privacy_level": "public",
            "protected": False,
            "materials": ["现场照片"],
            "taxonomy_opinions": [],
        }
        payload.update(overrides)
        return payload

    def test_health(self) -> None:
        response = self._get("/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_required(self) -> None:
        response = self._post("/observations", self._record("r1"))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_submit_suggest_decide_trace_roundtrip(self) -> None:
        headers = {"X-Actor-Id": "vol-1", "Idempotency-Key": "k1"}
        first = self._post("/observations", self._record("r1"), headers)
        self.assertEqual(first.status, 201)
        replay = self._post("/observations", self._record("r1"), headers)
        self.assertEqual(replay.body, first.body)

        second = self._post(
            "/observations",
            self._record("r2", latitude="25.0733", longitude="102.7409",
                         observed_at="2026-09-18T02:00:00Z"),
            {"X-Actor-Id": "vol-1", "Idempotency-Key": "k2"},
        )
        self.assertEqual(second.status, 201)

        suggestions = self._post("/candidates/suggest", {}, {"X-Actor-Id": "curator-1"})
        self.assertEqual(suggestions.status, 200)
        candidates = suggestions.body["created"]
        self.assertEqual(len(candidates), 1)
        candidate_id = candidates[0]["candidate_id"]

        decision = self._post(
            f"/candidates/{candidate_id}/decision",
            {"decision": "merge", "note": "同一处", "idempotency_key": "d1"},
            {"X-Actor-Id": "curator-1"},
        )
        self.assertEqual(decision.status, 200)
        canonical_id = decision.body["canonical_id"]

        canonical = self._get(f"/canonical/{canonical_id}", {"X-Actor-Id": "curator-1"})
        self.assertEqual(canonical.status, 200)
        self.assertEqual(set(canonical.body["all_sources"]), {"r1", "r2"})

        record_view = self._get("/observations/r1", {"X-Actor-Id": "curator-1"})
        self.assertEqual(record_view.body["memberships"][0]["canonical_id"], canonical_id)

    def test_evaluate_endpoint_explains_non_merge(self) -> None:
        self._post("/observations", self._record("r1"), {"X-Actor-Id": "vol-1"})
        self._post(
            "/observations",
            self._record("r2", coordinates={
                "latitude": "26.0", "longitude": "103.0", "accuracy_m": "10"}),
            {"X-Actor-Id": "vol-1"},
        )
        response = self._post(
            "/candidates/evaluate", {"record_a": "r1", "record_b": "r2"},
            {"X-Actor-Id": "curator-1"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["recommendation"], "no_match")
        self.assertIn("spatial_disjoint", response.body["veto_reasons"])

    def test_volunteer_cannot_decide(self) -> None:
        self._post("/observations", self._record("r1"), {"X-Actor-Id": "vol-1"})
        self._post(
            "/observations", self._record("r2", observed_at="2026-09-18T01:00:00Z"),
            {"X-Actor-Id": "vol-1"},
        )
        suggestions = self._post("/candidates/suggest", {}, {"X-Actor-Id": "curator-1"})
        candidate_id = suggestions.body["created"][0]["candidate_id"]
        response = self._post(
            f"/candidates/{candidate_id}/decision",
            {"decision": "merge", "note": "越权", "idempotency_key": "d1"},
            {"X-Actor-Id": "vol-1"},
        )
        self.assertEqual(response.status, 403)

    def test_route_not_found(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
