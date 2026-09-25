"""无第三方依赖的 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .service import ObservationRegistryService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: ObservationRegistryService) -> None:
        self.service = service
        # ThreadingHTTPServer 共享单连接；串行化请求以保证事务不交错。
        self._lock = threading.Lock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = lambda: self._actor(normalized_headers)

            if method == "POST" and path == "/users":
                result = self.service.create_user(payload["user_id"], payload["display_name"], payload["role"])
                return Response(201, result)

            if method == "POST" and path == "/observations":
                key = normalized_headers.get("idempotency-key", "").strip() or None
                result = self.service.submit_record(actor(), payload, idempotency_key=key)
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "observations" and parts[2] == "opinions":
                result = self.service.add_taxonomy_opinion(actor(), parts[1], payload)
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "observations" and parts[2] == "evidence":
                key = normalized_headers.get("idempotency-key", "").strip() or None
                result = self.service.add_evidence(
                    actor(), parts[1], payload["kind"], payload["ref"], idempotency_key=key
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "observations":
                return Response(200, self.service.get_record(actor(), parts[1]))

            if method == "POST" and path == "/candidates/suggest":
                window = payload.get("time_window_hours", "72")
                return Response(200, self.service.suggest_candidates(actor(), time_window_hours=window))
            if method == "POST" and path == "/candidates/evaluate":
                window = payload.get("time_window_hours", "72")
                return Response(200, self.service.evaluate_pair_on_demand(
                    actor(), payload["record_a"], payload["record_b"], time_window_hours=window,
                ))
            if method == "GET" and path == "/candidates":
                status = query.get("status", [None])[0]
                return Response(200, self.service.list_candidates(actor(), status))
            if method == "GET" and len(parts) == 2 and parts[0] == "candidates":
                return Response(200, self.service.get_candidate(actor(), int(parts[1])))
            if method == "POST" and len(parts) == 3 and parts[0] == "candidates" and parts[2] == "decision":
                result = self.service.decide_candidate(
                    actor(), int(parts[1]), payload["decision"], payload.get("note", ""),
                    payload["idempotency_key"], payload.get("canonical_id"),
                )
                return Response(200, result)

            if method == "GET" and len(parts) == 2 and parts[0] == "canonical":
                return Response(200, self.service.get_canonical(actor(), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "canonical" and parts[2] == "undo":
                record_ids = payload.get("record_ids")
                result = self.service.undo_merge(
                    actor(), parts[1], payload["reason"],
                    None if record_ids is None else tuple(record_ids),
                )
                return Response(200, result)

            if method == "GET" and path == "/audit":
                entity_type = query.get("entity_type", [None])[0]
                return Response(200, self.service.audit_timeline(actor(), entity_type))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ObservationRegistry/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with application._lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动观察记录候选合并登记 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("observation_registry.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(ObservationRegistryService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
