"""实现无障碍接续协助的服务链、重排、最小披露与升级规则。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .storage import Database


NEED_CATEGORIES = frozenset({"mobility", "sensory", "medical", "communication", "baggage", "other"})
HEALTH_CATEGORIES = frozenset({"medical"})
VISIBILITY_SCOPES = frozenset({"chain", "leg", "unit"})

LEG_OFFERED = "offered"
LEG_ACCEPTED = "accepted"
LEG_IN_PROGRESS = "in_progress"
LEG_DELIVERED = "delivered"
LEG_COMPLETED = "completed"
LEG_NO_SHOW = "no_show"
LEG_TAKEN_OVER = "taken_over"
LEG_SUPERSEDED = "superseded"

HANDOFF_PENDING = "pending"
HANDOFF_ARRIVED = "arrived"
HANDOFF_COMPLETED = "completed"

VERSION_ACTIVE = "active"
VERSION_SUPERSEDED = "superseded"

TRIP_ACTIVE = "active"
TRIP_COMPLETED = "completed"
TRIP_TAKEOVER = "takeover"

ESC_OPEN = "open"
ESC_RESOLVED = "resolved"

RESOURCE_AVAILABLE = "available"
RESOURCE_LOCKED = "locked"
RESOURCE_FAULTY = "faulty"

NEAR_WINDOW = timedelta(minutes=10)
ESCALATE_WINDOW = timedelta(minutes=10)
EMERGENCY_WINDOW = timedelta(minutes=30)

STAFF_ROLES = ("admin", "coordinator", "operator")
ID_LIKE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"


def parse_time(value: str) -> datetime:
    """解析服务统一使用的带时区 ISO 时间。"""

    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValidationError("时间必须包含时区")
    return parsed


def format_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class RelayService:
    """编排服务链锁定、交接、重排升级与访问披露。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # -- 基础工具 -------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now()

    def _now_text(self) -> str:
        return format_time(self._now())

    def _ident(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not value or any(ch not in ID_LIKE for ch in value) or len(value) > 64:
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, conn, actor_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = dict(row)
        if not actor["active"]:
            raise PermissionDenied("操作者已停用")
        return actor

    def _is_staff(self, actor: dict[str, Any]) -> bool:
        return actor["role"] in ("admin", "coordinator", "operator")

    def _is_coordinator(self, actor: dict[str, Any]) -> bool:
        return actor["role"] in ("admin", "coordinator")

    def _replay_seen(self, conn, request_id: str, payload: dict[str, Any]
                     ) -> tuple[WriteReceipt, dict[str, Any]] | None:
        """若 request_id 已成功处理，直接回放原始回执，绕过已推进的状态守卫。"""

        request_id = self._ident(request_id, "request_id")
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?",
                           (request_id,)).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return (WriteReceipt(request_id, row["resource_type"], row["resource_id"], True),
                json.loads(row["response_json"]))

    def _idem(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
              create: Callable[[], tuple[str, str, dict[str, Any]]]) -> tuple[WriteReceipt, dict[str, Any]]:
        request_id = self._ident(request_id, "request_id")
        payload_hash = digest(payload)
        row = conn.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            receipt = WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
            return receipt, json.loads(row["response_json"])
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now_text()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False), response

    def _audit(self, conn, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(conn, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now_text())

    # -- 值守能力与代理授权 ---------------------------------------------

    def register_resource(self, *, request_id: str, actor_id: str, organization_id: str,
                          site_id: str, kind: str, identifier: str,
                          window_start: str, window_end: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "organization_id": organization_id, "site_id": site_id,
                   "kind": kind, "identifier": identifier,
                   "window_start": window_start, "window_end": window_end}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            if actor["role"] not in ("admin", "operator"):
                raise PermissionDenied("只有运营人员可以登记值守资源")
            site = conn.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            if site["organization_id"] != organization_id:
                raise ValidationError("场所不属于该运营机构")
            if actor["organization_id"] != organization_id and actor["role"] != "admin":
                raise PermissionDenied("不能为其他组织登记资源")
            kind = self._ident(kind, "kind")
            identifier = self._ident(identifier, "identifier")
            start, end = parse_time(window_start), parse_time(window_end)
            if end <= start:
                raise ValidationError("值守窗口结束时间必须晚于开始时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                resource_id = uuid.uuid4().hex
                try:
                    conn.execute(
                        "INSERT INTO assist_resources(resource_id,organization_id,site_id,kind,"
                        "identifier,window_start,window_end,status,locked_leg_id) "
                        "VALUES(?,?,?,?,?,?,?,?,NULL)",
                        (resource_id, organization_id, site_id, kind, identifier,
                         format_time(start), format_time(end), RESOURCE_AVAILABLE),
                    )
                except Exception as exc:
                    raise ConflictError("同一资源标识已经登记") from exc
                self._audit(conn, actor_id=actor_id, action="resource.registered",
                            resource_type="assist_resource", resource_id=resource_id,
                            detail={"organization_id": organization_id, "site_id": site_id,
                                    "kind": kind, "identifier": identifier})
                response = {"resource_id": resource_id, "status": RESOURCE_AVAILABLE}
                return "assist_resource", resource_id, response

            return self._idem(conn, request_id=request_id, action="relay.register_resource",
                              payload=payload, create=create)

    def grant_agent(self, *, request_id: str, actor_id: str, passenger_actor_id: str,
                    agent_actor_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "passenger_actor_id": passenger_actor_id,
                   "agent_actor_id": agent_actor_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            passenger = self._actor(conn, passenger_actor_id)
            agent = self._actor(conn, agent_actor_id)
            if passenger["role"] != "passenger":
                raise ValidationError("被代理人必须是旅客身份")
            if not (actor_id == passenger_actor_id or actor["role"] == "admin"):
                raise PermissionDenied("只能由旅客本人或管理员授予代理权")
            if actor_id == passenger_actor_id and actor["role"] != "passenger":
                raise PermissionDenied("操作者与旅客身份不符")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = conn.execute(
                    "SELECT * FROM representation_grants WHERE passenger_actor_id=? AND agent_actor_id=?",
                    (passenger_actor_id, agent_actor_id),
                ).fetchone()
                if existing and existing["revoked_at"] is None:
                    raise ConflictError("代理授权已经存在")
                if existing:
                    conn.execute(
                        "UPDATE representation_grants SET revoked_at=NULL WHERE passenger_actor_id=? AND agent_actor_id=?",
                        (passenger_actor_id, agent_actor_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO representation_grants(passenger_actor_id,agent_actor_id,granted_at) "
                        "VALUES(?,?,?)",
                        (passenger_actor_id, agent_actor_id, self._now_text()),
                    )
                self._audit(conn, actor_id=actor_id, action="agent.granted",
                            resource_type="assistance", resource_id=passenger_actor_id,
                            detail={"agent_actor_id": agent_actor_id,
                                    "passenger_actor_id": passenger_actor_id})
                return "representation_grant", f"{passenger_actor_id}:{agent_actor_id}", {
                    "passenger_actor_id": passenger_actor_id, "agent_actor_id": agent_actor_id}

            return self._idem(conn, request_id=request_id, action="relay.grant_agent",
                              payload=payload, create=create)

    def _resolve_passenger(self, conn, actor: dict[str, Any], passenger_actor_id: str | None) -> str:
        if passenger_actor_id is None or passenger_actor_id == actor["actor_id"]:
            if actor["role"] != "passenger":
                raise PermissionDenied("非旅客身份必须指定 passenger_actor_id 且具备授权")
            return actor["actor_id"]
        passenger = self._actor(conn, passenger_actor_id)
        if passenger["role"] != "passenger":
            raise ValidationError("被代理人必须是旅客身份")
        if actor["role"] == "passenger":
            raise PermissionDenied("旅客不能代理其他旅客")
        if self._is_coordinator(actor):
            return passenger_actor_id
        grant = conn.execute(
            "SELECT 1 FROM representation_grants WHERE passenger_actor_id=? AND agent_actor_id=? "
            "AND revoked_at IS NULL",
            (passenger_actor_id, actor["actor_id"]),
        ).fetchone()
        if grant is None:
            raise PermissionDenied("缺少旅客的有效代理授权")
        return passenger_actor_id

    # -- 需求与可见范围 -------------------------------------------------

    def _validate_needs(self, conn, raw_needs: Any, leg_count: int,
                        valid_orgs: set[str]) -> list[dict[str, Any]]:
        if not isinstance(raw_needs, list) or not raw_needs:
            raise ValidationError("needs 至少包含一条需求")
        needs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_needs:
            if not isinstance(item, dict):
                raise ValidationError("需求项必须是对象")
            key = self._ident(item.get("need_key", ""), "need_key")
            if key in seen:
                raise ValidationError(f"需求 {key} 重复")
            seen.add(key)
            category = item.get("category", "")
            if category not in NEED_CATEGORIES:
                raise ValidationError(f"需求类别 {category} 不受支持")
            detail = item.get("detail")
            if not isinstance(detail, dict) or not detail:
                raise ValidationError("需求 detail 必须是非空对象")
            visibility = item.get("visibility") or {}
            scope = visibility.get("scope", "chain")
            if scope not in VISIBILITY_SCOPES:
                raise ValidationError("visibility.scope 必须是 chain/leg/unit")
            if category in HEALTH_CATEGORIES and scope == "chain":
                raise PermissionDenied("健康类需求不得设置为全链可见，必须限定到具体段或单位")
            if scope == "leg":
                ordinals = visibility.get("ordinals")
                if not isinstance(ordinals, list) or not ordinals:
                    raise ValidationError("leg 可见范围必须提供 ordinals")
                for ordinal in ordinals:
                    if not isinstance(ordinal, int) or ordinal < 0 or ordinal >= leg_count:
                        raise ValidationError("visibility.ordinals 超出行程段范围")
            elif scope == "unit":
                org_ids = visibility.get("organization_ids")
                if not isinstance(org_ids, list) or not org_ids:
                    raise ValidationError("unit 可见范围必须提供 organization_ids")
                for org_id in org_ids:
                    if org_id not in valid_orgs:
                        raise ValidationError(f"可见单位 {org_id} 不在本行程中")
            needs.append({"need_key": key, "category": category, "detail": detail,
                          "visibility": {"scope": scope,
                                         "ordinals": sorted(set(visibility.get("ordinals", []))),
                                         "organization_ids": sorted(set(visibility.get("organization_ids", [])))}})
        return needs

    def _need_visible(self, need: dict[str, Any], *, ordinal: int, organization_id: str) -> bool:
        visibility = need["visibility"]
        scope = visibility["scope"]
        if scope == "chain":
            return True
        if scope == "leg":
            return ordinal in visibility["ordinals"]
        return organization_id in visibility["organization_ids"]

    # -- 行程建链 -------------------------------------------------------

    def _validate_legs(self, conn, raw_legs: Any, *, check_capacity: bool = True) -> list[dict[str, Any]]:
        if not isinstance(raw_legs, list) or not raw_legs:
            raise ValidationError("legs 至少包含一段行程")
        legs: list[dict[str, Any]] = []
        previous_end: datetime | None = None
        previous_to: str | None = None
        for ordinal, item in enumerate(raw_legs):
            if not isinstance(item, dict):
                raise ValidationError("行程段必须是对象")
            site_id = self._text(item.get("site_id", ""), "site_id", 64)
            site = conn.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError(f"第 {ordinal} 段场所 {site_id} 不存在")
            from_location = self._text(item.get("from_location", ""), "from_location")
            to_location = self._text(item.get("to_location", ""), "to_location")
            start = parse_time(item.get("scheduled_start", ""))
            end = parse_time(item.get("scheduled_end", ""))
            if end <= start:
                raise ValidationError(f"第 {ordinal} 段结束时间必须晚于开始时间")
            if previous_end is not None:
                if start < previous_end:
                    raise ValidationError(f"第 {ordinal} 段开始时间早于上一段结束时间")
                if from_location != previous_to:
                    raise ValidationError(
                        f"第 {ordinal} 段接乘位置必须与上一段交接位置 {previous_to} 一致")
            deadline_text = item.get("acceptance_deadline")
            deadline = parse_time(deadline_text) if deadline_text else start
            if deadline > start:
                raise ValidationError("接单截止时间不能晚于本段开始时间")
            kinds = item.get("required_kinds", [])
            if not isinstance(kinds, list) or not kinds:
                raise ValidationError(f"第 {ordinal} 段必须声明 required_kinds")
            kinds = [self._ident(k, "required_kinds") for k in kinds]
            if check_capacity:
                for kind in sorted(set(kinds)):
                    capable = conn.execute(
                        "SELECT 1 FROM assist_resources WHERE organization_id=? AND site_id=? AND kind=? "
                        "AND status!=? AND window_start<=? AND window_end>=? LIMIT 1",
                        (site["organization_id"], site_id, kind, RESOURCE_FAULTY,
                         format_time(start), format_time(end)),
                    ).fetchone()
                    if capable is None:
                        raise ValidationError(
                            f"第 {ordinal} 段单位 {site['organization_id']} 在该时段缺少 {kind} 值守能力")
            legs.append({"ordinal": ordinal, "site_id": site_id,
                         "organization_id": site["organization_id"],
                         "from_location": from_location, "to_location": to_location,
                         "scheduled_start": format_time(start), "scheduled_end": format_time(end),
                         "acceptance_deadline": format_time(deadline),
                         "required_kinds": kinds})
            previous_end = end
            previous_to = to_location
        return legs

    def create_assistance(self, *, request_id: str, actor_id: str,
                          passenger_actor_id: str | None = None,
                          needs: list[dict[str, Any]] | None = None,
                          legs: list[dict[str, Any]] | None = None,
                          note: str = "") -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "passenger_actor_id": passenger_actor_id,
                   "needs": needs, "legs": legs, "note": note}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            passenger_id = self._resolve_passenger(conn, actor, passenger_actor_id)
            leg_specs = self._validate_legs(conn, legs)
            org_set = {spec["organization_id"] for spec in leg_specs}
            need_specs = self._validate_needs(conn, needs, len(leg_specs), org_set)

            def create() -> tuple[str, str, dict[str, Any]]:
                assistance_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO assistance_requests(assistance_id,passenger_actor_id,agent_actor_id,"
                    "current_version,status,created_at) VALUES(?,?,?,1,?,?)",
                    (assistance_id, passenger_id,
                     actor_id if actor_id != passenger_id and not self._is_coordinator(actor) else None,
                     TRIP_ACTIVE, self._now_text()),
                )
                for need in need_specs:
                    conn.execute(
                        "INSERT INTO need_items(need_id,assistance_id,need_key,category,detail_json,"
                        "visibility_json,created_at) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, assistance_id, need["need_key"], need["category"],
                         canonical_json(need["detail"]), canonical_json(need["visibility"]),
                         self._now_text()),
                    )
                self._insert_version(conn, assistance_id=assistance_id, version=1,
                                     reason="created", created_by=actor_id, leg_specs=leg_specs)
                self._audit(conn, actor_id=actor_id, action="assistance.created",
                            resource_type="assistance", resource_id=assistance_id,
                            detail={"passenger_actor_id": passenger_id, "leg_count": len(leg_specs),
                                    "need_keys": [n["need_key"] for n in need_specs]})
                response = {"assistance_id": assistance_id, "version": 1,
                            "status": TRIP_ACTIVE, "leg_count": len(leg_specs)}
                return "assistance", assistance_id, response

            return self._idem(conn, request_id=request_id, action="relay.create_assistance",
                              payload=payload, create=create)

    def _insert_version(self, conn, *, assistance_id: str, version: int, reason: str,
                        created_by: str, leg_specs: list[dict[str, Any]]) -> None:
        conn.execute(
            "INSERT INTO itinerary_versions(assistance_id,version,reason,created_by,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (assistance_id, version, reason, created_by, VERSION_ACTIVE, self._now_text()),
        )
        for spec in leg_specs:
            conn.execute(
                "INSERT INTO legs(leg_id,assistance_id,version,ordinal,site_id,organization_id,"
                "from_location,to_location,scheduled_start,scheduled_end,acceptance_deadline,"
                "required_kinds_json,state,frozen,source_version,accepted_at,started_at,"
                "delivered_at,completed_at,locked_resource_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (spec.get("leg_id") or uuid.uuid4().hex, assistance_id, version, spec["ordinal"],
                 spec["site_id"], spec["organization_id"], spec["from_location"], spec["to_location"],
                 spec["scheduled_start"], spec["scheduled_end"], spec["acceptance_deadline"],
                 canonical_json(spec["required_kinds"]), spec.get("state", LEG_OFFERED),
                 1 if spec.get("frozen") else 0, spec.get("source_version"),
                 spec.get("accepted_at"), spec.get("started_at"), spec.get("delivered_at"),
                 spec.get("completed_at"), spec.get("locked_resource_id")),
            )
        for ordinal in range(len(leg_specs) - 1):
            current_spec = leg_specs[ordinal]
            next_spec = leg_specs[ordinal + 1]
            conn.execute(
                "INSERT INTO handoffs(handoff_id,assistance_id,version,from_ordinal,to_ordinal,"
                "location,deadline,state,frozen) VALUES(?,?,?,?,?,?,?,?,0)",
                (uuid.uuid4().hex, assistance_id, version, ordinal, ordinal + 1,
                 current_spec["to_location"], next_spec["scheduled_start"], HANDOFF_PENDING),
            )

    # -- 读取辅助 -------------------------------------------------------

    def _assistance(self, conn, assistance_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM assistance_requests WHERE assistance_id=?",
                           (assistance_id,)).fetchone()
        if row is None:
            raise NotFoundError("接续协助单不存在")
        return dict(row)

    def _legs(self, conn, assistance_id: str, version: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM legs WHERE assistance_id=? AND version=? ORDER BY ordinal",
            (assistance_id, version),
        ).fetchall()
        return [dict(row) for row in rows]

    def _leg(self, conn, leg_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM legs WHERE leg_id=?", (leg_id,)).fetchone()
        if row is None:
            raise NotFoundError("行程段不存在")
        leg = dict(row)
        assistance = self._assistance(conn, leg["assistance_id"])
        leg["_current_version"] = assistance["current_version"]
        return leg

    def _handoffs(self, conn, assistance_id: str, version: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM handoffs WHERE assistance_id=? AND version=? ORDER BY from_ordinal",
            (assistance_id, version),
        ).fetchall()
        return [dict(row) for row in rows]

    def _needs(self, conn, assistance_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM need_items WHERE assistance_id=? ORDER BY need_key", (assistance_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail_json"])
            item["visibility"] = json.loads(item["visibility_json"])
            result.append(item)
        return result

    def _open_escalation(self, conn, *, assistance_id: str, version: int, ordinal: int,
                         reason: str, level: int, opened_by: str, note: str = "") -> str:
        existing = conn.execute(
            "SELECT * FROM escalations WHERE assistance_id=? AND version=? AND ordinal=? "
            "AND reason=? AND status=?",
            (assistance_id, version, ordinal, reason, ESC_OPEN),
        ).fetchone()
        if existing:
            if level > existing["level"]:
                conn.execute("UPDATE escalations SET level=?, note=note||? WHERE escalation_id=?",
                             (level, f" 升级至 {level} 级：{note}", existing["escalation_id"]))
                self._audit(conn, actor_id=opened_by, action="escalation.upgraded",
                            resource_type="escalation", resource_id=existing["escalation_id"],
                            detail={"level": level, "reason": reason})
            return existing["escalation_id"]
        escalation_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO escalations(escalation_id,assistance_id,version,ordinal,reason,level,"
            "status,note,opened_by,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (escalation_id, assistance_id, version, ordinal, reason, level, ESC_OPEN, note,
             opened_by, self._now_text()),
        )
        self._audit(conn, actor_id=opened_by, action="escalation.opened",
                    resource_type="escalation", resource_id=escalation_id,
                    detail={"assistance_id": assistance_id, "version": version,
                            "ordinal": ordinal, "reason": reason, "level": level, "note": note})
        return escalation_id

    # -- 接单与资源锁定 -------------------------------------------------

    def _find_resource(self, conn, *, organization_id: str, site_id: str, kind: str,
                       start: str, end: str) -> str | None:
        row = conn.execute(
            "SELECT resource_id FROM assist_resources WHERE organization_id=? AND site_id=? AND kind=? "
            "AND status=? AND window_start<=? AND window_end>=? ORDER BY identifier LIMIT 1",
            (organization_id, site_id, kind, RESOURCE_AVAILABLE, start, end),
        ).fetchone()
        return row["resource_id"] if row else None

    def accept_leg(self, *, request_id: str, actor_id: str, leg_id: str,
                   resource_id: str | None = None) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id, "resource_id": resource_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            self._require_open_current(conn, leg)
            if leg["state"] not in (LEG_OFFERED,):
                raise ConflictError(f"行程段当前状态 {leg['state']} 不能接单")
            if actor["role"] not in ("admin", "operator") or (
                    actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]):
                raise PermissionDenied("只有本段责任单位可以接单")
            now = self._now()
            if parse_time(leg["acceptance_deadline"]) < now:
                self._open_escalation(
                    conn, assistance_id=leg["assistance_id"], version=leg["version"],
                    ordinal=leg["ordinal"], reason="acceptance_timeout", level=2,
                    opened_by=actor_id, note="接单时已超过截止时间")
                raise ConflictError("已超过接单截止时间，已升级协调员重排")
            required = json.loads(leg["required_kinds_json"])
            chosen: list[str] = []
            if resource_id:
                target = conn.execute("SELECT * FROM assist_resources WHERE resource_id=?",
                                      (resource_id,)).fetchone()
                if target is None:
                    raise NotFoundError("资源不存在")
                if target["organization_id"] != leg["organization_id"] or target["site_id"] != leg["site_id"]:
                    raise PermissionDenied("资源不属于本段单位或场所")
                if target["status"] == RESOURCE_FAULTY:
                    raise ConflictError("设备已故障")
                if target["status"] == RESOURCE_LOCKED and target["locked_leg_id"] != leg_id:
                    raise ConflictError("资源已被其他段锁定")
                if target["kind"] not in required:
                    raise ValidationError("该资源类型不是本段所需")
                chosen = [resource_id]
                missing = [k for k in required if k != dict(target)["kind"]]
                for kind in sorted(set(missing)):
                    found = self._find_resource(
                        conn, organization_id=leg["organization_id"], site_id=leg["site_id"],
                        kind=kind, start=leg["scheduled_start"], end=leg["scheduled_end"])
                    if found is None:
                        raise ConflictError(f"本段仍缺少 {kind} 资源，不能锁定服务链")
                    chosen.append(found)
            else:
                for kind in required:
                    found = self._find_resource(
                        conn, organization_id=leg["organization_id"], site_id=leg["site_id"],
                        kind=kind, start=leg["scheduled_start"], end=leg["scheduled_end"])
                    if found is None:
                        self._open_escalation(
                            conn, assistance_id=leg["assistance_id"], version=leg["version"],
                            ordinal=leg["ordinal"], reason="no_capacity", level=2,
                            opened_by=actor_id, note=f"缺少 {kind}")
                        raise ConflictError(f"本段缺少 {kind} 值守能力，已升级协调员")
                    chosen.append(found)
            chosen = sorted(set(chosen))

            def create() -> tuple[str, str, dict[str, Any]]:
                for rid in chosen:
                    conn.execute(
                        "UPDATE assist_resources SET status=?, locked_leg_id=? WHERE resource_id=?",
                        (RESOURCE_LOCKED, leg_id, rid),
                    )
                conn.execute("UPDATE legs SET state=?, accepted_at=?, locked_resource_id=? WHERE leg_id=?",
                             (LEG_ACCEPTED, self._now_text(), chosen[0], leg_id))
                self._log_access(conn, actor=actor, leg=leg, reason="leg_acceptance")
                self._audit(conn, actor_id=actor_id, action="leg.accepted",
                            resource_type="leg", resource_id=leg_id,
                            detail={"assistance_id": leg["assistance_id"], "version": leg["version"],
                                    "ordinal": leg["ordinal"], "resources": chosen})
                return "leg", leg_id, {"leg_id": leg_id, "state": LEG_ACCEPTED,
                                       "locked_resources": chosen}

            return self._idem(conn, request_id=request_id, action="relay.accept_leg",
                              payload=payload, create=create)

    def decline_leg(self, *, request_id: str, actor_id: str, leg_id: str,
                    note: str = "") -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            self._require_open_current(conn, leg)
            if leg["state"] != LEG_OFFERED:
                raise ConflictError("只有待接手段可以退回")
            if actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("只有本段责任单位可以退回")
            if actor["role"] not in ("admin", "operator"):
                raise PermissionDenied("当前角色不能退回首段")

            def create() -> tuple[str, str, dict[str, Any]]:
                escalation_id = self._open_escalation(
                    conn, assistance_id=leg["assistance_id"], version=leg["version"],
                    ordinal=leg["ordinal"], reason="unit_declined", level=2,
                    opened_by=actor_id, note=note)
                return "leg", leg_id, {"leg_id": leg_id, "state": LEG_OFFERED,
                                       "escalation_id": escalation_id}

            return self._idem(conn, request_id=request_id, action="relay.decline_leg",
                              payload=payload, create=create)

    def assign_resource(self, *, request_id: str, actor_id: str, leg_id: str,
                        resource_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id, "resource_id": resource_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            self._require_open_current(conn, leg)
            if leg["state"] not in (LEG_ACCEPTED, LEG_IN_PROGRESS):
                raise ConflictError("只有已接单或进行中的段可以改派设备")
            if actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("只有本段单位可以改派设备")
            resource = conn.execute("SELECT * FROM assist_resources WHERE resource_id=?",
                                    (resource_id,)).fetchone()
            if resource is None:
                raise NotFoundError("资源不存在")
            if resource["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("资源不属于本段单位")
            if resource["kind"] not in json.loads(leg["required_kinds_json"]):
                raise ValidationError("该资源类型不是本段所需")
            if resource["status"] == RESOURCE_FAULTY:
                raise ConflictError("设备已故障")
            if resource["status"] == RESOURCE_LOCKED and resource["locked_leg_id"] != leg_id:
                raise ConflictError("资源已被其他段锁定")

            def create() -> tuple[str, str, dict[str, Any]]:
                if leg["locked_resource_id"]:
                    old = conn.execute(
                        "SELECT status FROM assist_resources WHERE resource_id=?",
                        (leg["locked_resource_id"],)).fetchone()
                    if old["status"] == RESOURCE_FAULTY:
                        conn.execute(
                            "UPDATE assist_resources SET locked_leg_id=NULL WHERE resource_id=?",
                            (leg["locked_resource_id"],))
                    else:
                        conn.execute(
                            "UPDATE assist_resources SET status=?, locked_leg_id=NULL WHERE resource_id=?",
                            (RESOURCE_AVAILABLE, leg["locked_resource_id"]),
                        )
                conn.execute(
                    "UPDATE assist_resources SET status=?, locked_leg_id=? WHERE resource_id=?",
                    (RESOURCE_LOCKED, leg_id, resource_id),
                )
                conn.execute("UPDATE legs SET locked_resource_id=? WHERE leg_id=?",
                             (resource_id, leg_id))
                self._audit(conn, actor_id=actor_id, action="leg.reassigned",
                            resource_type="leg", resource_id=leg_id,
                            detail={"resource_id": resource_id})
                return "leg", leg_id, {"leg_id": leg_id, "locked_resource_id": resource_id}

            return self._idem(conn, request_id=request_id, action="relay.assign_resource",
                              payload=payload, create=create)

    # -- 交接协议 -------------------------------------------------------

    def _require_open_current(self, conn, leg: dict[str, Any]) -> None:
        if leg["version"] != leg["_current_version"]:
            raise ConflictError("该段属于已被重排替代的历史版本")
        assistance = self._assistance(conn, leg["assistance_id"])
        if assistance["status"] == TRIP_TAKEOVER:
            raise ConflictError("服务链处于紧急人工接管状态，单位动作暂停")

    def start_leg(self, *, request_id: str, actor_id: str, leg_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            self._require_open_current(conn, leg)
            if leg["ordinal"] != 0:
                raise ConflictError("只有首段可以直接开始，后续段必须通过交接启动")
            if leg["state"] != LEG_ACCEPTED:
                raise ConflictError(f"首段状态 {leg['state']} 不能开始")
            if actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("只有本段单位可以开始护送")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE legs SET state=?, started_at=COALESCE(started_at,?) WHERE leg_id=?",
                             (LEG_IN_PROGRESS, self._now_text(), leg_id))
                self._log_access(conn, actor=actor, leg=leg, reason="leg_start")
                self._audit(conn, actor_id=actor_id, action="leg.started",
                            resource_type="leg", resource_id=leg_id,
                            detail={"assistance_id": leg["assistance_id"], "ordinal": 0})
                return "leg", leg_id, {"leg_id": leg_id, "state": LEG_IN_PROGRESS}

            return self._idem(conn, request_id=request_id, action="relay.start_leg",
                              payload=payload, create=create)

    def _handoff_between(self, conn, assistance_id: str, version: int,
                         from_ordinal: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM handoffs WHERE assistance_id=? AND version=? AND from_ordinal=?",
            (assistance_id, version, from_ordinal),
        ).fetchone()
        if row is None:
            raise NotFoundError("交接单不存在")
        return dict(row)

    def arrive_handoff(self, *, request_id: str, actor_id: str,
                       handoff_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "handoff_id": handoff_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            row = conn.execute("SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
            if row is None:
                raise NotFoundError("交接单不存在")
            handoff = dict(row)
            assistance = self._assistance(conn, handoff["assistance_id"])
            if handoff["version"] != assistance["current_version"]:
                raise ConflictError("交接单属于历史版本")
            if assistance["status"] == TRIP_TAKEOVER:
                raise ConflictError("紧急人工接管中，交接暂停")
            legs = {leg["ordinal"]: leg for leg in self._legs(conn, handoff["assistance_id"], handoff["version"])}
            outbound = legs[handoff["from_ordinal"]]
            if actor["role"] == "operator" and actor["organization_id"] != outbound["organization_id"]:
                raise PermissionDenied("只有交出方单位可以报告到达交接位置")
            if outbound["state"] != LEG_IN_PROGRESS:
                raise ConflictError("交出段尚未在护送中")
            if handoff["state"] == HANDOFF_COMPLETED:
                raise ConflictError("交接已经完成")

            def create() -> tuple[str, str, dict[str, Any]]:
                stamp = self._now_text()
                state = HANDOFF_ARRIVED if handoff["state"] == HANDOFF_PENDING else handoff["state"]
                conn.execute(
                    "UPDATE handoffs SET state=?, arrival_reported_by=?, arrival_reported_at=?, "
                    "outbound_confirmed_by=?, outbound_confirmed_at=?, passenger_present=1 "
                    "WHERE handoff_id=?",
                    (state, actor_id, stamp, actor_id, stamp, handoff_id),
                )
                self._log_access(conn, actor=actor, leg=outbound, reason="handoff_arrival")
                self._audit(conn, actor_id=actor_id, action="handoff.arrived",
                            resource_type="handoff", resource_id=handoff_id,
                            detail={"assistance_id": handoff["assistance_id"],
                                    "version": handoff["version"],
                                    "from_ordinal": handoff["from_ordinal"],
                                    "location": handoff["location"]})
                return "handoff", handoff_id, {"handoff_id": handoff_id, "state": state}

            return self._idem(conn, request_id=request_id, action="relay.arrive_handoff",
                              payload=payload, create=create)

    def receive_handoff(self, *, request_id: str, actor_id: str,
                        handoff_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "handoff_id": handoff_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            row = conn.execute("SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
            if row is None:
                raise NotFoundError("交接单不存在")
            handoff = dict(row)
            assistance = self._assistance(conn, handoff["assistance_id"])
            if handoff["version"] != assistance["current_version"]:
                raise ConflictError("交接单属于历史版本")
            if assistance["status"] == TRIP_TAKEOVER:
                raise ConflictError("紧急人工接管中，交接暂停")
            legs = {leg["ordinal"]: leg for leg in self._legs(conn, handoff["assistance_id"], handoff["version"])}
            outbound = legs[handoff["from_ordinal"]]
            inbound = legs[handoff["to_ordinal"]]
            if actor["role"] == "operator" and actor["organization_id"] != inbound["organization_id"]:
                raise PermissionDenied("只有接入方单位可以确认接走旅客")
            if handoff["state"] != HANDOFF_ARRIVED:
                raise ConflictError("交出方尚未带旅客到达，接入方不能单方关单")
            if inbound["state"] != LEG_ACCEPTED:
                raise ConflictError(f"接入段状态 {inbound['state']}，必须先接单锁定资源")
            same_org = outbound["organization_id"] == inbound["organization_id"]
            first_actor = handoff["outbound_confirmed_by"] or handoff["arrival_reported_by"]
            if same_org and handoff["inbound_confirmed_by"] is None and actor_id == first_actor:
                raise PermissionDenied("同一交接需要第二名值守人员复诵确认")

            def create() -> tuple[str, str, dict[str, Any]]:
                stamp = self._now_text()
                conn.execute(
                    "UPDATE handoffs SET state=?, inbound_confirmed_by=?, inbound_confirmed_at=? "
                    "WHERE handoff_id=?",
                    (HANDOFF_COMPLETED, actor_id, stamp, handoff_id),
                )
                conn.execute(
                    "UPDATE legs SET state=?, completed_at=? WHERE leg_id=?",
                    (LEG_COMPLETED, stamp, outbound["leg_id"]),
                )
                # 交出方的设备/人员资源在双签后释放，接入段使用自己已锁定的资源。
                conn.execute(
                    "UPDATE assist_resources SET status=?, locked_leg_id=NULL "
                    "WHERE locked_leg_id=? AND status!=?",
                    (RESOURCE_AVAILABLE, outbound["leg_id"], RESOURCE_FAULTY),
                )
                conn.execute(
                    "UPDATE legs SET state=?, started_at=COALESCE(started_at,?) WHERE leg_id=?",
                    (LEG_IN_PROGRESS, stamp, inbound["leg_id"]),
                )
                self._log_access(conn, actor=actor, leg=inbound, reason="handoff_receipt")
                self._audit(conn, actor_id=actor_id, action="handoff.completed",
                            resource_type="handoff", resource_id=handoff_id,
                            detail={"assistance_id": handoff["assistance_id"],
                                    "version": handoff["version"],
                                    "from_ordinal": handoff["from_ordinal"],
                                    "to_ordinal": handoff["to_ordinal"],
                                    "outbound_leg_id": outbound["leg_id"],
                                    "inbound_leg_id": inbound["leg_id"]})
                return "handoff", handoff_id, {"handoff_id": handoff_id, "state": HANDOFF_COMPLETED,
                                               "completed_leg_id": outbound["leg_id"],
                                               "active_leg_id": inbound["leg_id"]}

            return self._idem(conn, request_id=request_id, action="relay.receive_handoff",
                              payload=payload, create=create)

    def report_no_show(self, *, request_id: str, actor_id: str, leg_id: str,
                       note: str = "") -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            self._require_open_current(conn, leg)
            if leg["state"] != LEG_IN_PROGRESS:
                raise ConflictError("只有护送中的段可以报告旅客失约")
            if actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("只有本段单位可以报告失约")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE legs SET state=?, no_show_reported_at=? WHERE leg_id=?",
                             (LEG_NO_SHOW, self._now_text(), leg_id))
                escalation_id = self._open_escalation(
                    conn, assistance_id=leg["assistance_id"], version=leg["version"],
                    ordinal=leg["ordinal"], reason="passenger_no_show", level=2,
                    opened_by=actor_id, note=note)
                return "leg", leg_id, {"leg_id": leg_id, "state": LEG_NO_SHOW,
                                       "escalation_id": escalation_id}

            return self._idem(conn, request_id=request_id, action="relay.report_no_show",
                              payload=payload, create=create)

    def recover_no_show(self, *, request_id: str, actor_id: str,
                        leg_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            if leg["version"] != leg["_current_version"]:
                raise ConflictError("历史版本段需通过重排处理")
            if leg["state"] != LEG_NO_SHOW:
                raise ConflictError("该段未处于失约状态")
            if actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("只有本段单位可以确认旅客已找回")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE legs SET state=? WHERE leg_id=?", (LEG_IN_PROGRESS, leg_id))
                conn.execute(
                    "UPDATE escalations SET status=?, resolved_by=?, resolved_at=?, "
                    "note=note||' '||? WHERE assistance_id=? AND version=? AND ordinal=? "
                    "AND reason='passenger_no_show' AND status=?",
                    (ESC_RESOLVED, actor_id, self._now_text(), "旅客找回，继续护送",
                     leg["assistance_id"], leg["version"], leg["ordinal"], ESC_OPEN),
                )
                self._audit(conn, actor_id=actor_id, action="leg.no_show_recovered",
                            resource_type="leg", resource_id=leg_id,
                            detail={"assistance_id": leg["assistance_id"]})
                return "leg", leg_id, {"leg_id": leg_id, "state": LEG_IN_PROGRESS}

            return self._idem(conn, request_id=request_id, action="relay.recover_no_show",
                              payload=payload, create=create)

    def report_delivered(self, *, request_id: str, actor_id: str,
                         leg_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            self._require_open_current(conn, leg)
            legs = self._legs(conn, leg["assistance_id"], leg["version"])
            if leg["ordinal"] != len(legs) - 1:
                raise ConflictError("只有末段可以报告送达")
            if leg["state"] != LEG_IN_PROGRESS:
                raise ConflictError("末段必须处于护送中")
            if actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("只有末段单位可以报告送达")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE legs SET state=?, delivered_at=? WHERE leg_id=?",
                             (LEG_DELIVERED, self._now_text(), leg_id))
                self._audit(conn, actor_id=actor_id, action="leg.delivered",
                            resource_type="leg", resource_id=leg_id,
                            detail={"assistance_id": leg["assistance_id"]})
                return "leg", leg_id, {"leg_id": leg_id, "state": LEG_DELIVERED}

            return self._idem(conn, request_id=request_id, action="relay.report_delivered",
                              payload=payload, create=create)

    def confirm_completion(self, *, request_id: str, actor_id: str,
                           assistance_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "assistance_id": assistance_id}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            assistance = self._assistance(conn, assistance_id)
            if assistance["status"] != TRIP_ACTIVE:
                raise ConflictError("协助单当前状态不能确认完成")
            legs = self._legs(conn, assistance_id, assistance["current_version"])
            last = legs[-1]
            if last["state"] != LEG_DELIVERED:
                raise ConflictError("末段单位尚未报告送达，不能关单")
            if not (self._is_coordinator(actor) or actor_id == assistance["passenger_actor_id"]
                    or actor_id == assistance["agent_actor_id"]):
                raise PermissionDenied("送达确认必须由旅客、代理或协调员完成")

            def create() -> tuple[str, str, dict[str, Any]]:
                stamp = self._now_text()
                conn.execute("UPDATE legs SET state=?, completed_at=? WHERE leg_id=?",
                             (LEG_COMPLETED, stamp, last["leg_id"]))
                conn.execute(
                    "UPDATE assistance_requests SET status=? WHERE assistance_id=?",
                    (TRIP_COMPLETED, assistance_id),
                )
                for resource in conn.execute(
                        "SELECT resource_id FROM assist_resources WHERE locked_leg_id IN "
                        "(SELECT leg_id FROM legs WHERE assistance_id=? AND version=?)",
                        (assistance_id, assistance["current_version"])):
                    conn.execute(
                        "UPDATE assist_resources SET status=?, locked_leg_id=NULL WHERE resource_id=?",
                        (RESOURCE_AVAILABLE, resource["resource_id"]),
                    )
                self._audit(conn, actor_id=actor_id, action="assistance.completed",
                            resource_type="assistance", resource_id=assistance_id,
                            detail={"version": assistance["current_version"]})
                return "assistance", assistance_id, {"assistance_id": assistance_id,
                                                      "status": TRIP_COMPLETED}

            return self._idem(conn, request_id=request_id, action="relay.confirm_completion",
                              payload=payload, create=create)

    # -- 重排引擎 -------------------------------------------------------

    def _revise(self, conn, *, assistance_id: str, actor_id: str, reason: str,
                new_specs: list[dict[str, Any]], check_capacity: bool = True) -> dict[str, Any]:
        assistance = self._assistance(conn, assistance_id)
        if assistance["status"] not in (TRIP_ACTIVE, TRIP_TAKEOVER):
            raise ConflictError("协助单当前状态不允许重排")
        old_version = assistance["current_version"]
        old_legs = {leg["ordinal"]: leg for leg in self._legs(conn, assistance_id, old_version)}
        new_specs = self._validate_legs(conn, new_specs, check_capacity=check_capacity)
        new_version = old_version + 1

        for spec in new_specs:
            old = old_legs.get(spec["ordinal"])
            same_route = bool(old) and old["organization_id"] == spec["organization_id"] and \
                old["from_location"] == spec["from_location"] and old["to_location"] == spec["to_location"]
            locked_status = None
            if old and old["locked_resource_id"]:
                status_row = conn.execute(
                    "SELECT status FROM assist_resources WHERE resource_id=?",
                    (old["locked_resource_id"],)).fetchone()
                locked_status = status_row["status"] if status_row else None
            if old and old["state"] == LEG_COMPLETED:
                if not same_route:
                    raise ConflictError(
                        f"第 {spec['ordinal']} 段已完成，改线不能改变已完成段的原始记录")
                spec["state"] = LEG_COMPLETED
                spec["frozen"] = True
                spec["source_version"] = old_version
                spec["scheduled_start"] = old["scheduled_start"]
                spec["scheduled_end"] = old["scheduled_end"]
                spec["acceptance_deadline"] = old["acceptance_deadline"]
                spec["accepted_at"] = old["accepted_at"]
                spec["started_at"] = old["started_at"]
                spec["completed_at"] = old["completed_at"]
                spec["locked_resource_id"] = old["locked_resource_id"]
            elif old and old["state"] == LEG_IN_PROGRESS and same_route:
                # 护送人仍在旅客身边：段继续进行；设备故障则清空锁，等待改派替代设备。
                spec["state"] = LEG_IN_PROGRESS
                spec["source_version"] = old_version
                spec["accepted_at"] = old["accepted_at"]
                spec["started_at"] = old["started_at"]
                spec["locked_resource_id"] = (
                    old["locked_resource_id"] if locked_status != RESOURCE_FAULTY else None)
            elif old and old["state"] == LEG_DELIVERED and same_route:
                spec["state"] = LEG_DELIVERED
                spec["source_version"] = old_version
                spec["locked_resource_id"] = old["locked_resource_id"]
            else:
                # 时刻或线路变化的待接/已接手段回到待接单，按新版本重新锁定资源。
                spec["state"] = LEG_OFFERED

        self._insert_version(conn, assistance_id=assistance_id, version=new_version,
                             reason=reason, created_by=actor_id, leg_specs=new_specs)
        new_legs = {leg["ordinal"]: leg for leg in self._legs(conn, assistance_id, new_version)}

        # 进行中/已送达段的资源锁随段迁移到新版本；其余段的锁释放（故障设备保持故障）。
        carried_ordinals = {spec["ordinal"] for spec in new_specs
                            if spec["state"] in (LEG_IN_PROGRESS, LEG_DELIVERED)}
        old_leg_by_id = {leg["leg_id"]: leg for leg in old_legs.values()}
        old_leg_ids = list(old_leg_by_id)
        if old_leg_ids:
            placeholders = ",".join("?" for _ in old_leg_ids)
            locked_rows = conn.execute(
                f"SELECT resource_id,status,locked_leg_id FROM assist_resources "
                f"WHERE locked_leg_id IN ({placeholders})", old_leg_ids,
            ).fetchall()
            for row in locked_rows:
                rid, status, locked_leg_id = row["resource_id"], row["status"], row["locked_leg_id"]
                owner_ordinal = old_leg_by_id[locked_leg_id]["ordinal"]
                if owner_ordinal in carried_ordinals and status != RESOURCE_FAULTY:
                    conn.execute("UPDATE assist_resources SET locked_leg_id=? WHERE resource_id=?",
                                 (new_legs[owner_ordinal]["leg_id"], rid))
                elif status == RESOURCE_FAULTY:
                    conn.execute("UPDATE assist_resources SET locked_leg_id=NULL WHERE resource_id=?",
                                 (rid,))
                else:
                    conn.execute(
                        "UPDATE assist_resources SET status=?, locked_leg_id=NULL WHERE resource_id=?",
                        (RESOURCE_AVAILABLE, rid),
                    )

        # 已完成交接在新版本中冻结复制原始双签记录。
        old_handoffs = {(h["from_ordinal"], h["to_ordinal"]): h
                        for h in self._handoffs(conn, assistance_id, old_version)}
        for ordinal in range(len(new_specs) - 1):
            new_handoff = self._handoff_between(conn, assistance_id, new_version, ordinal)
            old_handoff = old_handoffs.get((ordinal, ordinal + 1))
            if new_legs[ordinal]["state"] == LEG_COMPLETED and old_handoff \
                    and old_handoff["state"] == HANDOFF_COMPLETED:
                conn.execute(
                    "UPDATE handoffs SET frozen=1, state=?, outbound_confirmed_by=?, "
                    "outbound_confirmed_at=?, inbound_confirmed_by=?, inbound_confirmed_at=?, "
                    "arrival_reported_by=?, arrival_reported_at=?, passenger_present=1 "
                    "WHERE handoff_id=?",
                    (HANDOFF_COMPLETED, old_handoff["outbound_confirmed_by"],
                     old_handoff["outbound_confirmed_at"], old_handoff["inbound_confirmed_by"],
                     old_handoff["inbound_confirmed_at"], old_handoff["arrival_reported_by"],
                     old_handoff["arrival_reported_at"], new_handoff["handoff_id"]),
                )

        conn.execute("UPDATE itinerary_versions SET status=? WHERE assistance_id=? AND version=?",
                     (VERSION_SUPERSEDED, assistance_id, old_version))
        conn.execute(
            "UPDATE escalations SET status=?, resolved_by=?, resolved_at=?, note=note||? "
            "WHERE assistance_id=? AND version=? AND status=?",
            (ESC_RESOLVED, actor_id, self._now_text(), f" 已随 {reason} 重排至版本 {new_version}",
             assistance_id, old_version, ESC_OPEN),
        )
        conn.execute("UPDATE assistance_requests SET current_version=?, status=? WHERE assistance_id=?",
                     (new_version, TRIP_ACTIVE, assistance_id))
        self._audit(conn, actor_id=actor_id, action="itinerary.revised",
                    resource_type="assistance", resource_id=assistance_id,
                    detail={"old_version": old_version, "new_version": new_version, "reason": reason,
                            "reoffered_ordinals": [s["ordinal"] for s in new_specs
                                                   if s["state"] == LEG_OFFERED]})
        return {"assistance_id": assistance_id, "version": new_version, "reason": reason,
                "legs": [self._leg_view(new_legs[s["ordinal"]], s) for s in new_specs]}

    def report_delay(self, *, request_id: str, actor_id: str, leg_id: str,
                     delay_minutes: int) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "leg_id": leg_id, "delay_minutes": delay_minutes}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            if leg["version"] != leg["_current_version"]:
                raise ConflictError("历史版本段请基于最新版本报告")
            assistance = self._assistance(conn, leg["assistance_id"])
            if assistance["status"] == TRIP_COMPLETED:
                raise ConflictError("协助单已完成")
            if actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("只有本段单位或协调员可以报告晚点")
            if not isinstance(delay_minutes, int) or delay_minutes <= 0:
                raise ValidationError("晚点分钟数必须是正整数")
            legs = self._legs(conn, leg["assistance_id"], leg["version"])
            delta = timedelta(minutes=delay_minutes)
            specs: list[dict[str, Any]] = []
            for item in legs:
                spec = {"site_id": item["site_id"], "from_location": item["from_location"],
                        "to_location": item["to_location"],
                        "required_kinds": json.loads(item["required_kinds_json"])}
                start = parse_time(item["scheduled_start"])
                end = parse_time(item["scheduled_end"])
                deadline = parse_time(item["acceptance_deadline"])
                if item["ordinal"] >= leg["ordinal"] and item["state"] != LEG_COMPLETED:
                    start, end, deadline = start + delta, end + delta, deadline + delta
                spec["scheduled_start"] = format_time(start)
                spec["scheduled_end"] = format_time(end)
                spec["acceptance_deadline"] = format_time(deadline)
                specs.append(spec)

            def create() -> tuple[str, str, dict[str, Any]]:
                result = self._revise(conn, assistance_id=leg["assistance_id"], actor_id=actor_id,
                                      reason="delay", new_specs=specs)
                return "assistance", leg["assistance_id"], result

            return self._idem(conn, request_id=request_id, action="relay.report_delay",
                              payload=payload, create=create)

    def change_ticket(self, *, request_id: str, actor_id: str, assistance_id: str,
                      legs: list[dict[str, Any]], note: str = "") -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "assistance_id": assistance_id, "legs": legs, "note": note}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            assistance = self._assistance(conn, assistance_id)
            if not (self._is_coordinator(actor) or actor_id == assistance["passenger_actor_id"]
                    or actor_id == assistance["agent_actor_id"]):
                raise PermissionDenied("只有旅客、代理或协调员可以改签")

            def create() -> tuple[str, str, dict[str, Any]]:
                result = self._revise(conn, assistance_id=assistance_id, actor_id=actor_id,
                                      reason="ticket_change", new_specs=legs)
                result["note"] = note
                return "assistance", assistance_id, result

            return self._idem(conn, request_id=request_id, action="relay.change_ticket",
                              payload=payload, create=create)

    def report_equipment_failure(self, *, request_id: str, actor_id: str, resource_id: str,
                                 note: str = "") -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "resource_id": resource_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            resource = conn.execute("SELECT * FROM assist_resources WHERE resource_id=?",
                                    (resource_id,)).fetchone()
            if resource is None:
                raise NotFoundError("资源不存在")
            if actor["role"] == "operator" and actor["organization_id"] != resource["organization_id"]:
                raise PermissionDenied("只有资源所属单位可以报告故障")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE assist_resources SET status=? WHERE resource_id=?",
                             (RESOURCE_FAULTY, resource_id))
                affected_leg = None
                if resource["locked_leg_id"]:
                    affected_leg = self._leg(conn, resource["locked_leg_id"])
                    escalation_id = self._open_escalation(
                        conn, assistance_id=affected_leg["assistance_id"],
                        version=affected_leg["version"], ordinal=affected_leg["ordinal"],
                        reason="equipment_failure", level=2, opened_by=actor_id,
                        note=f"资源 {resource['identifier']} 故障：{note}")
                else:
                    escalation_id = None
                self._audit(conn, actor_id=actor_id, action="resource.faulted",
                            resource_type="assist_resource", resource_id=resource_id,
                            detail={"locked_leg_id": resource["locked_leg_id"], "note": note})
                response = {"resource_id": resource_id, "status": RESOURCE_FAULTY}
                requires_assignment: list[int] = []
                if affected_leg and affected_leg["version"] == affected_leg["_current_version"]:
                    legs = self._legs(conn, affected_leg["assistance_id"], affected_leg["version"])
                    specs = []
                    for item in legs:
                        spec = {"site_id": item["site_id"], "from_location": item["from_location"],
                                "to_location": item["to_location"],
                                "required_kinds": json.loads(item["required_kinds_json"]),
                                "scheduled_start": item["scheduled_start"],
                                "scheduled_end": item["scheduled_end"],
                                "acceptance_deadline": item["acceptance_deadline"]}
                        specs.append(spec)
                    revised = self._revise(
                        conn, assistance_id=affected_leg["assistance_id"], actor_id=actor_id,
                        reason="equipment_failure", new_specs=specs, check_capacity=False)
                    # 检测新版本中缺少可用资源的段：升级紧急指挥人工补位。
                    for new_leg in self._legs(conn, affected_leg["assistance_id"],
                                              revised["version"]):
                        if new_leg["state"] not in (LEG_OFFERED, LEG_ACCEPTED, LEG_IN_PROGRESS):
                            continue
                        kinds = json.loads(new_leg["required_kinds_json"])
                        locked_kinds: set[str] = set()
                        if new_leg["locked_resource_id"]:
                            r = conn.execute(
                                "SELECT kind,status FROM assist_resources WHERE resource_id=?",
                                (new_leg["locked_resource_id"],)).fetchone()
                            if r and r["status"] != RESOURCE_FAULTY:
                                locked_kinds.add(r["kind"])
                        for kind in kinds:
                            if kind in locked_kinds:
                                continue
                            found = self._find_resource(
                                conn, organization_id=new_leg["organization_id"],
                                site_id=new_leg["site_id"], kind=kind,
                                start=new_leg["scheduled_start"], end=new_leg["scheduled_end"])
                            if found is None:
                                requires_assignment.append(new_leg["ordinal"])
                                self._open_escalation(
                                    conn, assistance_id=affected_leg["assistance_id"],
                                    version=revised["version"], ordinal=new_leg["ordinal"],
                                    reason="no_replace_capacity", level=3, opened_by=actor_id,
                                    note=f"资源 {resource['identifier']} 故障后无 {kind} 替代")
                    response["revision"] = revised
                    response["requires_assignment"] = sorted(set(requires_assignment))
                if escalation_id:
                    response["escalation_id"] = escalation_id
                return "assist_resource", resource_id, response

            return self._idem(conn, request_id=request_id, action="relay.equipment_failure",
                              payload=payload, create=create)

    def takeover(self, *, request_id: str, actor_id: str, assistance_id: str,
                 note: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "assistance_id": assistance_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            if not self._is_coordinator(actor):
                raise PermissionDenied("只有协调员可以启动紧急人工接管")
            assistance = self._assistance(conn, assistance_id)
            if assistance["status"] == TRIP_TAKEOVER:
                raise ConflictError("协助单已处于人工接管状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute("UPDATE assistance_requests SET status=? WHERE assistance_id=?",
                             (TRIP_TAKEOVER, assistance_id))
                legs = self._legs(conn, assistance_id, assistance["current_version"])
                target = next((leg for leg in legs if leg["state"] in (
                    LEG_OFFERED, LEG_ACCEPTED, LEG_IN_PROGRESS, LEG_NO_SHOW)), None)
                ordinal = target["ordinal"] if target else 0
                for leg in legs:
                    if leg["state"] in (LEG_IN_PROGRESS, LEG_ACCEPTED, LEG_NO_SHOW):
                        conn.execute("UPDATE legs SET state=?, taken_over_by=? WHERE leg_id=?",
                                     (LEG_TAKEN_OVER, actor_id, leg["leg_id"]))
                escalation_id = self._open_escalation(
                    conn, assistance_id=assistance_id,
                    version=assistance["current_version"], ordinal=ordinal,
                    reason="manual_takeover", level=3, opened_by=actor_id, note=note)
                self._audit(conn, actor_id=actor_id, action="assistance.takeover",
                            resource_type="assistance", resource_id=assistance_id,
                            detail={"note": note, "escalation_id": escalation_id})
                return "assistance", assistance_id, {"assistance_id": assistance_id,
                                                      "status": TRIP_TAKEOVER,
                                                      "escalation_id": escalation_id}

            return self._idem(conn, request_id=request_id, action="relay.takeover",
                              payload=payload, create=create)

    def resume_from_takeover(self, *, request_id: str, actor_id: str, assistance_id: str,
                             legs: list[dict[str, Any]]) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "assistance_id": assistance_id, "legs": legs}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            if not self._is_coordinator(actor):
                raise PermissionDenied("只有协调员可以解除人工接管")
            assistance = self._assistance(conn, assistance_id)
            if assistance["status"] != TRIP_TAKEOVER:
                raise ConflictError("协助单未处于人工接管状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                result = self._revise(conn, assistance_id=assistance_id, actor_id=actor_id,
                                      reason="takeover_resume", new_specs=legs)
                conn.execute(
                    "UPDATE escalations SET status=?, resolved_by=?, resolved_at=?, note=note||? "
                    "WHERE assistance_id=? AND reason='manual_takeover' AND status=?",
                    (ESC_RESOLVED, actor_id, self._now_text(), " 人工接管结束，已编排新版本",
                     assistance_id, ESC_OPEN),
                )
                return "assistance", assistance_id, result

            return self._idem(conn, request_id=request_id, action="relay.resume_takeover",
                              payload=payload, create=create)

    # -- 超时扫描与升级处置 ---------------------------------------------

    def sweep_timeouts(self) -> dict[str, Any]:
        """根据当前时钟扫描全部进行中版本，产生确定性升级。"""

        opened: list[dict[str, Any]] = []
        with self.database.transaction(immediate=True) as conn:
            now = self._now()
            assists = conn.execute(
                "SELECT * FROM assistance_requests WHERE status!=?", (TRIP_COMPLETED,)
            ).fetchall()
            for assistance_row in assists:
                assistance = dict(assistance_row)
                if assistance["status"] == TRIP_TAKEOVER:
                    continue
                version = assistance["current_version"]
                legs = self._legs(conn, assistance["assistance_id"], version)
                handoffs = self._handoffs(conn, assistance["assistance_id"], version)
                for leg in legs:
                    if leg["state"] == LEG_OFFERED and parse_time(leg["acceptance_deadline"]) < now:
                        esc = self._open_escalation(
                            conn, assistance_id=assistance["assistance_id"], version=version,
                            ordinal=leg["ordinal"], reason="acceptance_timeout", level=2,
                            opened_by="system", note="超过接单截止时间仍无人接单")
                        opened.append({"escalation_id": esc, "reason": "acceptance_timeout",
                                       "ordinal": leg["ordinal"]})
                    if leg["state"] == LEG_IN_PROGRESS:
                        outgoing = next((h for h in handoffs if h["from_ordinal"] == leg["ordinal"]), None)
                        if outgoing is None and parse_time(leg["scheduled_end"]) + ESCALATE_WINDOW < now \
                                and leg["state"] != LEG_DELIVERED:
                            esc = self._open_escalation(
                                conn, assistance_id=assistance["assistance_id"], version=version,
                                ordinal=leg["ordinal"], reason="delivery_overdue", level=2,
                                opened_by="system", note="末段超过计划送达时间")
                            opened.append({"escalation_id": esc, "reason": "delivery_overdue",
                                           "ordinal": leg["ordinal"]})
                for handoff in handoffs:
                    deadline = parse_time(handoff["deadline"])
                    if handoff["state"] == HANDOFF_ARRIVED:
                        if deadline + EMERGENCY_WINDOW < now:
                            esc = self._open_escalation(
                                conn, assistance_id=assistance["assistance_id"], version=version,
                                ordinal=handoff["to_ordinal"], reason="handoff_overdue", level=3,
                                opened_by="system", note="交接超时 30 分钟，升级紧急指挥")
                            opened.append({"escalation_id": esc, "reason": "handoff_overdue",
                                           "level": 3, "ordinal": handoff["to_ordinal"]})
                        elif deadline + ESCALATE_WINDOW < now:
                            esc = self._open_escalation(
                                conn, assistance_id=assistance["assistance_id"], version=version,
                                ordinal=handoff["to_ordinal"], reason="handoff_overdue", level=2,
                                opened_by="system", note="交接超过截止时间")
                            opened.append({"escalation_id": esc, "reason": "handoff_overdue",
                                           "level": 2, "ordinal": handoff["to_ordinal"]})
                    elif handoff["state"] == HANDOFF_PENDING:
                        outbound = next(l for l in legs if l["ordinal"] == handoff["from_ordinal"])
                        if outbound["state"] == LEG_IN_PROGRESS and deadline + ESCALATE_WINDOW < now:
                            esc = self._open_escalation(
                                conn, assistance_id=assistance["assistance_id"], version=version,
                                ordinal=handoff["from_ordinal"], reason="arrival_overdue", level=2,
                                opened_by="system", note="交出方未按时到达交接位置")
                            opened.append({"escalation_id": esc, "reason": "arrival_overdue",
                                           "ordinal": handoff["from_ordinal"]})
            self._audit(conn, actor_id="system", action="timeouts.swept",
                        resource_type="system", resource_id="sweep",
                        detail={"opened": len(opened)})
        return {"opened": opened, "count": len(opened)}

    def resolve_escalation(self, *, request_id: str, actor_id: str, escalation_id: str,
                           note: str = "") -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "escalation_id": escalation_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            seen = self._replay_seen(conn, request_id, payload)
            if seen is not None:
                return seen
            actor = self._actor(conn, actor_id)
            row = conn.execute("SELECT * FROM escalations WHERE escalation_id=?",
                               (escalation_id,)).fetchone()
            if row is None:
                raise NotFoundError("升级记录不存在")
            escalation = dict(row)
            if escalation["status"] == ESC_RESOLVED:
                raise ConflictError("升级记录已经处置")
            if not self._is_coordinator(actor):
                raise PermissionDenied("只有协调员可以关闭升级")

            def create() -> tuple[str, str, dict[str, Any]]:
                conn.execute(
                    "UPDATE escalations SET status=?, resolved_by=?, resolved_at=?, note=note||? "
                    "WHERE escalation_id=?",
                    (ESC_RESOLVED, actor_id, self._now_text(), f" 处置：{note}", escalation_id),
                )
                self._audit(conn, actor_id=actor_id, action="escalation.resolved",
                            resource_type="escalation", resource_id=escalation_id,
                            detail={"note": note})
                return "escalation", escalation_id, {"escalation_id": escalation_id,
                                                      "status": ESC_RESOLVED}

            return self._idem(conn, request_id=request_id, action="relay.resolve_escalation",
                              payload=payload, create=create)

    # -- 最小披露与访问留痕 ---------------------------------------------

    def _log_access(self, conn, *, actor: dict[str, Any], leg: dict[str, Any], reason: str) -> list[str]:
        needs = self._needs(conn, leg["assistance_id"])
        revealed = [need["need_key"] for need in needs
                    if self._need_visible(need, ordinal=leg["ordinal"],
                                          organization_id=leg["organization_id"])]
        conn.execute(
            "INSERT INTO access_log(assistance_id,actor_id,organization_id,version,ordinal,"
            "revealed_keys_json,reason,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
            (leg["assistance_id"], actor["actor_id"], actor["organization_id"], leg["version"],
             leg["ordinal"], canonical_json(revealed), reason, self._now_text()),
        )
        return revealed

    def reveal_leg_needs(self, *, actor_id: str, leg_id: str,
                         reason: str = "view") -> dict[str, Any]:
        with self.database.transaction(immediate=False) as conn:
            actor = self._actor(conn, actor_id)
            leg = self._leg(conn, leg_id)
            if not self._is_staff(actor):
                raise PermissionDenied("只有值守人员可以查看段需求")
            if actor["role"] == "operator" and actor["organization_id"] != leg["organization_id"]:
                raise PermissionDenied("不能查看其他单位段的需求")
            needs = self._needs(conn, leg["assistance_id"])
            visible = [{"need_key": n["need_key"], "category": n["category"], "detail": n["detail"]}
                       for n in needs
                       if self._need_visible(n, ordinal=leg["ordinal"],
                                             organization_id=leg["organization_id"])]
            revealed_keys = self._log_access(conn, actor=actor, leg=leg, reason=reason)
            self._audit(conn, actor_id=actor_id, action="needs.revealed",
                        resource_type="leg", resource_id=leg_id,
                        detail={"assistance_id": leg["assistance_id"], "version": leg["version"],
                                "ordinal": leg["ordinal"], "revealed_keys": revealed_keys,
                                "reason": reason})
            return {"leg_id": leg_id, "assistance_id": leg["assistance_id"],
                    "version": leg["version"], "ordinal": leg["ordinal"],
                    "needs": visible, "revealed_keys": revealed_keys}

    def access_history(self, *, actor_id: str, assistance_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=False) as conn:
            actor = self._actor(conn, actor_id)
            assistance = self._assistance(conn, assistance_id)
            if not (self._is_coordinator(actor) or actor_id == assistance["passenger_actor_id"]
                    or actor_id == assistance["agent_actor_id"] or actor["role"] == "auditor"):
                raise PermissionDenied("只有旅客本人、代理、协调员或审计员可以核对访问履历")
            rows = conn.execute(
                "SELECT * FROM access_log WHERE assistance_id=? ORDER BY sequence", (assistance_id,)
            ).fetchall()
            items = [{"sequence": row["sequence"], "actor_id": row["actor_id"],
                      "organization_id": row["organization_id"], "version": row["version"],
                      "ordinal": row["ordinal"], "revealed_keys": json.loads(row["revealed_keys_json"]),
                      "reason": row["reason"], "occurred_at": row["occurred_at"]} for row in rows]
            return {"assistance_id": assistance_id, "items": items, "count": len(items)}

    # -- 视图 -----------------------------------------------------------

    def _leg_view(self, leg: dict[str, Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "leg_id": leg["leg_id"], "version": leg["version"], "ordinal": leg["ordinal"],
            "site_id": leg["site_id"], "organization_id": leg["organization_id"],
            "from_location": leg["from_location"], "to_location": leg["to_location"],
            "scheduled_start": leg["scheduled_start"], "scheduled_end": leg["scheduled_end"],
            "acceptance_deadline": leg["acceptance_deadline"],
            "required_kinds": json.loads(leg["required_kinds_json"]),
            "state": leg["state"], "frozen": bool(leg["frozen"]),
            "source_version": leg["source_version"],
            "locked_resource_id": leg["locked_resource_id"],
        }

    def get_assistance(self, *, actor_id: str, assistance_id: str,
                       version: int | None = None) -> dict[str, Any]:
        with self.database.transaction(immediate=False) as conn:
            actor = self._actor(conn, actor_id)
            assistance = self._assistance(conn, assistance_id)
            if actor["role"] == "passenger" and actor_id != assistance["passenger_actor_id"]:
                raise PermissionDenied("只能查看本人的接续协助单")
            version = version or assistance["current_version"]
            if actor["role"] == "operator":
                legs = self._legs(conn, assistance_id, version)
                if not any(leg["organization_id"] == actor["organization_id"] for leg in legs):
                    raise PermissionDenied("本单位不承担该行程的任何段")
            handoffs = self._handoffs(conn, assistance_id, version)
            if actor["role"] == "operator":
                org = actor["organization_id"]
                org_ordinals = {leg["ordinal"] for leg in legs}
                handoffs = [h for h in handoffs
                            if h["from_ordinal"] in org_ordinals or h["to_ordinal"] in org_ordinals]
            version_row = conn.execute(
                "SELECT * FROM itinerary_versions WHERE assistance_id=? AND version=?",
                (assistance_id, version),
            ).fetchone()
            if version_row is None:
                raise NotFoundError("行程版本不存在")
            return {
                "assistance_id": assistance_id, "version": version,
                "current_version": assistance["current_version"],
                "status": assistance["status"],
                "version_reason": version_row["reason"],
                "version_status": version_row["status"],
                "passenger_actor_id": assistance["passenger_actor_id"],
                "legs": [self._leg_view(leg) for leg in
                         self._legs(conn, assistance_id, version)
                         if actor["role"] != "operator"
                         or leg["organization_id"] == actor["organization_id"]],
                "handoffs": [dict(h) for h in handoffs],
            }

    def _risk(self, now: datetime, legs: list[dict[str, Any]], handoffs: list[dict[str, Any]],
              open_levels: list[int], takeover: bool) -> str:
        if takeover:
            return "critical"
        if 3 in open_levels:
            return "critical"
        risk = "none"
        for leg in legs:
            if leg["state"] == LEG_OFFERED:
                deadline = parse_time(leg["acceptance_deadline"])
                if deadline < now:
                    return "critical" if 2 in open_levels else "high"
                if deadline - now <= NEAR_WINDOW:
                    risk = "near"
            elif leg["state"] in (LEG_IN_PROGRESS, LEG_ACCEPTED):
                end = parse_time(leg["scheduled_end"])
                if end < now:
                    return "high"
                if end - now <= NEAR_WINDOW:
                    risk = "near"
        for handoff in handoffs:
            if handoff["state"] in (HANDOFF_PENDING, HANDOFF_ARRIVED):
                deadline = parse_time(handoff["deadline"])
                if deadline < now:
                    return "high"
                if deadline - now <= NEAR_WINDOW:
                    risk = "near"
        if 2 in open_levels:
            return "high"
        return risk

    def board(self, *, actor_id: str) -> dict[str, Any]:
        """值班员看板：当前责任人、下一次交接与超时风险。"""

        with self.database.transaction(immediate=False) as conn:
            actor = self._actor(conn, actor_id)
            if not self._is_staff(actor):
                raise PermissionDenied("看板仅对值守人员开放")
            now = self._now()
            query = "SELECT * FROM assistance_requests WHERE status!=?"
            parameters: list[Any] = [TRIP_COMPLETED]
            rows = conn.execute(query, parameters).fetchall()
            items = []
            for assistance_row in rows:
                assistance = dict(assistance_row)
                version = assistance["current_version"]
                legs = self._legs(conn, assistance["assistance_id"], version)
                handoffs = self._handoffs(conn, assistance["assistance_id"], version)
                is_operator = actor["role"] == "operator"
                if is_operator:
                    org = actor["organization_id"]
                    my_open_legs = [leg for leg in legs
                                    if leg["organization_id"] == org
                                    and leg["state"] != LEG_COMPLETED]
                    if not my_open_legs:
                        continue
                chain_active = next((leg for leg in legs if leg["state"] in (
                    LEG_IN_PROGRESS, LEG_NO_SHOW, LEG_TAKEN_OVER, LEG_DELIVERED)), None)
                if chain_active is None:
                    chain_active = next((leg for leg in legs if leg["state"] == LEG_ACCEPTED), None)
                if chain_active is None:
                    chain_active = next((leg for leg in legs if leg["state"] == LEG_OFFERED), None)
                if is_operator:
                    # 责任人：若护送中段属于本单位则是本单位，否则是本单位下一段待办责任。
                    if chain_active is not None and chain_active["organization_id"] == org:
                        active_leg = chain_active
                    else:
                        active_leg = sorted(my_open_legs, key=lambda leg: leg["ordinal"])[0]
                    my_ordinals = {leg["ordinal"] for leg in my_open_legs}
                    visible_handoffs = [
                        h for h in handoffs
                        if (h["from_ordinal"] in my_ordinals or h["to_ordinal"] in my_ordinals)
                        and h["state"] != HANDOFF_COMPLETED]
                else:
                    active_leg = chain_active
                    visible_handoffs = [h for h in handoffs if h["state"] != HANDOFF_COMPLETED]
                next_handoff = None
                if active_leg is not None:
                    candidate = next((h for h in visible_handoffs
                                      if h["from_ordinal"] == active_leg["ordinal"]), None)
                    if candidate is None and is_operator:
                        # 本单位尚未接手：显示把旅客交进来的那次交接。
                        candidate = next((h for h in visible_handoffs
                                          if h["to_ordinal"] == active_leg["ordinal"]), None)
                    if candidate is not None:
                        inbound = next((l for l in legs if l["ordinal"] == candidate["to_ordinal"]), None)
                        next_handoff = {
                            "handoff_id": candidate["handoff_id"],
                            "location": candidate["location"],
                            "deadline": candidate["deadline"],
                            "state": candidate["state"],
                            "inbound_organization_id": inbound["organization_id"] if inbound else None,
                            "inbound_state": inbound["state"] if inbound else None,
                        }
                esc_rows = conn.execute(
                    "SELECT level,reason,ordinal,status FROM escalations WHERE assistance_id=? "
                    "AND version=? AND status=?",
                    (assistance["assistance_id"], version, ESC_OPEN),
                ).fetchall()
                if is_operator:
                    esc_rows = [row for row in esc_rows if row["ordinal"] in my_ordinals]
                open_levels = [row["level"] for row in esc_rows]
                responsible = None
                if active_leg is not None:
                    responsible = {
                        "ordinal": active_leg["ordinal"],
                        "organization_id": active_leg["organization_id"],
                        "site_id": active_leg["site_id"],
                        "state": active_leg["state"],
                        "locked_resource_id": active_leg["locked_resource_id"],
                        "taken_over_by": active_leg["taken_over_by"],
                    }
                risk_legs = my_open_legs if is_operator else legs
                items.append({
                    "assistance_id": assistance["assistance_id"],
                    "version": version,
                    "status": assistance["status"],
                    "current_responsible": responsible,
                    "next_handoff": next_handoff,
                    "open_escalations": [{"level": row["level"], "reason": row["reason"],
                                          "ordinal": row["ordinal"]} for row in esc_rows],
                    "timeout_risk": self._risk(
                        now, risk_legs, visible_handoffs, open_levels,
                        assistance["status"] == TRIP_TAKEOVER and not is_operator),
                })
            order = {"critical": 0, "high": 1, "near": 2, "none": 3}
            items.sort(key=lambda item: (order[item["timeout_risk"]], item["assistance_id"]))
            return {"generated_at": format_time(now), "items": items, "count": len(items)}

    def list_escalations(self, *, actor_id: str, assistance_id: str | None = None,
                         status: str = ESC_OPEN) -> dict[str, Any]:
        with self.database.transaction(immediate=False) as conn:
            actor = self._actor(conn, actor_id)
            if not self._is_staff(actor):
                raise PermissionDenied("只有值守人员可以查看升级")
            sql = "SELECT * FROM escalations WHERE status=?"
            parameters: list[Any] = [status]
            if assistance_id:
                sql += " AND assistance_id=?"
                parameters.append(assistance_id)
            rows = conn.execute(sql + " ORDER BY opened_at", parameters).fetchall()
            items = []
            for row in rows:
                data = dict(row)
                if actor["role"] == "operator":
                    legs = self._legs(conn, data["assistance_id"], data["version"])
                    leg = next((l for l in legs if l["ordinal"] == data["ordinal"]), None)
                    if leg is None or leg["organization_id"] != actor["organization_id"]:
                        continue
                items.append(data)
            return {"items": items, "count": len(items)}
