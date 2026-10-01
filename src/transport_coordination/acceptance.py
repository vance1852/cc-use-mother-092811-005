"""运行接续协助平台的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .assistance import AssistanceService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整的铁路→城市客运→站内服务队接续链并核对关键规则。"""

    base = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)

    def iso(hour: int, minute: int = 0) -> str:
        return base.replace(hour=hour, minute=minute).isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(base)
        domain = DomainService(database, clock)
        svc = AssistanceService(database, clock)

        for oid, name in (("ohub", "枢纽管理方"), ("orail", "铁路段"),
                          ("ourban", "城市客运"), ("oteam", "站内服务队")):
            domain.register_organization(request_id=f"org-{oid}", actor_id="bootstrap",
                                         organization_id=oid, name=name)
        domain.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-hub",
                              display_name="枢纽管理员", role="admin", organization_id="ohub")
        domain.register_actor(request_id="req-coord", actor_id="admin-hub", new_actor_id="coord-1",
                              display_name="无障碍协调员", role="coordinator", organization_id="ohub")
        units = (("orail", "rail", "rail"), ("ourban", "urb", "urban"),
                 ("oteam", "team", "station_team"))
        for org, prefix, _ in units:
            domain.register_actor(request_id=f"req-disp-{prefix}", actor_id="admin-hub",
                                  new_actor_id=f"disp-{prefix}", display_name=f"{prefix}值班员",
                                  role="dispatcher", organization_id=org)
            domain.register_actor(request_id=f"req-w-{prefix}", actor_id="admin-hub",
                                  new_actor_id=f"w-{prefix}", display_name=f"{prefix}现场员",
                                  role="worker", organization_id=org)

        # 各单位声明值守能力（交接点、设备、窗口）
        svc.declare_capability(request_id="cap-rail", actor_id="disp-rail",
                               organization_id="orail", service_kind="rail",
                               handover_point="站台直梯口",
                               equipment=[{"code": "wheelchair", "ref": "RW-01"}],
                               window_start=iso(0), window_end=iso(23, 59))
        svc.declare_capability(request_id="cap-urb", actor_id="disp-urb",
                               organization_id="ourban", service_kind="urban",
                               handover_point="换乘大厅服务台",
                               equipment=[{"code": "wheelchair", "ref": "UW-01"}],
                               window_start=iso(0), window_end=iso(23, 59))
        svc.declare_capability(request_id="cap-team", actor_id="disp-team",
                               organization_id="oteam", service_kind="station_team",
                               handover_point="换乘大厅服务台",
                               equipment=[{"code": "wheelchair", "ref": "TW-01"}],
                               window_start=iso(0), window_end=iso(23, 59))

        # 旅客提交服务链与最小披露需求
        chain = svc.create_chain(
            request_id="req-chain", actor_id="coord-1", passenger_ref="P-轮椅旅客-01",
            passenger_token="passenger-secret-01",
            segments=[
                {"organization_id": "orail", "service_kind": "rail",
                 "board_location": "列车3车", "handover_location": "站台直梯口",
                 "scheduled_start": iso(8), "scheduled_end": iso(8, 30),
                 "equipment_required": ["wheelchair"]},
                {"organization_id": "ourban", "service_kind": "urban",
                 "board_location": "站台直梯口", "handover_location": "换乘大厅服务台",
                 "scheduled_start": iso(8, 30), "scheduled_end": iso(9),
                 "equipment_required": ["wheelchair"]},
                {"organization_id": "oteam", "service_kind": "station_team",
                 "board_location": "换乘大厅服务台",
                 "scheduled_start": iso(9), "scheduled_end": iso(9, 20),
                 "equipment_required": ["wheelchair"]},
            ],
            needs=[
                {"code": "wheelchair_user", "detail": "全程轮椅使用者", "sensitivity": "general"},
                {"code": "oxygen", "detail": "携氧，避免长距离步行", "sensitivity": "health",
                 "visibility": "legs", "segments": [1, 2]},
            ])
        chain_id = chain["chain_id"]

        # 各段接受后锁定资源
        for seq, prefix in ((1, "rail"), (2, "urb"), (3, "team")):
            svc.accept_segment(request_id=f"req-accept-{seq}", actor_id=f"disp-{prefix}",
                               chain_id=chain_id, segment_seq=seq,
                               assignee_actor_id=f"w-{prefix}")
        svc.start_segment(request_id="req-start-1", actor_id="w-rail",
                          chain_id=chain_id, segment_seq=1)
        svc.handover_arrive(request_id="req-arr-1", actor_id="w-rail",
                            chain_id=chain_id, boundary_seq=1)
        svc.handover_receive(request_id="req-rcv-1", actor_id="w-urb",
                             chain_id=chain_id, boundary_seq=1)

        # 晚点：在途的第2段设备锁迁移，后续段回到待承接必须重新接受
        clock._value += timedelta(minutes=20)
        svc.report_delay(request_id="req-delay-2", actor_id="coord-1", chain_id=chain_id,
                         segment_seq=2, new_end=iso(9, 20), reason="站通道拥堵")
        svc.accept_segment(request_id="req-reaccept-3", actor_id="disp-team",
                           chain_id=chain_id, segment_seq=3, assignee_actor_id="w-team")
        svc.handover_arrive(request_id="req-arr-2", actor_id="w-urb",
                            chain_id=chain_id, boundary_seq=2)
        # 站内服务队在接手前取阅本段需求（只能看到全程通用项）
        svc.access_segment_needs(actor_id="w-team", chain_id=chain_id, segment_seq=3)
        svc.handover_receive(request_id="req-rcv-2", actor_id="w-team",
                             chain_id=chain_id, boundary_seq=2)

        # 核对最小披露与访问留痕
        access = svc.access_log(chain_id=chain_id, passenger_token="passenger-secret-01")
        team_codes = {code for entry in access["entries"] if entry["actor_id"] == "w-team"
                      for code in entry["need_codes"]}
        history = svc.completed_history(chain_id=chain_id, passenger_token="passenger-secret-01")
        board = svc.duty_board(actor_id="coord-1")
        valid, event_count = domain.verify_audit()

        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "final_version": next(i["version"] for i in board["items"] if i["chain_id"] == chain_id),
            "chain_status": next(i["status"] for i in board["items"] if i["chain_id"] == chain_id),
            "team_sees_health_item": "oxygen" in team_codes,
            "completed_segments": len(history["segments"]),
            "completed_handovers": len(history["handovers"]),
            "immutable_segments": all(s["immutable"] for s in history["segments"]),
            "access_entries": len(access["entries"]),
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = (result["status"] == "ok" and result["audit_valid"]
                and result["chain_status"] == "completed"
                and result["team_sees_health_item"] is False
                and result["completed_segments"] == 3
                and result["completed_handovers"] == 2
                and result["immutable_segments"])
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
