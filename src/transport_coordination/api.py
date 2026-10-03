"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .commute_service import CommuteService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments and segments[0] == "commute" and isinstance(service, CommuteService):
            return _commute_route(service, method, segments, parsed, body, actor_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _commute_route(service: CommuteService, method: str, segments: list[str], parsed,
                   body: dict[str, Any], actor_id: str) -> tuple[int, dict[str, Any]]:
    """分派通勤覆盖决策相关的接口。"""

    query = parse_qs(parsed.query)
    if segments == ["commute", "scenarios"] and method == "POST":
        receipt = service.create_scenario(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if segments == ["commute", "scenarios"] and method == "GET":
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, {"items": service.list_scenarios(site_id)}
    if len(segments) == 3 and segments[:2] == ["commute", "scenarios"] and method == "GET":
        return 200, service.get_scenario(segments[2])
    if segments == ["commute", "plans"] and method == "POST":
        receipt = service.create_plan(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if segments == ["commute", "plans"] and method == "GET":
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, {"items": service.list_plans(site_id, query.get("case_id", [None])[0],
                                                 query.get("status", [None])[0])}
    if len(segments) == 3 and segments[:2] == ["commute", "plans"] and method == "GET":
        return 200, service.get_plan(segments[2])
    if len(segments) == 4 and segments[:2] == ["commute", "plans"] \
            and segments[3] == "submit" and method == "POST":
        receipt = service.submit_plan(actor_id=actor_id, plan_id=segments[2], **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if segments == ["commute", "compare"] and method == "GET":
        base_plan_id = query.get("base_plan_id", [""])[0]
        candidate_plan_id = query.get("candidate_plan_id", [""])[0]
        if not base_plan_id or not candidate_plan_id:
            raise ValidationError("base_plan_id 与 candidate_plan_id 不能为空")
        return 200, service.compare_plans(base_plan_id, candidate_plan_id)
    if segments == ["commute", "reviews"] and method == "POST":
        receipt = service.create_review(actor_id=actor_id, **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    if segments == ["commute", "reviews"] and method == "GET":
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, {"items": service.list_reviews(site_id, query.get("status", [None])[0])}
    if len(segments) == 3 and segments[:2] == ["commute", "reviews"] and method == "GET":
        return 200, service.get_review(segments[2])
    if len(segments) == 4 and segments[:2] == ["commute", "reviews"] \
            and segments[3] == "decision" and method == "POST":
        receipt = service.decide_review(actor_id=actor_id, review_id=segments[2], **body)
        return 200 if receipt.replayed else 201, receipt.__dict__
    return 404, {"error": "route_not_found", "message": "接口不存在"}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动都市圈一小时通勤协同评估服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = CommuteService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
