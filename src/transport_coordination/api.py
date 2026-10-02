"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .relay import RelayService
from .service import DomainService
from .storage import Database


def _path(path: str) -> tuple[str, tuple[str, ...]]:
    parsed = urlparse(path)
    parts = tuple(segment for segment in parsed.path.split("/") if segment)
    return parsed, parts


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed, parts = _path(path)
    actor_id = headers.get("X-Actor-Id", "")
    relay = RelayService(service.database, service.clock)

    def call(func, **kwargs):
        result = func(actor_id=actor_id, **kwargs)
        receipt, response = result
        return 200 if receipt.replayed else 201, {**receipt.__dict__, "result": response}

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

        # -- 接续协助平台 ---------------------------------------------------
        if method == "POST" and parsed.path == "/relay/resources":
            return call(relay.register_resource, **body)
        if method == "POST" and parsed.path == "/relay/agent-grants":
            return call(relay.grant_agent, **body)
        if method == "POST" and parsed.path == "/assistances":
            return call(relay.create_assistance, **body)
        if method == "GET" and len(parts) == 2 and parts[0] == "assistances":
            query = parse_qs(parsed.query)
            version = int(query["version"][0]) if query.get("version") else None
            return 200, relay.get_assistance(actor_id=actor_id, assistance_id=parts[1],
                                            version=version)
        if method == "POST" and len(parts) == 3 and parts[0] == "assistances" \
                and parts[2] == "ticket-change":
            return call(relay.change_ticket, assistance_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "assistances" \
                and parts[2] == "complete":
            return call(relay.confirm_completion, assistance_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "assistances" \
                and parts[2] == "takeover":
            return call(relay.takeover, assistance_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "assistances" \
                and parts[2] == "takeover-resume":
            return call(relay.resume_from_takeover, assistance_id=parts[1], **body)
        if method == "GET" and len(parts) == 3 and parts[0] == "assistances" \
                and parts[2] == "access-history":
            return 200, relay.access_history(actor_id=actor_id, assistance_id=parts[1])
        if method == "POST" and len(parts) == 3 and parts[0] == "legs" and parts[2] == "accept":
            return call(relay.accept_leg, leg_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "legs" and parts[2] == "decline":
            return call(relay.decline_leg, leg_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "legs" and parts[2] == "start":
            return call(relay.start_leg, leg_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "legs" \
                and parts[2] == "assign-resource":
            return call(relay.assign_resource, leg_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "legs" and parts[2] == "no-show":
            return call(relay.report_no_show, leg_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "legs" \
                and parts[2] == "no-show-recover":
            return call(relay.recover_no_show, leg_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "legs" and parts[2] == "delivered":
            return call(relay.report_delivered, leg_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "legs" and parts[2] == "delay":
            return call(relay.report_delay, leg_id=parts[1], **body)
        if method == "GET" and len(parts) == 3 and parts[0] == "legs" and parts[2] == "needs":
            return 200, relay.reveal_leg_needs(actor_id=actor_id, leg_id=parts[1],
                                              **({"reason": body["reason"]} if body.get("reason") else {}))
        if method == "POST" and len(parts) == 3 and parts[0] == "handoffs" and parts[2] == "arrive":
            return call(relay.arrive_handoff, handoff_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "handoffs" and parts[2] == "receive":
            return call(relay.receive_handoff, handoff_id=parts[1], **body)
        if method == "POST" and len(parts) == 3 and parts[0] == "resources" and parts[2] == "failure":
            return call(relay.report_equipment_failure, resource_id=parts[1], **body)
        if method == "POST" and parsed.path == "/timeouts/sweep":
            return 200, relay.sweep_timeouts()
        if method == "POST" and len(parts) == 3 and parts[0] == "escalations" and parts[2] == "resolve":
            return call(relay.resolve_escalation, escalation_id=parts[1], **body)
        if method == "GET" and parsed.path == "/escalations":
            query = parse_qs(parsed.query)
            return 200, relay.list_escalations(
                actor_id=actor_id,
                assistance_id=query.get("assistance_id", [None])[0],
                status=query.get("status", ["open"])[0])
        if method == "GET" and parsed.path == "/board":
            return 200, relay.board(actor_id=actor_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


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

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
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
