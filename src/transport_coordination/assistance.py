"""实现无障碍旅客接续协助链：分段承接、两阶段交接、改线重排、最小披露与升级。"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor

# ---- 确定性时限规则（UTC，分钟） -----------------------------------------
HANDOVER_GRACE_MINUTES = 5        # 交接点超时宽限
INCOMING_GRACE_MINUTES = 3        # 送出方已到、接方未确认的宽限
LEVEL2_LAG_MINUTES = 10           # 一级未处置后升级二级
ACCEPT_DEADLINE_MINUTES = 10      # 新版本到岗承接时限
EQUIPMENT_FIX_MINUTES = 10        # 设备故障一级处置时限
NO_SHOW_DECISION_MINUTES = 15     # 失约后协调员处置时限
START_GRACE_MINUTES = 5           # 首段未开工宽限
BOARD_IMMINENT_MINUTES = 15       # 值守板“临近”阈值

SEGMENT_STATES = frozenset({
    "proposed", "accepted", "in_progress", "resourcing",
    "completed", "superseded", "cancelled",
})
HANDOVER_STATES = frozenset({
    "pending", "awaiting_incoming", "completed", "superseded", "cancelled",
})
CHAIN_STATUSES = frozenset({"active", "no_show", "emergency", "completed", "cancelled"})
SENSITIVITIES = frozenset({"general", "health"})


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_iso(value: str, field: str) -> str:
    """校验并归一化 ISO-8601 时间字符串。"""

    value = str(value).strip()
    if not value:
        raise ValidationError(f"{field} 不能为空")
    try:
        normalized = value.replace("Z", "+00:00")
        from datetime import datetime
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须携带时区")
    return value


class AssistanceService:
    """编排服务链全生命周期的确定性规则。"""

    def __init__(self, database, clock) -> None:
        self.database = database
        self.clock = clock

    # ---- 基础工具 --------------------------------------------------------
    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _soon(self, minutes: int) -> str:
        return (self.clock.now() + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str) -> Actor:
        if not actor_id:
            raise PermissionDenied("缺少操作者标识 X-Actor-Id")
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require_role(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> dict[str, Any]:
        if not request_id or not str(request_id).strip():
            raise ValidationError("request_id 不能为空")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_id: str,
               detail: dict[str, Any], resource_type: str = "assistance_chain") -> None:
        append_event(connection, actor_id=actor_id or "system", action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    # ---- 值守能力与设备 --------------------------------------------------
    def declare_capability(self, *, request_id: str, actor_id: str, organization_id: str,
                           service_kind: str, handover_point: str,
                           equipment: list[dict[str, str]], window_start: str,
                           window_end: str, site_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "service_kind": service_kind, "handover_point": handover_point,
                   "equipment": equipment, "window_start": window_start,
                   "window_end": window_end}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator", "dispatcher")
            _replay = self._replay_if_exists(connection, request_id, "declare_capability", payload)
            if _replay is not None:
                return _replay
            if actor.organization_id != organization_id and actor.role != "admin":
                raise PermissionDenied("不能为其他组织登记值守能力")
            if not isinstance(equipment, list) or not equipment:
                raise ValidationError("equipment 必须是非空列表")
            for item in equipment:
                if not item.get("code") or not item.get("ref"):
                    raise ValidationError("设备项必须包含 code 与 ref")
            window_start = _parse_iso(window_start, "window_start")
            window_end = _parse_iso(window_end, "window_end")
            if window_end <= window_start:
                raise ValidationError("值守窗口结束时间必须晚于开始时间")

            def create():
                capability_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO capability_declarations(capability_id,organization_id,site_id,"
                    "service_kind,handover_point,equipment_json,window_start,window_end,active,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
                    (capability_id, organization_id, site_id, service_kind, handover_point,
                     canonical_json(equipment), window_start, window_end, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="capability.declared",
                            resource_id=capability_id, resource_type="capability",
                            detail={"organization_id": organization_id,
                                    "service_kind": service_kind,
                                    "handover_point": handover_point,
                                    "window_start": window_start, "window_end": window_end})
                return "capability", capability_id, {"capability_id": capability_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_capability", payload=payload, create=create)

    def report_equipment_incident(self, *, request_id: str, actor_id: str, organization_id: str,
                                  handover_point: str, resource_code: str, resource_ref: str,
                                  reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "handover_point": handover_point, "resource_code": resource_code,
                   "resource_ref": resource_ref, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "operator", "dispatcher", "worker")
            _replay = self._replay_if_exists(connection, request_id, "report_equipment_incident", payload)
            if _replay is not None:
                return _replay
            if actor.organization_id != organization_id and actor.role != "admin":
                raise PermissionDenied("不能上报其他组织的设备故障")

            def create():
                incident_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO equipment_incidents(incident_id,organization_id,handover_point,"
                    "resource_code,resource_ref,state,reported_by,reported_at) "
                    "VALUES(?,?,?,?,?,'open',?,?)",
                    (incident_id, organization_id, handover_point, resource_code,
                     resource_ref, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="equipment.incident_reported",
                            resource_id=incident_id, resource_type="equipment_incident",
                            detail={"organization_id": organization_id,
                                    "handover_point": handover_point,
                                    "resource_code": resource_code, "resource_ref": resource_ref})
                self._apply_equipment_fault(connection, incident_id=incident_id,
                                           organization_id=organization_id,
                                           handover_point=handover_point,
                                           resource_code=resource_code, resource_ref=resource_ref)
                return "equipment_incident", incident_id, {"incident_id": incident_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="report_equipment_incident", payload=payload,
                                    create=create)

    # ---- 服务链建档 ------------------------------------------------------
    def create_chain(self, *, request_id: str, actor_id: str | None, passenger_ref: str,
                     passenger_token: str, segments: list[dict[str, Any]],
                     needs: list[dict[str, Any]], agent_actor_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "passenger_ref": passenger_ref,
                   "passenger_token_hash": _hash_token(passenger_token or ""),
                   "segments": segments, "needs": needs, "agent_actor_id": agent_actor_id}
        with self.database.transaction(immediate=True) as connection:
            _replay = self._replay_if_exists(connection, request_id, "create_chain", payload)
            if _replay is not None:
                return _replay
            if actor_id:
                actor = self._actor(connection, actor_id)
                self._require_role(actor, "admin", "coordinator")
            if not passenger_token or len(passenger_token) < 8:
                raise ValidationError("旅客核验令牌至少 8 个字符，供其本人后续核验")
            if not isinstance(segments, list) or len(segments) < 2:
                raise ValidationError("接续链至少包含两个区段")
            if not isinstance(needs, list) or not needs:
                raise ValidationError("必须提交必要需求清单")
            normalized_segments = [self._normalize_segment_spec(index, spec)
                                   for index, spec in enumerate(segments, start=1)]
            for previous, current in zip(normalized_segments, normalized_segments[1:]):
                if current["scheduled_start"] < previous["scheduled_end"]:
                    raise ValidationError(
                        f"第 {current['seq']} 段开始早于前一段结束，服务链断裂")
                if not previous["handover_location"]:
                    raise ValidationError("相邻区段必须声明交接位置")
            normalized_needs = [self._normalize_need(index, item, len(normalized_segments))
                                for index, item in enumerate(needs, start=1)]

            def create():
                chain_id = uuid.uuid4().hex
                creator = actor_id or (agent_actor_id or "passenger")
                connection.execute(
                    "INSERT INTO assistance_chains(chain_id,passenger_ref,passenger_token_hash,"
                    "agent_actor_id,status,current_version,segment_count,created_by,created_at) "
                    "VALUES(?,?,?,?,?,1,?,?,?)",
                    (chain_id, passenger_ref, _hash_token(passenger_token), agent_actor_id,
                     "active", len(normalized_segments), creator, self._now()),
                )
                connection.execute(
                    "INSERT INTO itinerary_versions(chain_id,version,reason,trigger_actor_id,created_at)"
                    " VALUES(?,1,'initial',?,?)",
                    (chain_id, creator, self._now()),
                )
                for spec in normalized_segments:
                    connection.execute(
                        "INSERT INTO chain_segments(segment_id,chain_id,version,seq,service_kind,"
                        "organization_id,scheduled_start,scheduled_end,board_location,"
                        "handover_location,equipment_required_json,state,immutable,"
                        "source_segment_id,created_at) "
                        "VALUES(?,?,1,?,?,?,?,?,?,?,?, 'proposed',0,NULL,?)",
                        (uuid.uuid4().hex, chain_id, spec["seq"], spec["service_kind"],
                         spec["organization_id"], spec["scheduled_start"], spec["scheduled_end"],
                         spec["board_location"], spec["handover_location"],
                         canonical_json(spec["equipment_required"]), self._now()),
                    )
                self._rebuild_handovers(connection, chain_id=chain_id, version=1)
                for item in normalized_needs:
                    connection.execute(
                        "INSERT INTO need_items(item_id,chain_id,code,detail,sensitivity,"
                        "visible_scope,visible_segments_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, chain_id, item["code"], item["detail"],
                         item["sensitivity"], item["visibility"],
                         canonical_json(item["segments"]), self._now()),
                    )
                self._audit(connection, actor_id=creator, action="chain.created",
                            resource_id=chain_id,
                            detail={"segments": len(normalized_segments),
                                    "needs": len(normalized_needs), "version": 1})
                return "assistance_chain", chain_id, {"chain_id": chain_id, "version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_chain", payload=payload, create=create)

    def _normalize_segment_spec(self, seq: int, spec: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(spec, dict):
            raise ValidationError(f"第 {seq} 段必须是对象")
        organization_id = str(spec.get("organization_id", "")).strip()
        service_kind = str(spec.get("service_kind", "")).strip()
        board_location = str(spec.get("board_location", "")).strip()
        if not organization_id or not service_kind or not board_location:
            raise ValidationError(f"第 {seq} 段缺少 organization_id/service_kind/board_location")
        start = _parse_iso(spec.get("scheduled_start", ""), f"第 {seq} 段 scheduled_start")
        end = _parse_iso(spec.get("scheduled_end", ""), f"第 {seq} 段 scheduled_end")
        if end <= start:
            raise ValidationError(f"第 {seq} 段结束时间必须晚于开始时间")
        equipment = spec.get("equipment_required", [])
        if not isinstance(equipment, list):
            raise ValidationError(f"第 {seq} 段 equipment_required 必须是列表")
        return {
            "seq": seq, "organization_id": organization_id, "service_kind": service_kind,
            "board_location": board_location,
            "handover_location": str(spec.get("handover_location", "")).strip() or None,
            "scheduled_start": start, "scheduled_end": end,
            "equipment_required": sorted(str(code).strip() for code in equipment if str(code).strip()),
        }

    def _normalize_need(self, index: int, item: dict[str, Any], segment_count: int) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValidationError(f"第 {index} 项需求必须是对象")
        code = str(item.get("code", "")).strip()
        detail = str(item.get("detail", "")).strip()
        if not code or not detail:
            raise ValidationError(f"第 {index} 项需求缺少 code 或 detail")
        sensitivity = item.get("sensitivity", "general")
        if sensitivity not in SENSITIVITIES:
            raise ValidationError("sensitivity 只能是 general 或 health")
        visibility = item.get("visibility", "legs" if sensitivity == "health" else "all")
        if visibility not in {"all", "legs"}:
            raise ValidationError("visibility 只能是 all 或 legs")
        if sensitivity == "health" and visibility == "all":
            raise ValidationError(f"健康类需求 {code} 不得对全链披露")
        segments = item.get("segments")
        if visibility == "legs":
            if not isinstance(segments, list) or not segments:
                raise ValidationError(f"需求 {code} 必须显式指定可见区段 segments")
            segments = sorted({int(value) for value in segments})
            if any(value < 1 or value > segment_count for value in segments):
                raise ValidationError(f"需求 {code} 的可见区段超出范围")
        else:
            segments = list(range(1, segment_count + 1))
        return {"code": code, "detail": detail, "sensitivity": sensitivity,
                "visibility": visibility, "segments": segments}

    # ---- 承接与锁定 ------------------------------------------------------
    def accept_segment(self, *, request_id: str, actor_id: str, chain_id: str,
                       segment_seq: int, assignee_actor_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "chain_id": chain_id,
                   "segment_seq": segment_seq, "assignee_actor_id": assignee_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "dispatcher")
            _replay = self._replay_if_exists(connection, request_id, "accept_segment", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            self._require_mutable_chain(chain)
            segment = self._current_segment(connection, chain, segment_seq)
            if actor.role != "admin" and actor.organization_id != segment["organization_id"]:
                raise PermissionDenied("只能承接本单位区段")
            assignee = self._actor(connection, assignee_actor_id)
            if assignee.organization_id != segment["organization_id"]:
                raise PermissionDenied("责任人必须属于承接单位")
            if assignee.role not in {"worker", "dispatcher"}:
                raise ValidationError("区段责任人必须是现场人员或值班员")
            if segment["state"] not in {"proposed", "resourcing"}:
                raise ConflictError(f"区段当前状态 {segment['state']} 不可承接")

            required = json.loads(segment["equipment_required_json"])
            candidate = self._match_capability(connection, organization_id=segment["organization_id"],
                                               service_kind=segment["service_kind"],
                                               points=self._segment_points(segment),
                                               start=segment["scheduled_start"],
                                               end=segment["scheduled_end"], required=required,
                                               exclude_segment=segment["segment_id"])
            if candidate is None:
                raise ConflictError("当前值守窗口或设备能力无法满足，需协调员重排")

            def create():
                for code, resource_ref in candidate.items():
                    connection.execute(
                        "INSERT INTO resource_locks(lock_id,chain_id,version,segment_id,"
                        "organization_id,resource_code,resource_ref,state,locked_at) "
                        "VALUES(?,?,?,?,?,?,?,'locked',?)",
                        (uuid.uuid4().hex, chain_id, chain["current_version"], segment["segment_id"],
                         segment["organization_id"], code, resource_ref, self._now()),
                    )
                connection.execute(
                    "UPDATE chain_segments SET state='accepted',assigned_actor_id=?,"
                    "accepted_at=? WHERE segment_id=?",
                    (assignee_actor_id, self._now(), segment["segment_id"]),
                )
                self._close_acceptance_escalations(connection, chain, segment_seq)
                self._audit(connection, actor_id=actor_id, action="segment.accepted",
                            resource_id=chain_id,
                            detail={"version": chain["current_version"], "seq": segment_seq,
                                    "assignee_actor_id": assignee_actor_id,
                                    "resources": candidate})
                return "segment", segment["segment_id"], {"state": "accepted",
                                                          "seq": segment_seq,
                                                          "version": chain["current_version"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="accept_segment", payload=payload, create=create)

    def start_segment(self, *, request_id: str, actor_id: str, chain_id: str,
                      segment_seq: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "chain_id": chain_id, "segment_seq": segment_seq}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            _replay = self._replay_if_exists(connection, request_id, "start_segment", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            self._require_mutable_chain(chain)
            segment = self._current_segment(connection, chain, segment_seq)
            self._require_segment_actor(connection, actor, segment)
            if segment["state"] != "accepted":
                raise ConflictError(f"区段状态 {segment['state']} 不可开工")

            def create():
                connection.execute(
                    "UPDATE chain_segments SET state='in_progress' WHERE segment_id=?",
                    (segment["segment_id"],),
                )
                self._audit(connection, actor_id=actor_id, action="segment.started",
                            resource_id=chain_id,
                            detail={"version": chain["current_version"], "seq": segment_seq})
                return "segment", segment["segment_id"], {"state": "in_progress", "seq": segment_seq}

            return self._idempotent(connection, request_id=request_id,
                                    action="start_segment", payload=payload, create=create)

    # ---- 两阶段交接 ------------------------------------------------------
    def handover_arrive(self, *, request_id: str, actor_id: str, chain_id: str,
                        boundary_seq: int) -> dict[str, Any]:
        """送出方把旅客带到交接点：交接进入等待接方状态。"""

        payload = {"actor_id": actor_id, "chain_id": chain_id, "boundary_seq": boundary_seq}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            chain = self._load_chain(connection, chain_id)
            replay = self._replay_if_exists(connection, request_id, "handover_arrive", payload)
            if replay is not None:
                return replay
            self._require_mutable_chain(chain)
            handover = self._current_handover(connection, chain, boundary_seq)
            outgoing = self._current_segment(connection, chain, boundary_seq)
            self._require_segment_actor(connection, actor, outgoing)
            if handover["state"] == "awaiting_incoming":
                raise ConflictError("送出回执已存在，等待接方确认")
            if handover["state"] == "completed":
                raise ConflictError("交接已经完成，重复回执不能再次交接")
            if handover["state"] != "pending":
                raise ConflictError(f"交接状态 {handover['state']} 不可送出")
            if outgoing["state"] != "in_progress":
                raise ConflictError("送出区段尚未在途，不能送出")

            def create():
                connection.execute(
                    "UPDATE handovers SET state='awaiting_incoming',outgoing_actor_id=?,"
                    "outgoing_confirmed_at=? WHERE handover_id=?",
                    (actor.actor_id, self._now(), handover["handover_id"]),
                )
                self._audit(connection, actor_id=actor_id, action="handover.arrived",
                            resource_id=chain_id,
                            detail={"version": chain["current_version"],
                                    "boundary": boundary_seq, "location": handover["location"]})
                return "handover", handover["handover_id"], {"state": "awaiting_incoming",
                                                             "boundary": boundary_seq}

            return self._idempotent(connection, request_id=request_id,
                                    action="handover_arrive", payload=payload, create=create)

    def handover_receive(self, *, request_id: str, actor_id: str, chain_id: str,
                         boundary_seq: int) -> dict[str, Any]:
        """接方确认接收：完成一次且仅一次交接。"""

        payload = {"actor_id": actor_id, "chain_id": chain_id, "boundary_seq": boundary_seq}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            chain = self._load_chain(connection, chain_id)
            replay = self._replay_if_exists(connection, request_id, "handover_receive", payload)
            if replay is not None:
                return replay
            self._require_mutable_chain(chain)
            handover = self._current_handover(connection, chain, boundary_seq)
            outgoing = self._current_segment(connection, chain, boundary_seq)
            incoming = self._current_segment(connection, chain, boundary_seq + 1)
            if handover["state"] == "completed":
                raise ConflictError("交接已经完成，重复回执不能再次交接")
            if handover["state"] != "awaiting_incoming":
                raise ConflictError("必须先由送出方把旅客带到交接点")
            if incoming["state"] != "accepted":
                raise ConflictError(f"接方区段状态 {incoming['state']}，不能接收")
            self._require_segment_actor(connection, actor, incoming)

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE handovers SET state='completed',incoming_actor_id=?,"
                    "incoming_confirmed_at=?,completed_at=?,immutable=1 WHERE handover_id=?",
                    (actor.actor_id, now, now, handover["handover_id"]),
                )
                connection.execute(
                    "UPDATE chain_segments SET state='completed',completed_at=?,immutable=1 "
                    "WHERE segment_id=?",
                    (now, outgoing["segment_id"]),
                )
                connection.execute(
                    "UPDATE chain_segments SET state='in_progress' WHERE segment_id=?",
                    (incoming["segment_id"],),
                )
                connection.execute(
                    "UPDATE resource_locks SET released_at=?, state='released' "
                    "WHERE segment_id=? AND state='locked'",
                    (now, outgoing["segment_id"]),
                )
                detail = {"version": chain["current_version"], "boundary": boundary_seq,
                          "location": handover["location"],
                          "outgoing_actor_id": handover["outgoing_actor_id"],
                          "incoming_actor_id": actor.actor_id}
                last_boundary = chain["segment_count"] - 1
                if boundary_seq == last_boundary:
                    connection.execute(
                        "UPDATE chain_segments SET state='completed',completed_at=?,immutable=1 "
                        "WHERE segment_id=?",
                        (now, incoming["segment_id"]),
                    )
                    connection.execute(
                        "UPDATE resource_locks SET released_at=?, state='released' "
                        "WHERE segment_id=? AND state='locked'",
                        (now, incoming["segment_id"]),
                    )
                    connection.execute(
                        "UPDATE assistance_chains SET status='completed' WHERE chain_id=?",
                        (chain_id,),
                    )
                    detail["chain_completed"] = True
                self._audit(connection, actor_id=actor_id, action="handover.received",
                            resource_id=chain_id, detail=detail)
                return "handover", handover["handover_id"], {"state": "completed",
                                                             "boundary": boundary_seq,
                                                             "chain_completed": detail.get("chain_completed", False)}

            return self._idempotent(connection, request_id=request_id,
                                    action="handover_receive", payload=payload, create=create)

    def _replay_if_exists(self, connection, request_id: str, action: str,
                          payload: dict[str, Any]) -> dict[str, Any] | None:
        """同一 request_id+载荷已执行过时返回原回执，否则返回 None。"""

        receipt = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=? AND action=?",
            (request_id, action),
        ).fetchone()
        if receipt is None:
            return None
        if receipt["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {"request_id": request_id, "resource_type": receipt["resource_type"],
                "resource_id": receipt["resource_id"], "replayed": True,
                **json.loads(receipt["response_json"])}

    # ---- 晚点与改签：版本化重排 ------------------------------------------
    def report_delay(self, *, request_id: str, actor_id: str, chain_id: str,
                     segment_seq: int, new_end: str, reason: str) -> dict[str, Any]:
        new_end = _parse_iso(new_end, "new_end")
        payload = {"actor_id": actor_id, "chain_id": chain_id,
                   "segment_seq": segment_seq, "new_end": new_end, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            _replay = self._replay_if_exists(connection, request_id, "report_delay", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            self._require_mutable_chain(chain)
            delayed = self._current_segment(connection, chain, segment_seq)
            if delayed["state"] in {"completed", "superseded", "cancelled"}:
                raise ConflictError("已完成或已取消的区段不能晚点")
            if new_end <= delayed["scheduled_end"]:
                raise ValidationError("晚点后的结束时间必须晚于原排定时间")
            if actor.role not in {"admin", "coordinator"} and actor.organization_id != delayed["organization_id"]:
                raise PermissionDenied("只有协调员或本段单位可上报晚点")
            delta_minutes = self._minutes_between(delayed["scheduled_end"], new_end)
            if not reason or not str(reason).strip():
                raise ValidationError("reason 不能为空")

            def create():
                version = self._new_version(connection, chain=chain, reason=f"delay:{reason}",
                                            trigger=actor.actor_id)
                new_delayed = self._version_segment(connection, chain, version, segment_seq)
                connection.execute(
                    "UPDATE chain_segments SET scheduled_end=? WHERE segment_id=?",
                    (new_end, new_delayed["segment_id"]),
                )
                required = json.loads(new_delayed["equipment_required_json"])
                candidate = self._match_capability(
                    connection, organization_id=new_delayed["organization_id"],
                    service_kind=new_delayed["service_kind"],
                    points=self._segment_points(new_delayed),
                    start=new_delayed["scheduled_start"], end=new_end, required=required,
                    exclude_segment=new_delayed["segment_id"])
                if new_delayed["state"] in {"accepted", "in_progress"}:
                    if candidate is not None:
                        self._migrate_locks(connection, chain, old_segment=delayed,
                                            new_segment=new_delayed, version=version)
                    else:
                        connection.execute(
                            "UPDATE chain_segments SET state='resourcing' WHERE segment_id=?",
                            (new_delayed["segment_id"],),
                        )
                        self._raise_escalation(connection, chain=chain, segment_seq=segment_seq,
                                               kind="equipment_fault", level=1,
                                               deadline_at=self._soon(EQUIPMENT_FIX_MINUTES),
                                               detail={"reason": "delay_window_uncovered"},
                                               version=version)
                elif new_delayed["state"] == "proposed":
                    self._raise_escalation(connection, chain=chain, segment_seq=segment_seq,
                                           kind="acceptance_due", level=1,
                                           deadline_at=self._soon(ACCEPT_DEADLINE_MINUTES),
                                           detail={"reason": "delay_reschedule"},
                                           version=version)
                # 后续区段一律回到待承接：必须重新接受后才重新锁定资源
                for seq in range(segment_seq + 1, chain["segment_count"] + 1):
                    old_seg = self._current_segment(connection, chain, seq)
                    new_seg = self._version_segment(connection, chain, version, seq)
                    new_start = self._shift(old_seg["scheduled_start"], delta_minutes)
                    new_seg_end = self._shift(old_seg["scheduled_end"], delta_minutes)
                    connection.execute(
                        "UPDATE chain_segments SET scheduled_start=?,scheduled_end=?,"
                        "state='proposed',assigned_actor_id=NULL,accepted_at=NULL "
                        "WHERE segment_id=?",
                        (new_start, new_seg_end, new_seg["segment_id"]),
                    )
                    self._raise_escalation(connection, chain=chain, segment_seq=seq,
                                           kind="acceptance_due", level=1,
                                           deadline_at=self._soon(ACCEPT_DEADLINE_MINUTES),
                                           detail={"reason": "upstream_delay"},
                                           version=version)
                self._release_superseded_locks(connection, chain, old_version=version - 1)
                self._reset_new_version_handovers(connection, chain_id=chain_id, version=version,
                                                  from_boundary=segment_seq)
                self._audit(connection, actor_id=actor_id, action="chain.delayed",
                            resource_id=chain_id,
                            detail={"version": version, "seq": segment_seq,
                                    "delta_minutes": delta_minutes, "reason": reason})
                return "assistance_chain", chain_id, {"version": version, "change": "delay"}

            return self._idempotent(connection, request_id=request_id,
                                    action="report_delay", payload=payload, create=create)

    def rebook(self, *, request_id: str, actor_id: str, chain_id: str, from_seq: int,
               segments: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "chain_id": chain_id, "from_seq": from_seq,
                   "segments": segments, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "coordinator")
            _replay = self._replay_if_exists(connection, request_id, "rebook", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            self._require_mutable_chain(chain)
            if from_seq < 1 or from_seq > chain["segment_count"]:
                raise ValidationError("from_seq 超出区段范围")
            anchor = self._current_segment(connection, chain, from_seq)
            if anchor["state"] == "completed":
                raise ConflictError("已完成区段不能改签，只能在其后重排")
            specs = [self._normalize_segment_spec(index, spec)
                     for index, spec in enumerate(segments, start=from_seq)]
            if from_seq > 1:
                anchor_previous = connection.execute(
                    "SELECT * FROM chain_segments WHERE chain_id=? AND version=? AND seq=?",
                    (chain_id, chain["current_version"], from_seq - 1),
                ).fetchone()
                if anchor_previous is None:
                    raise ValidationError("改签起点前序区段不存在")
                previous_end = anchor_previous["scheduled_end"]
            else:
                previous_end = None
            for spec in specs:
                if previous_end and spec["scheduled_start"] < previous_end:
                    raise ValidationError("改签首段不得早于前段结束时间")
                previous_end = spec["scheduled_end"]
            if not reason or not str(reason).strip():
                raise ValidationError("reason 不能为空")

            def create():
                version = self._new_version(connection, chain=chain, reason=f"rebook:{reason}",
                                            trigger=actor.actor_id)
                # 删除新版本中从 from_seq 起的自动副本，换成改签后的区段
                connection.execute(
                    "DELETE FROM chain_segments WHERE chain_id=? AND version=? AND seq>=?",
                    (chain_id, version, from_seq),
                )
                for offset, spec in enumerate(specs):
                    seq = from_seq + offset
                    self._insert_segment_row(connection, chain=chain, version=version, spec=spec,
                                             seq=seq, state="proposed")
                    self._raise_escalation(connection, chain=chain, segment_seq=seq,
                                           kind="acceptance_due", level=1,
                                           deadline_at=self._soon(ACCEPT_DEADLINE_MINUTES),
                                           detail={"reason": "rebook"}, version=version)
                new_count = from_seq - 1 + len(specs)
                # 保留段（含在途段）的锁从旧版本源区段迁移
                for seq in range(1, from_seq):
                    source_seg = connection.execute(
                        "SELECT * FROM chain_segments WHERE chain_id=? AND version=? AND seq=?",
                        (chain_id, version - 1, seq),
                    ).fetchone()
                    new_seg = self._version_segment(connection, chain, version, seq, must_exist=False)
                    if new_seg is not None and source_seg["state"] in {"accepted", "in_progress"}:
                        self._migrate_locks(connection, chain, old_segment=source_seg,
                                            new_segment=new_seg, version=version)
                self._release_superseded_locks(connection, chain, old_version=version - 1)
                connection.execute(
                    "UPDATE assistance_chains SET segment_count=? WHERE chain_id=?",
                    (new_count, chain_id),
                )
                # 新版本复制的交接点在新路线范围内全部失效，按新区段重建；
                # 边界处若已完成（immutable）则保留原始交接记录。
                connection.execute(
                    "DELETE FROM handovers WHERE chain_id=? AND version=? AND boundary_seq>=? "
                    "AND state!='completed'",
                    (chain_id, version, from_seq - 1),
                )
                self._rebuild_handovers(connection, chain_id=chain_id, version=version)
                self._audit(connection, actor_id=actor_id, action="chain.rebooked",
                            resource_id=chain_id,
                            detail={"version": version, "from_seq": from_seq,
                                    "segment_count": new_count, "reason": reason})
                return "assistance_chain", chain_id, {"version": version, "change": "rebook",
                                                       "segment_count": new_count}

            return self._idempotent(connection, request_id=request_id,
                                    action="rebook", payload=payload, create=create)

    # ---- 旅客失约 --------------------------------------------------------
    def mark_no_show(self, *, request_id: str, actor_id: str, chain_id: str,
                     boundary_seq: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "chain_id": chain_id, "boundary_seq": boundary_seq}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            _replay = self._replay_if_exists(connection, request_id, "mark_no_show", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            if chain["status"] != "active":
                raise ConflictError(f"服务链状态 {chain['status']} 不能登记失约")
            handover = self._current_handover(connection, chain, boundary_seq)
            if handover["state"] not in {"pending", "awaiting_incoming"}:
                raise ConflictError("该交接点已结束，不能登记失约")
            if actor.role not in {"admin", "coordinator", "dispatcher"}:
                raise PermissionDenied("只有协调员或值班员可登记失约")
            if actor.role == "dispatcher":
                outgoing = self._current_segment(connection, chain, boundary_seq)
                incoming = self._current_segment(connection, chain, boundary_seq + 1)
                if actor.organization_id not in {outgoing["organization_id"], incoming["organization_id"]}:
                    raise PermissionDenied("只能登记本单位相关交接点的失约")

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE assistance_chains SET status='no_show' WHERE chain_id=?", (chain_id,),
                )
                connection.execute(
                    "UPDATE chain_segments SET state='cancelled' WHERE chain_id=? AND version=? "
                    "AND seq>=? AND state IN ('proposed','accepted','resourcing')",
                    (chain_id, chain["current_version"], boundary_seq + 1),
                )
                connection.execute(
                    "UPDATE handovers SET state='cancelled' WHERE chain_id=? AND version=? "
                    "AND boundary_seq>=?",
                    (chain_id, chain["current_version"], boundary_seq),
                )
                connection.execute(
                    "UPDATE resource_locks SET state='released',released_at=? "
                    "WHERE chain_id=? AND version=? AND state='locked' AND segment_id IN "
                    "(SELECT segment_id FROM chain_segments WHERE chain_id=? AND version=? "
                    "AND state='cancelled')",
                    (now, chain_id, chain["current_version"], chain_id, chain["current_version"]),
                )
                self._raise_escalation(connection, chain=chain, segment_seq=boundary_seq + 1,
                                       kind="passenger_no_show", level=2,
                                       deadline_at=self._soon(NO_SHOW_DECISION_MINUTES),
                                       detail={"boundary_seq": boundary_seq})
                self._audit(connection, actor_id=actor_id, action="chain.no_show",
                            resource_id=chain_id,
                            detail={"version": chain["current_version"],
                                    "boundary": boundary_seq})
                return "assistance_chain", chain_id, {"status": "no_show"}

            return self._idempotent(connection, request_id=request_id,
                                    action="mark_no_show", payload=payload, create=create)

    def resume_after_no_show(self, *, request_id: str, actor_id: str, chain_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "chain_id": chain_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "coordinator")
            _replay = self._replay_if_exists(connection, request_id, "resume_after_no_show", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            if chain["status"] != "no_show":
                raise ConflictError("仅失约挂起的服务链可恢复")

            def create():
                version = self._new_version(connection, chain=chain, reason="no_show_resume",
                                            trigger=actor.actor_id)
                # 新版本中取消态副本回到 proposed，等待各单位按新时刻重新承接
                connection.execute(
                    "UPDATE chain_segments SET state='proposed',assigned_actor_id=NULL,"
                    "accepted_at=NULL WHERE chain_id=? AND version=? AND state='cancelled'",
                    (chain_id, version),
                )
                connection.execute(
                    "UPDATE handovers SET state='pending',outgoing_actor_id=NULL,"
                    "incoming_actor_id=NULL,outgoing_confirmed_at=NULL,incoming_confirmed_at=NULL,"
                    "completed_at=NULL WHERE chain_id=? AND version=? AND state='cancelled'",
                    (chain_id, version),
                )
                connection.execute(
                    "UPDATE assistance_chains SET status='active' WHERE chain_id=?", (chain_id,),
                )
                self._resolve_escalations(connection, chain, kind="passenger_no_show")
                for row in connection.execute(
                    "SELECT seq FROM chain_segments WHERE chain_id=? AND version=? AND state='proposed'",
                    (chain_id, version),
                ):
                    self._raise_escalation(connection, chain=chain, segment_seq=row["seq"],
                                           kind="acceptance_due", level=1,
                                           deadline_at=self._soon(ACCEPT_DEADLINE_MINUTES),
                                           detail={"reason": "no_show_resume"},
                                           version=version)
                self._audit(connection, actor_id=actor_id, action="chain.resumed",
                            resource_id=chain_id, detail={"version": version})
                return "assistance_chain", chain_id, {"version": version, "status": "active"}

            return self._idempotent(connection, request_id=request_id,
                                    action="resume_after_no_show", payload=payload, create=create)

    # ---- 紧急人工接管 ----------------------------------------------------
    def emergency_takeover(self, *, request_id: str, actor_id: str, chain_id: str,
                           reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "chain_id": chain_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            _replay = self._replay_if_exists(connection, request_id, "emergency_takeover", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            if chain["status"] == "emergency":
                raise ConflictError("服务链已处于紧急接管状态")
            if chain["status"] in {"completed", "cancelled"}:
                raise ConflictError("已结束的服务链不能紧急接管")
            if not reason or not str(reason).strip():
                raise ValidationError("reason 不能为空")

            def create():
                connection.execute(
                    "UPDATE assistance_chains SET status='emergency' WHERE chain_id=?", (chain_id,),
                )
                now = self._now()
                self._raise_escalation(connection, chain=chain, segment_seq=None,
                                       kind="emergency", level=3, deadline_at=now,
                                       detail={"reason": reason, "reporter": actor.actor_id})
                self._audit(connection, actor_id=actor_id, action="chain.emergency",
                            resource_id=chain_id, detail={"reason": reason})
                return "assistance_chain", chain_id, {"status": "emergency", "escalation_level": 3}

            return self._idempotent(connection, request_id=request_id,
                                    action="emergency_takeover", payload=payload, create=create)

    def assign_emergency_owner(self, *, request_id: str, actor_id: str, chain_id: str,
                               owner_actor_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "chain_id": chain_id, "owner_actor_id": owner_actor_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "coordinator")
            _replay = self._replay_if_exists(connection, request_id, "assign_emergency_owner", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            if chain["status"] != "emergency":
                raise ConflictError("仅紧急接管中的服务链可指派现场负责人")
            owner = self._actor(connection, owner_actor_id)

            def create():
                connection.execute(
                    "UPDATE escalation_events SET owner_actor_id=?,status='acknowledged' "
                    "WHERE chain_id=? AND version=? AND kind='emergency' AND status IN ('open','acknowledged')",
                    (owner_actor_id, chain_id, chain["current_version"]),
                )
                self._audit(connection, actor_id=actor_id, action="emergency.owner_assigned",
                            resource_id=chain_id, detail={"owner_actor_id": owner_actor_id,
                                                          "owner_organization_id": owner.organization_id})
                return "assistance_chain", chain_id, {"owner_actor_id": owner_actor_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="assign_emergency_owner", payload=payload, create=create)

    def resolve_emergency(self, *, request_id: str, actor_id: str, chain_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "chain_id": chain_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "coordinator")
            _replay = self._replay_if_exists(connection, request_id, "resolve_emergency", payload)
            if _replay is not None:
                return _replay
            chain = self._load_chain(connection, chain_id)
            if chain["status"] != "emergency":
                raise ConflictError("服务链不处于紧急接管状态")

            def create():
                connection.execute(
                    "UPDATE assistance_chains SET status='active' WHERE chain_id=?", (chain_id,),
                )
                self._resolve_escalations(connection, chain, kind="emergency")
                self._audit(connection, actor_id=actor_id, action="emergency.resolved",
                            resource_id=chain_id, detail={})
                return "assistance_chain", chain_id, {"status": "active"}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_emergency", payload=payload, create=create)

    # ---- 最小披露：按需取阅并留痕 ----------------------------------------
    def access_segment_needs(self, *, actor_id: str, chain_id: str,
                             segment_seq: int) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            chain = self._load_chain(connection, chain_id)
            segment = self._current_segment(connection, chain, segment_seq)
            if actor.role not in {"admin", "coordinator"} and actor.organization_id != segment["organization_id"]:
                raise PermissionDenied("只能取阅本单位承接区段的需求")
            if actor.role == "worker" and segment["assigned_actor_id"] not in (None, actor.actor_id):
                raise PermissionDenied("只有被指派的现场责任人可取阅需求明细")
            items = self._visible_need_rows(connection, chain_id, segment_seq)
            now = self._now()
            connection.execute(
                "INSERT INTO need_disclosures(disclosure_id,chain_id,version,segment_seq,actor_id,"
                "organization_id,item_ids_json,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, chain_id, chain["current_version"], segment_seq, actor.actor_id,
                 actor.organization_id, canonical_json([row["code"] for row in items]),
                 "segment_duty", now),
            )
            self._audit(connection, actor_id=actor_id, action="needs.accessed",
                        resource_id=chain_id,
                        detail={"version": chain["current_version"], "seq": segment_seq,
                                "item_codes": [row["code"] for row in items]})
            return {"chain_id": chain_id, "version": chain["current_version"],
                    "segment_seq": segment_seq,
                    "items": [{"code": row["code"], "detail": row["detail"],
                               "sensitivity": row["sensitivity"]} for row in items]}

    def _visible_need_rows(self, connection, chain_id: str, segment_seq: int):
        rows = connection.execute(
            "SELECT * FROM need_items WHERE chain_id=?", (chain_id,)
        ).fetchall()
        visible = []
        for row in rows:
            if row["visible_scope"] == "all" or segment_seq in set(json.loads(row["visible_segments_json"])):
                visible.append(row)
        return visible

    def passenger_view(self, *, chain_id: str, passenger_token: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            chain = self._load_chain(connection, chain_id)
            if chain["passenger_token_hash"] != _hash_token(passenger_token or ""):
                raise PermissionDenied("旅客核验令牌不匹配")
            return self._chain_detail(connection, chain, viewer="passenger")

    def coordinator_view(self, *, actor_id: str, chain_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "coordinator")
            chain = self._load_chain(connection, chain_id)
            return self._chain_detail(connection, chain, viewer="coordinator")

    def access_log(self, *, chain_id: str, passenger_token: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            chain = self._load_chain(connection, chain_id)
            if chain["passenger_token_hash"] != _hash_token(passenger_token or ""):
                raise PermissionDenied("旅客核验令牌不匹配")
            entries = []
            query = connection.execute(
                "SELECT d.*, a.display_name AS actor_name FROM need_disclosures d "
                "JOIN actors a ON a.actor_id=d.actor_id WHERE d.chain_id=? ORDER BY d.created_at",
                (chain_id,),
            )
            for row in query:
                entries.append({"at": row["created_at"], "actor_id": row["actor_id"],
                                "actor_name": row["actor_name"],
                                "organization_id": row["organization_id"],
                                "version": row["version"], "segment_seq": row["segment_seq"],
                                "need_codes": json.loads(row["item_ids_json"]),
                                "reason": row["reason"]})
            return {"chain_id": chain_id, "entries": entries}

    # ---- 值守板 ----------------------------------------------------------
    def duty_board(self, *, actor_id: str, organization_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_role(actor, "admin", "coordinator", "dispatcher", "worker")
            self.sweep_timeouts(connection=connection)
            if actor.role not in {"admin", "coordinator"}:
                organization_id = actor.organization_id
            chains = connection.execute(
                "SELECT * FROM assistance_chains WHERE status!='cancelled' ORDER BY created_at"
            ).fetchall()
            items = []
            for chain in chains:
                detail = self._board_chain(connection, chain, organization_id, actor)
                if detail is not None:
                    items.append(detail)
            return {"at": self._now(), "items": items}

    def _board_chain(self, connection, chain, organization_id, actor):
        version = chain["current_version"]
        segments = connection.execute(
            "SELECT * FROM chain_segments WHERE chain_id=? AND version=? ORDER BY seq",
            (chain["chain_id"], version),
        ).fetchall()
        if organization_id and not any(seg["organization_id"] == organization_id for seg in segments):
            return None
        current = next((s for s in segments if s["state"] in {"in_progress", "resourcing"}), None)
        if current is None:
            current = next((s for s in segments if s["state"] in {"accepted", "proposed"}), None)
        handovers = connection.execute(
            "SELECT * FROM handovers WHERE chain_id=? AND version=? ORDER BY boundary_seq",
            (chain["chain_id"], version),
        ).fetchall()
        next_handover = next((h for h in handovers if h["state"] != "completed"), None)
        responsibility = None
        if current is not None:
            assignee = None
            if current["assigned_actor_id"]:
                row = connection.execute(
                    "SELECT display_name FROM actors WHERE actor_id=?",
                    (current["assigned_actor_id"],),
                ).fetchone()
                assignee = row["display_name"] if row else None
            responsibility = {"segment_seq": current["seq"],
                              "organization_id": current["organization_id"],
                              "service_kind": current["service_kind"],
                              "actor_id": current["assigned_actor_id"],
                              "actor_name": assignee, "state": current["state"]}
        open_escalations = connection.execute(
            "SELECT kind,level,status,deadline_at,owner_actor_id FROM escalation_events "
            "WHERE chain_id=? AND version=? AND status!='resolved' ORDER BY level DESC, created_at",
            (chain["chain_id"], version),
        ).fetchall()
        return {
            "chain_id": chain["chain_id"],
            "passenger_ref": chain["passenger_ref"],
            "status": chain["status"],
            "version": version,
            "current_responsibility": responsibility,
            "next_handover": self._handover_risk(connection, chain, next_handover, current),
            "readiness": {"accepted_or_done": sum(1 for s in segments
                                                  if s["state"] in {"accepted", "in_progress", "completed"}),
                          "total": len(segments)},
            "escalations": [{"kind": row["kind"], "level": row["level"],
                             "status": row["status"], "deadline_at": row["deadline_at"],
                             "owner_actor_id": row["owner_actor_id"]} for row in open_escalations],
            # 仅暴露本单位可见的需求代码，绝不包含健康明细
            "need_codes_for_my_unit": self._need_codes_for_org(connection, chain, segments, organization_id),
        }

    def _need_codes_for_org(self, connection, chain, segments, organization_id):
        if not organization_id:
            return []
        codes = set()
        for seg in segments:
            if seg["organization_id"] != organization_id:
                continue
            for row in self._visible_need_rows(connection, chain["chain_id"], seg["seq"]):
                codes.add(row["code"])
        return sorted(codes)

    def _handover_risk(self, connection, chain, handover, current_segment):
        if handover is None:
            return None
        now_dt = self.clock.now()
        from datetime import datetime
        deadline = datetime.fromisoformat(handover["scheduled_at"].replace("Z", "+00:00"))
        seconds_remaining = int((deadline - now_dt).total_seconds())
        grace = deadline + timedelta(minutes=HANDOVER_GRACE_MINUTES)
        level = "none"
        if handover["state"] in {"superseded", "cancelled"}:
            level = "inactive"
        elif handover["state"] == "awaiting_incoming":
            incoming_deadline = datetime.fromisoformat(
                (handover["outgoing_confirmed_at"] or handover["scheduled_at"]).replace("Z", "+00:00")
            ) + timedelta(minutes=INCOMING_GRACE_MINUTES)
            if now_dt >= incoming_deadline:
                level = "overdue"
            else:
                level = "imminent"
        elif now_dt >= grace:
            level = "overdue"
        elif now_dt >= deadline - timedelta(minutes=BOARD_IMMINENT_MINUTES):
            level = "imminent"
        incoming = connection.execute(
            "SELECT assigned_actor_id,organization_id FROM chain_segments "
            "WHERE chain_id=? AND version=? AND seq=?",
            (chain["chain_id"], chain["current_version"], handover["boundary_seq"] + 1),
        ).fetchone()
        return {"boundary_seq": handover["boundary_seq"], "location": handover["location"],
                "scheduled_at": handover["scheduled_at"], "state": handover["state"],
                "outgoing_actor_id": handover["outgoing_actor_id"],
                "incoming_actor_id": handover["incoming_actor_id"]
                or (incoming["assigned_actor_id"] if incoming else None),
                "incoming_organization_id": incoming["organization_id"] if incoming else None,
                "seconds_remaining": seconds_remaining, "timeout_risk": level}

    # ---- 超时与升级的确定性扫描 ------------------------------------------
    def sweep_timeouts(self, *, connection=None) -> int:
        """检查当前版本所有交接与待承接区段，按规则生成/升级事件，返回事件数。"""

        @contextmanager
        def owned_transaction():
            with self.database.transaction(immediate=True) as conn:
                yield conn

        context = nullcontext(connection) if connection is not None else owned_transaction()
        created = 0
        with context as conn:
            now = self._now()
            chains = conn.execute(
                "SELECT * FROM assistance_chains WHERE status IN ('active','no_show')"
            ).fetchall()
            for chain in chains:
                version = chain["current_version"]
                for handover in conn.execute(
                    "SELECT * FROM handovers WHERE chain_id=? AND version=? "
                    "AND state IN ('pending','awaiting_incoming')",
                    (chain["chain_id"], version),
                ):
                    kind: str | None = None
                    if handover["state"] == "awaiting_incoming":
                        due = self._shift(handover["outgoing_confirmed_at"]
                                          or handover["scheduled_at"], INCOMING_GRACE_MINUTES)
                        if now >= due:
                            kind = "handover_incoming_missing"
                    else:
                        due = self._shift(handover["scheduled_at"], HANDOVER_GRACE_MINUTES)
                        if now >= due:
                            kind = "handover_overdue"
                    if kind is None:
                        continue
                    if self._raise_escalation(conn, chain=chain,
                                              segment_seq=handover["boundary_seq"] + 1,
                                              kind=kind, level=1, deadline_at=now,
                                              detail={"boundary_seq": handover["boundary_seq"]}):
                        created += 1
                    hard_due = self._shift(handover["scheduled_at"],
                                           HANDOVER_GRACE_MINUTES + LEVEL2_LAG_MINUTES)
                    if now >= hard_due and self._raise_escalation(
                        conn, chain=chain, segment_seq=handover["boundary_seq"] + 1,
                        kind=kind, level=2, deadline_at=now,
                        detail={"boundary_seq": handover["boundary_seq"]}
                    ):
                        created += 1
                for segment in conn.execute(
                    "SELECT * FROM chain_segments WHERE chain_id=? AND version=? "
                    "AND state IN ('proposed','resourcing')",
                    (chain["chain_id"], version),
                ):
                    kind = "equipment_fault" if segment["state"] == "resourcing" else "acceptance_due"
                    open_row = conn.execute(
                        "SELECT * FROM escalation_events WHERE chain_id=? AND version=? "
                        "AND segment_seq=? AND kind=? AND status!='resolved' ORDER BY level DESC",
                        (chain["chain_id"], version, segment["seq"], kind),
                    ).fetchone()
                    if open_row is None:
                        minutes = EQUIPMENT_FIX_MINUTES if kind == "equipment_fault" else ACCEPT_DEADLINE_MINUTES
                        if self._raise_escalation(conn, chain=chain, segment_seq=segment["seq"],
                                                  kind=kind, level=1, deadline_at=self._soon(minutes),
                                                  detail={"state": segment["state"]}):
                            created += 1
                    elif open_row["level"] == 1 and now >= open_row["deadline_at"]:
                        if self._raise_escalation(conn, chain=chain, segment_seq=segment["seq"],
                                                  kind=kind, level=2, deadline_at=now,
                                                  detail={"state": segment["state"]}):
                            created += 1
                if chain["status"] == "active":
                    first = conn.execute(
                        "SELECT * FROM chain_segments WHERE chain_id=? AND version=? AND seq=1",
                        (chain["chain_id"], version),
                    ).fetchone()
                    if first and first["state"] == "accepted" and now >= self._shift(
                        first["scheduled_start"], START_GRACE_MINUTES
                    ):
                        if self._raise_escalation(conn, chain=chain, segment_seq=1,
                                                  kind="start_overdue", level=1, deadline_at=now,
                                                  detail={}):
                            created += 1
        return created

    def _raise_escalation(self, connection, *, chain, segment_seq, kind, level,
                          deadline_at, detail, version=None) -> bool:
        """创建升级事件；同级已存在则跳过，存在更低级开放事件时升级。返回是否新建。"""

        version = version or chain["current_version"]
        existing = connection.execute(
            "SELECT * FROM escalation_events WHERE chain_id=? AND version=? AND kind=? "
            "AND status!='resolved' ORDER BY level DESC",
            (chain["chain_id"], version, kind),
        ).fetchall()
        if existing and existing[0]["level"] >= level:
            return False
        for row in existing:
            connection.execute(
                "UPDATE escalation_events SET status='resolved',resolved_at=? WHERE escalation_id=?",
                (self._now(), row["escalation_id"]),
            )
        escalation_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO escalation_events(escalation_id,chain_id,version,segment_seq,level,kind,"
            "status,deadline_at,detail_json,created_at) VALUES(?,?,?,?,?,?,'open',?,?,?)",
            (escalation_id, chain["chain_id"], version, segment_seq, level, kind,
             deadline_at, canonical_json(detail), self._now()),
        )
        self._audit(connection, actor_id="system", action=f"escalation.level{level}",
                    resource_id=chain["chain_id"],
                    detail={"kind": kind, "segment_seq": segment_seq, "version": version,
                            "deadline_at": deadline_at})
        return True

    def _resolve_escalations(self, connection, chain, *, kind: str) -> None:
        connection.execute(
            "UPDATE escalation_events SET status='resolved',resolved_at=? "
            "WHERE chain_id=? AND version=? AND kind=? AND status!='resolved'",
            (self._now(), chain["chain_id"], chain["current_version"], kind),
        )

    def _close_acceptance_escalations(self, connection, chain, segment_seq: int) -> None:
        connection.execute(
            "UPDATE escalation_events SET status='resolved',resolved_at=? "
            "WHERE chain_id=? AND version=? AND segment_seq=? AND kind IN ('acceptance_due','equipment_fault') "
            "AND status!='resolved'",
            (self._now(), chain["chain_id"], chain["current_version"], segment_seq),
        )

    # ---- 设备故障联动 ----------------------------------------------------
    def _apply_equipment_fault(self, connection, *, incident_id, organization_id,
                               handover_point, resource_code, resource_ref) -> None:
        chain_rows = connection.execute(
            "SELECT s.*, c.current_version FROM resource_locks l "
            "JOIN chain_segments s ON s.segment_id=l.segment_id "
            "JOIN assistance_chains c ON c.chain_id=s.chain_id "
            "WHERE l.resource_ref=? AND l.resource_code=? AND l.state='locked' "
            "AND s.version=c.current_version",
            (resource_ref, resource_code),
        ).fetchall()
        for seg in chain_rows:
            chain = connection.execute(
                "SELECT * FROM assistance_chains WHERE chain_id=?", (seg["chain_id"],)
            ).fetchone()
            connection.execute(
                "UPDATE resource_locks SET state='released',released_at=? WHERE lock_id IN ("
                "SELECT lock_id FROM resource_locks WHERE segment_id=? AND resource_ref=? "
                "AND state='locked')",
                (self._now(), seg["segment_id"], resource_ref),
            )
            required = json.loads(seg["equipment_required_json"])
            substitute = self._match_capability(
                connection, organization_id=organization_id, service_kind=seg["service_kind"],
                points=self._segment_points(seg) | {handover_point},
                start=seg["scheduled_start"],
                end=seg["scheduled_end"], required=[resource_code],
                exclude_segment=seg["segment_id"], broken_ref=resource_ref,
            )
            if substitute is not None and chain["status"] in {"active", "emergency"}:
                new_ref = substitute[resource_code]
                connection.execute(
                    "INSERT INTO resource_locks(lock_id,chain_id,version,segment_id,"
                    "organization_id,resource_code,resource_ref,state,locked_at) "
                    "VALUES(?,?,?,?,?,?,?,'locked',?)",
                    (uuid.uuid4().hex, seg["chain_id"], seg["version"], seg["segment_id"],
                     organization_id, resource_code, new_ref, self._now()),
                )
                self._audit(connection, actor_id="system", action="equipment.substituted",
                            resource_id=incident_id, resource_type="equipment_incident",
                            detail={"chain_id": seg["chain_id"], "seq": seg["seq"],
                                    "resource_code": resource_code,
                                    "old_ref": resource_ref, "new_ref": new_ref})
            else:
                connection.execute(
                    "UPDATE chain_segments SET state='resourcing' WHERE segment_id=?",
                    (seg["segment_id"],),
                )
                self._raise_escalation(connection, chain=chain, segment_seq=seg["seq"],
                                       kind="equipment_fault", level=1,
                                       deadline_at=self._soon(EQUIPMENT_FIX_MINUTES),
                                       detail={"resource_code": resource_code,
                                               "resource_ref": resource_ref})

    def resolve_equipment_incident(self, *, request_id: str, actor_id: str,
                                   incident_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "incident_id": incident_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            _replay = self._replay_if_exists(connection, request_id, "resolve_equipment_incident", payload)
            if _replay is not None:
                return _replay
            incident = connection.execute(
                "SELECT * FROM equipment_incidents WHERE incident_id=?", (incident_id,)
            ).fetchone()
            if incident is None:
                raise NotFoundError("设备故障事件不存在")
            if incident["state"] == "resolved":
                raise ConflictError("故障事件已解决")
            if actor.role not in {"admin", "operator", "dispatcher"}:
                raise PermissionDenied("只有值班员可关闭设备故障")
            if actor.role == "dispatcher" and actor.organization_id != incident["organization_id"]:
                raise PermissionDenied("只能关闭本单位故障事件")

            def create():
                connection.execute(
                    "UPDATE equipment_incidents SET state='resolved',resolved_at=? WHERE incident_id=?",
                    (self._now(), incident_id),
                )
                self._audit(connection, actor_id=actor_id, action="equipment.incident_resolved",
                            resource_id=incident_id, resource_type="equipment_incident",
                            detail={"resource_ref": incident["resource_ref"]})
                return "equipment_incident", incident_id, {"state": "resolved"}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_equipment_incident", payload=payload,
                                    create=create)

    # ---- 版本复制与重排原语 ----------------------------------------------
    def _new_version(self, connection, *, chain, reason: str, trigger: str) -> int:
        version = chain["current_version"] + 1
        connection.execute(
            "INSERT INTO itinerary_versions(chain_id,version,reason,trigger_actor_id,created_at)"
            " VALUES(?,?,?,?,?)",
            (chain["chain_id"], version, reason, trigger, self._now()),
        )
        for row in connection.execute(
            "SELECT * FROM chain_segments WHERE chain_id=? AND version=? ORDER BY seq",
            (chain["chain_id"], chain["current_version"]),
        ):
            immutable = 1 if row["state"] == "completed" else 0
            connection.execute(
                "INSERT INTO chain_segments(segment_id,chain_id,version,seq,service_kind,"
                "organization_id,scheduled_start,scheduled_end,board_location,handover_location,"
                "equipment_required_json,state,assigned_actor_id,accepted_at,completed_at,"
                "immutable,source_segment_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, chain["chain_id"], version, row["seq"], row["service_kind"],
                 row["organization_id"], row["scheduled_start"], row["scheduled_end"],
                 row["board_location"], row["handover_location"], row["equipment_required_json"],
                 row["state"], row["assigned_actor_id"], row["accepted_at"], row["completed_at"],
                 immutable, row["segment_id"], self._now()),
            )
        for row in connection.execute(
            "SELECT * FROM handovers WHERE chain_id=? AND version=? ORDER BY boundary_seq",
            (chain["chain_id"], chain["current_version"]),
        ):
            immutable = 1 if row["state"] == "completed" else 0
            connection.execute(
                "INSERT INTO handovers(handover_id,chain_id,version,boundary_seq,location,"
                "scheduled_at,state,outgoing_actor_id,incoming_actor_id,outgoing_confirmed_at,"
                "incoming_confirmed_at,completed_at,immutable,source_handover_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, chain["chain_id"], version, row["boundary_seq"], row["location"],
                 row["scheduled_at"], row["state"], row["outgoing_actor_id"], row["incoming_actor_id"],
                 row["outgoing_confirmed_at"], row["incoming_confirmed_at"], row["completed_at"],
                 immutable, row["handover_id"]),
            )
        connection.execute(
            "UPDATE assistance_chains SET current_version=? WHERE chain_id=?",
            (version, chain["chain_id"]),
        )
        # 旧版本未完成的区段与交接统一作废旧计划（已完成、已取消的保持原始记录），
        # 其占用的设备锁随即释放，由新版本按重新承接结果再锁定。
        connection.execute(
            "UPDATE chain_segments SET state='superseded' WHERE chain_id=? AND version=? "
            "AND state IN ('proposed','accepted','resourcing','in_progress')",
            (chain["chain_id"], chain["current_version"]),
        )
        connection.execute(
            "UPDATE handovers SET state='superseded' WHERE chain_id=? AND version=? "
            "AND state IN ('pending','awaiting_incoming')",
            (chain["chain_id"], chain["current_version"]),
        )
        # 旧锁是否释放由调用方在迁移（_migrate_locks）或 _release_superseded_locks 中决定；
        # 被 supersede 区段上的锁不会参与当前匹配（_resource_free 仅计 accepted/in_progress）。
        # 旧版本未关闭的升级事件属于旧计划，随版本切换结案；必要时扫描器在新版本重建。
        connection.execute(
            "UPDATE escalation_events SET status='resolved',resolved_at=? "
            "WHERE chain_id=? AND version=? AND status!='resolved'",
            (self._now(), chain["chain_id"], chain["current_version"]),
        )
        return version

    def _release_superseded_locks(self, connection, chain, *, old_version: int) -> None:
        """旧版本未被迁移到新版本的锁定设备全部释放。"""

        connection.execute(
            "UPDATE resource_locks SET state='released',released_at=? "
            "WHERE state='locked' AND released_at IS NULL AND chain_id=? AND version=?",
            (self._now(), chain["chain_id"], old_version),
        )

    def _reset_new_version_handovers(self, connection, *, chain_id: str, version: int,
                                     from_boundary: int) -> None:
        """新版本由 _new_version 复制了交接点；把受影响边界重置为待交接，时间对齐新排定。"""

        segments = {row["seq"]: row for row in connection.execute(
            "SELECT * FROM chain_segments WHERE chain_id=? AND version=?",
            (chain_id, version),
        ).fetchall()}
        for row in connection.execute(
            "SELECT * FROM handovers WHERE chain_id=? AND version=? AND boundary_seq>=? "
            "AND state!='completed' ORDER BY boundary_seq",
            (chain_id, version, from_boundary),
        ).fetchall():
            outgoing = segments.get(row["boundary_seq"])
            scheduled_at = outgoing["scheduled_end"] if outgoing else row["scheduled_at"]
            connection.execute(
                "UPDATE handovers SET state='pending',scheduled_at=?,outgoing_actor_id=NULL,"
                "incoming_actor_id=NULL,outgoing_confirmed_at=NULL,incoming_confirmed_at=NULL,"
                "completed_at=NULL,immutable=0 WHERE handover_id=?",
                (scheduled_at, row["handover_id"]),
            )

    def _migrate_locks(self, connection, chain, *, old_segment, new_segment, version) -> None:
        locks = connection.execute(
            "SELECT * FROM resource_locks WHERE segment_id=? AND state='locked'",
            (old_segment["segment_id"],),
        ).fetchall()
        for lock in locks:
            connection.execute(
                "INSERT INTO resource_locks(lock_id,chain_id,version,segment_id,organization_id,"
                "resource_code,resource_ref,state,locked_at) VALUES(?,?,?,?,?,?,?,'locked',?)",
                (uuid.uuid4().hex, chain["chain_id"], version, new_segment["segment_id"],
                 lock["organization_id"], lock["resource_code"], lock["resource_ref"],
                 lock["locked_at"]),
            )
            # 旧版本锁行仅作历史，释放以免与当前计划重复占位
            connection.execute(
                "UPDATE resource_locks SET state='released',released_at=? WHERE lock_id=?",
                (self._now(), lock["lock_id"]),
            )

    def _insert_segment_row(self, connection, *, chain, version, spec, seq, state) -> str:
        segment_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO chain_segments(segment_id,chain_id,version,seq,service_kind,"
            "organization_id,scheduled_start,scheduled_end,board_location,handover_location,"
            "equipment_required_json,state,immutable,source_segment_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?)",
            (segment_id, chain["chain_id"], version, seq, spec["service_kind"],
             spec["organization_id"], spec["scheduled_start"], spec["scheduled_end"],
             spec["board_location"], spec["handover_location"],
             canonical_json(spec["equipment_required"]), state, self._now()),
        )
        return segment_id

    def _rebuild_handovers(self, connection, *, chain_id: str, version: int) -> None:
        segments = connection.execute(
            "SELECT * FROM chain_segments WHERE chain_id=? AND version=? ORDER BY seq",
            (chain_id, version),
        ).fetchall()
        for outgoing, incoming in zip(segments, segments[1:]):
            exists = connection.execute(
                "SELECT 1 FROM handovers WHERE chain_id=? AND version=? AND boundary_seq=?",
                (chain_id, version, outgoing["seq"]),
            ).fetchone()
            if exists:
                continue
            location = outgoing["handover_location"] or incoming["board_location"]
            connection.execute(
                "INSERT INTO handovers(handover_id,chain_id,version,boundary_seq,location,"
                "scheduled_at,state) VALUES(?,?,?,?,?,?,'pending')",
                (uuid.uuid4().hex, chain_id, version, outgoing["seq"], location,
                 outgoing["scheduled_end"]),
            )

    # ---- 能力匹配与锁冲突 ------------------------------------------------
    def _match_capability(self, connection, *, organization_id, service_kind, points,
                          start, end, required, exclude_segment=None, broken_ref=None):
        capabilities = connection.execute(
            "SELECT * FROM capability_declarations WHERE organization_id=? AND service_kind=? "
            "AND active=1 AND window_start<=? AND window_end>=? ORDER BY window_start",
            (organization_id, service_kind, start, end),
        ).fetchall()
        points = set(points)
        for capability in capabilities:
            equipment = json.loads(capability["equipment_json"])
            by_code: dict[str, list[str]] = {}
            for item in equipment:
                by_code.setdefault(item["code"], []).append(item["ref"])
            if not all(code in by_code for code in required):
                continue
            # 单位必须在区段任一接触点（登车点/交接点）具备该设备
            if points and capability["handover_point"] not in points:
                continue
            chosen: dict[str, str] = {}
            feasible = True
            for code in required:
                pick = None
                for ref in sorted(by_code[code]):
                    if ref == broken_ref:
                        continue
                    if self._resource_broken(connection, organization_id=organization_id,
                                             resource_code=code, resource_ref=ref):
                        continue
                    if self._resource_free(connection, resource_ref=ref, start=start, end=end,
                                           exclude_segment=exclude_segment):
                        pick = ref
                        break
                if pick is None:
                    feasible = False
                    break
                chosen[code] = pick
            if feasible:
                return chosen
        return None

    def _resource_broken(self, connection, *, organization_id, resource_code,
                         resource_ref) -> bool:
        return connection.execute(
            "SELECT 1 FROM equipment_incidents WHERE organization_id=? AND resource_code=? "
            "AND resource_ref=? AND state='open'",
            (organization_id, resource_code, resource_ref),
        ).fetchone() is not None

    def _resource_free(self, connection, *, resource_ref, start, end, exclude_segment=None) -> bool:
        query = (
            "SELECT 1 FROM resource_locks l JOIN chain_segments s ON s.segment_id=l.segment_id "
            "WHERE l.resource_ref=? AND l.state='locked' AND l.released_at IS NULL "
            "AND s.scheduled_start < ? AND s.scheduled_end > ? AND s.state IN ('accepted','in_progress')"
        )
        params: list[Any] = [resource_ref, end, start]
        if exclude_segment:
            query += " AND l.segment_id != ?"
            params.append(exclude_segment)
        return connection.execute(query, params).fetchone() is None

    # ---- 查询与序列化 ----------------------------------------------------
    def _load_chain(self, connection, chain_id: str):
        chain = connection.execute(
            "SELECT * FROM assistance_chains WHERE chain_id=?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise NotFoundError("服务链不存在")
        return chain

    def _require_mutable_chain(self, chain) -> None:
        if chain["status"] == "emergency":
            raise ConflictError("服务链处于紧急人工接管，现场操作暂停")
        if chain["status"] in {"completed", "cancelled"}:
            raise ConflictError("服务链已结束，不可变更")

    def _current_segment(self, connection, chain, seq: int):
        segment = connection.execute(
            "SELECT * FROM chain_segments WHERE chain_id=? AND version=? AND seq=?",
            (chain["chain_id"], chain["current_version"], seq),
        ).fetchone()
        if segment is None:
            raise NotFoundError(f"当前版本第 {seq} 段不存在")
        if segment["immutable"]:
            raise ConflictError("已完成区段保持原始记录，不可变更")
        return segment

    def _version_segment(self, connection, chain, version, seq, must_exist=True):
        row = connection.execute(
            "SELECT * FROM chain_segments WHERE chain_id=? AND version=? AND seq=?",
            (chain["chain_id"], version, seq),
        ).fetchone()
        if row is None and must_exist:
            raise NotFoundError(f"新版本第 {seq} 段缺失")
        return row

    def _current_handover(self, connection, chain, boundary_seq: int):
        handover = connection.execute(
            "SELECT * FROM handovers WHERE chain_id=? AND version=? AND boundary_seq=?",
            (chain["chain_id"], chain["current_version"], boundary_seq),
        ).fetchone()
        if handover is None:
            raise NotFoundError(f"当前版本交接点 {boundary_seq} 不存在")
        return handover

    def _require_segment_actor(self, connection, actor: Actor, segment) -> None:
        if actor.role == "admin":
            return
        if actor.organization_id != segment["organization_id"]:
            raise PermissionDenied("只能操作本单位区段")
        if actor.role == "worker" and segment["assigned_actor_id"] != actor.actor_id:
            raise PermissionDenied("只有被指派的现场责任人可执行该操作")

    def _segment_points(self, segment) -> set[str]:
        points = {segment["board_location"]}
        if segment["handover_location"]:
            points.add(segment["handover_location"])
        return points

    def completed_history(self, *, chain_id: str, passenger_token: str | None = None,
                          actor_id: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            chain = self._load_chain(connection, chain_id)
            if passenger_token is not None:
                if chain["passenger_token_hash"] != _hash_token(passenger_token or ""):
                    raise PermissionDenied("旅客核验令牌不匹配")
            elif actor_id is not None:
                actor = self._actor(connection, actor_id)
                if actor.role not in {"admin", "coordinator"}:
                    raise PermissionDenied("只有协调员可查看全量历史")
            else:
                raise PermissionDenied("缺少访问凭证")
            rows = connection.execute(
                "SELECT * FROM chain_segments s WHERE s.chain_id=? AND s.state='completed' "
                "AND NOT EXISTS (SELECT 1 FROM chain_segments p "
                "WHERE p.segment_id=s.source_segment_id AND p.state='completed') "
                "ORDER BY s.completed_at, s.version, s.seq",
                (chain_id,),
            ).fetchall()
            handovers = connection.execute(
                "SELECT * FROM handovers h WHERE h.chain_id=? AND h.state='completed' "
                "AND NOT EXISTS (SELECT 1 FROM handovers p "
                "WHERE p.handover_id=h.source_handover_id AND p.state='completed') "
                "ORDER BY h.completed_at, h.version, h.boundary_seq",
                (chain_id,),
            ).fetchall()
            return {"chain_id": chain_id,
                    "segments": [self._segment_json(row) for row in rows],
                    "handovers": [self._handover_json(row) for row in handovers]}

    def _chain_detail(self, connection, chain, *, viewer: str) -> dict[str, Any]:
        versions = connection.execute(
            "SELECT * FROM itinerary_versions WHERE chain_id=? ORDER BY version",
            (chain["chain_id"],),
        ).fetchall()
        all_segments = connection.execute(
            "SELECT * FROM chain_segments WHERE chain_id=? ORDER BY version,seq",
            (chain["chain_id"],),
        ).fetchall()
        all_handovers = connection.execute(
            "SELECT * FROM handovers WHERE chain_id=? ORDER BY version,boundary_seq",
            (chain["chain_id"],),
        ).fetchall()
        needs = connection.execute(
            "SELECT * FROM need_items WHERE chain_id=?", (chain["chain_id"],)
        ).fetchall()
        result = {
            "chain_id": chain["chain_id"],
            "passenger_ref": chain["passenger_ref"],
            "status": chain["status"],
            "current_version": chain["current_version"],
            "segment_count": chain["segment_count"],
            "versions": [{"version": row["version"], "reason": row["reason"],
                          "trigger_actor_id": row["trigger_actor_id"], "created_at": row["created_at"]}
                         for row in versions],
            "segments": [self._segment_json(row) for row in all_segments],
            "handovers": [self._handover_json(row) for row in all_handovers],
            "needs": [{"code": row["code"], "sensitivity": row["sensitivity"],
                       "visibility": row["visible_scope"],
                       "visible_segments": json.loads(row["visible_segments_json"])}
                      for row in needs],
        }
        if viewer == "passenger":
            result["needs"] = [{"code": row["code"], "detail": row["detail"],
                                "sensitivity": row["sensitivity"], "visibility": row["visible_scope"],
                                "visible_segments": json.loads(row["visible_segments_json"])}
                               for row in needs]
        return result

    def _segment_json(self, row) -> dict[str, Any]:
        return {"segment_id": row["segment_id"], "version": row["version"], "seq": row["seq"],
                "service_kind": row["service_kind"], "organization_id": row["organization_id"],
                "scheduled_start": row["scheduled_start"], "scheduled_end": row["scheduled_end"],
                "board_location": row["board_location"],
                "handover_location": row["handover_location"],
                "equipment_required": json.loads(row["equipment_required_json"]),
                "state": row["state"], "assigned_actor_id": row["assigned_actor_id"],
                "accepted_at": row["accepted_at"], "completed_at": row["completed_at"],
                "immutable": bool(row["immutable"]), "source_segment_id": row["source_segment_id"]}

    def _handover_json(self, row) -> dict[str, Any]:
        return {"handover_id": row["handover_id"], "version": row["version"],
                "boundary_seq": row["boundary_seq"], "location": row["location"],
                "scheduled_at": row["scheduled_at"], "state": row["state"],
                "outgoing_actor_id": row["outgoing_actor_id"],
                "incoming_actor_id": row["incoming_actor_id"],
                "outgoing_confirmed_at": row["outgoing_confirmed_at"],
                "incoming_confirmed_at": row["incoming_confirmed_at"],
                "completed_at": row["completed_at"], "immutable": bool(row["immutable"]),
                "source_handover_id": row["source_handover_id"]}

    # ---- 时间工具 --------------------------------------------------------
    @staticmethod
    def _minutes_between(start: str, end: str) -> int:
        from datetime import datetime
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return int((b - a).total_seconds() // 60)

    @staticmethod
    def _shift(value: str, minutes: int) -> str:
        from datetime import datetime
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
