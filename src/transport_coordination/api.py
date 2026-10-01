"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .assistance import AssistanceService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def _chain_id(path: str) -> str:
    parts = [p for p in urlparse(path).path.split("/") if p]
    return parts[1] if len(parts) > 1 else ""


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None, assistance: AssistanceService | None = None
          ) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    passenger_token = headers.get("X-Passenger-Token", "") or query.get("token", [""])[0]
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
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        if assistance is None:
            return 404, {"error": "route_not_found", "message": "接口不存在"}

        # ---- 值守能力与设备 ----
        if method == "POST" and parsed.path == "/capabilities":
            result = assistance.declare_capability(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and parsed.path == "/equipment-incidents":
            result = assistance.report_equipment_incident(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "POST" and len(parts) == 3 and parts[0] == "equipment-incidents" and parts[2] == "resolve":
            result = assistance.resolve_equipment_incident(actor_id=actor_id, incident_id=parts[1], **body)
            return 200 if result.get("replayed") else 200, result

        # ---- 服务链 ----
        if method == "POST" and parsed.path == "/chains":
            result = assistance.create_chain(actor_id=actor_id or None, **body)
            return 200 if result.get("replayed") else 201, result
        if parts and parts[0] == "chains" and len(parts) >= 2:
            chain_id = parts[1]
            if method == "POST" and len(parts) == 5 and parts[2] == "segments" and parts[4] == "accept":
                result = assistance.accept_segment(actor_id=actor_id, chain_id=chain_id,
                                                   segment_seq=int(parts[3]), **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 5 and parts[2] == "segments" and parts[4] == "start":
                result = assistance.start_segment(actor_id=actor_id, chain_id=chain_id,
                                                  segment_seq=int(parts[3]), **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 5 and parts[2] == "handovers" and parts[4] == "arrive":
                result = assistance.handover_arrive(actor_id=actor_id, chain_id=chain_id,
                                                    boundary_seq=int(parts[3]), **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 5 and parts[2] == "handovers" and parts[4] == "receive":
                result = assistance.handover_receive(actor_id=actor_id, chain_id=chain_id,
                                                     boundary_seq=int(parts[3]), **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 3 and parts[2] == "delay":
                result = assistance.report_delay(actor_id=actor_id, chain_id=chain_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 3 and parts[2] == "rebook":
                result = assistance.rebook(actor_id=actor_id, chain_id=chain_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 3 and parts[2] == "no-show":
                result = assistance.mark_no_show(actor_id=actor_id, chain_id=chain_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 3 and parts[2] == "resume":
                result = assistance.resume_after_no_show(actor_id=actor_id, chain_id=chain_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 3 and parts[2] == "emergency":
                result = assistance.emergency_takeover(actor_id=actor_id, chain_id=chain_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 4 and parts[2] == "emergency" and parts[3] == "owner":
                result = assistance.assign_emergency_owner(actor_id=actor_id, chain_id=chain_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and len(parts) == 4 and parts[2] == "emergency" and parts[3] == "resolve":
                result = assistance.resolve_emergency(actor_id=actor_id, chain_id=chain_id, **body)
                return 200 if result.get("replayed") else 200, result
            if method == "GET" and len(parts) == 2:
                if actor_id:
                    return 200, assistance.coordinator_view(actor_id=actor_id, chain_id=chain_id)
                return 200, assistance.passenger_view(chain_id=chain_id, passenger_token=passenger_token)
            if method == "GET" and len(parts) == 3 and parts[2] == "needs":
                segment_seq = int(query.get("segment_seq", ["0"])[0])
                return 200, assistance.access_segment_needs(actor_id=actor_id, chain_id=chain_id,
                                                            segment_seq=segment_seq)
            if method == "GET" and len(parts) == 3 and parts[2] == "access-log":
                return 200, assistance.access_log(chain_id=chain_id, passenger_token=passenger_token)
            if method == "GET" and len(parts) == 3 and parts[2] == "history":
                if actor_id:
                    return 200, assistance.completed_history(chain_id=chain_id, actor_id=actor_id)
                return 200, assistance.completed_history(chain_id=chain_id,
                                                         passenger_token=passenger_token)

        if method == "GET" and parsed.path == "/duty-board":
            organization_id = query.get("organization_id", [None])[0]
            return 200, assistance.duty_board(actor_id=actor_id, organization_id=organization_id)
        if method == "POST" and parsed.path == "/sweeps":
            return 200, {"created": assistance.sweep_timeouts(), "at": assistance._now()}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    assistance: AssistanceService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", ""),
                                 "X-Passenger-Token": self.headers.get("X-Passenger-Token", "")},
                                self.assistance)
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

    parser = argparse.ArgumentParser(description="启动接续协助协同服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    domain = DomainService(database)
    assistance = AssistanceService(database, domain.clock)
    Handler.service = domain
    Handler.assistance = assistance
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
