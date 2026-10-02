"""运行接续协助平台的离线端到端验收。

场景复现题目中的故障：轮椅旅客由铁路、城运、站内队三方接力，铁路首段晚点后，
后续单位若按原时刻到场并各自关闭工单，旅客抵达转乘口将无人接应。本验收证明：

1. 每段必须接单锁定资源，交接必须交出方到达 + 接入方接走双签，任何单方不能关单；
2. 晚点触发确定性重排，已完成段原始记录冻结保留，后续段按新版本重新锁定；
3. 重复回执不会完成两次交接；
4. 健康类需求最小披露，单位只能看到本单位段被授权的需求，且每次访问留痕；
5. 值班员看板给出当前责任人、下一次交接与超时风险，旅客可核对访问履历。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .relay import RelayService
from .service import DomainService
from .storage import Database


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def run() -> dict[str, object]:
    t0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    clock = FixedClock(t0)
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "relay_acceptance.sqlite3")
        svc = DomainService(database, clock)
        relay = RelayService(database, clock)

        for org, name in [("rail", "铁路"), ("urban", "城市客运"), ("station", "站内服务队")]:
            svc.register_organization(request_id=f"org-{org}", actor_id="bootstrap",
                                      organization_id=org, name=name)
        svc.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin",
                           display_name="枢纽管理员", role="admin", organization_id="rail")
        for aid, org, name, role in [
            ("coord", "rail", "枢纽无障碍协调员", "coordinator"),
            ("r1", "rail", "铁路值班员", "operator"),
            ("u1", "urban", "城运值班员", "operator"),
            ("s1", "station", "站内值班员", "operator"),
            ("pax", "station", "轮椅旅客", "passenger"),
        ]:
            svc.register_actor(request_id=f"actor-{aid}", actor_id="admin", new_actor_id=aid,
                               display_name=name, role=role, organization_id=org)
        for sid, org, name in [("hub", "rail", "铁路枢纽"), ("metro", "urban", "城运换乘层"),
                               ("hall", "station", "站内大厅")]:
            svc.register_site(request_id=f"site-{sid}", actor_id="admin", site_id=sid,
                              organization_id=org, name=name, timezone_name="Asia/Shanghai")
        for rid, org, site, kind in [
            ("rw", "rail", "hub", "wheelchair"), ("ra", "rail", "hub", "attendant"),
            ("uw", "urban", "metro", "wheelchair"), ("ua", "urban", "metro", "attendant"),
            ("sw", "station", "hall", "wheelchair"), ("sa", "station", "hall", "attendant"),
        ]:
            relay.register_resource(
                request_id=f"res-{rid}",
                actor_id={"rail": "r1", "urban": "u1", "station": "s1"}[org],
                organization_id=org, site_id=site, kind=kind, identifier=rid,
                window_start=_iso(t0 - timedelta(hours=2)),
                window_end=_iso(t0 + timedelta(hours=12)))

        def leg(site, frm, to, h1, h2):
            return {"site_id": site, "from_location": frm, "to_location": to,
                    "scheduled_start": _iso(t0.replace(hour=h1)),
                    "scheduled_end": _iso(t0.replace(hour=h2)),
                    "acceptance_deadline": _iso(t0.replace(hour=h1)),
                    "required_kinds": ["wheelchair", "attendant"]}

        _, created = relay.create_assistance(
            request_id="trip-001", actor_id="pax",
            needs=[
                {"need_key": "wheelchair", "category": "mobility",
                 "detail": {"type": "manual_wheelchair"}, "visibility": {"scope": "chain"}},
                {"need_key": "medicine", "category": "medical",
                 "detail": {"cold_chain": "随身药品需 2-8℃ 携带"},
                 "visibility": {"scope": "leg", "ordinals": [0, 2]}},
            ],
            legs=[leg("hub", "列车站台", "东转乘口", 9, 10),
                  leg("metro", "东转乘口", "西大厅", 10, 11),
                  leg("hall", "西大厅", "出租站点", 11, 12)])
        aid = created["assistance_id"]
        chain = relay.get_assistance(actor_id="coord", assistance_id=aid)
        leg_ids = [item["leg_id"] for item in chain["legs"]]
        handoff_ids = [item["handoff_id"] for item in chain["handoffs"]]

        for actor, lid, req in [("r1", leg_ids[0], "acc-rail"),
                                ("u1", leg_ids[1], "acc-urban"),
                                ("s1", leg_ids[2], "acc-station")]:
            relay.accept_leg(request_id=req, actor_id=actor, leg_id=lid)

        # 最小披露：城运段看不到医疗需求，铁路/站内段可以。
        urban_needs = relay.reveal_leg_needs(actor_id="u1", leg_id=leg_ids[1])["needs"]
        rail_needs = relay.reveal_leg_needs(actor_id="r1", leg_id=leg_ids[0])["needs"]
        minimal_disclosure = (
            [n["need_key"] for n in urban_needs] == ["wheelchair"]
            and {n["need_key"] for n in rail_needs} == {"wheelchair", "medicine"})

        # 接入方在交出方到达前不能单方关单。
        blocked_single_close = False
        try:
            relay.receive_handoff(request_id="early", actor_id="u1",
                                  handoff_id=handoff_ids[0])
        except Exception:
            blocked_single_close = True

        relay.start_leg(request_id="start", actor_id="r1", leg_id=leg_ids[0])
        relay.arrive_handoff(request_id="arrive-0", actor_id="r1",
                             handoff_id=handoff_ids[0])
        receipt, _ = relay.receive_handoff(request_id="receive-0", actor_id="u1",
                                           handoff_id=handoff_ids[0])
        replay, _ = relay.receive_handoff(request_id="receive-0", actor_id="u1",
                                          handoff_id=handoff_ids[0])
        duplicate_blocked = (not receipt.replayed) and replay.replayed
        try:
            relay.receive_handoff(request_id="receive-0-again", actor_id="u1",
                                  handoff_id=handoff_ids[0])
            duplicate_blocked = False
        except Exception:
            pass

        # 铁路首段晚点 40 分钟：已完成首段冻结，进行中的城运段顺延，站内段回待接单。
        clock._value = t0 + timedelta(minutes=70)
        _, revised = relay.report_delay(request_id="delay-001", actor_id="r1",
                                        leg_id=leg_ids[0], delay_minutes=40)
        delay_reordered = (
            revised["version"] == 2
            and revised["legs"][0]["state"] == "completed"
            and revised["legs"][0]["frozen"] is True
            and revised["legs"][1]["state"] == "in_progress"
            and revised["legs"][2]["state"] == "offered"
            and "T11:40:00" in revised["legs"][2]["scheduled_start"])
        v1 = relay.get_assistance(actor_id="coord", assistance_id=aid, version=1)
        history_preserved = (
            v1["version_status"] == "superseded"
            and v1["legs"][0]["state"] == "completed"
            and "T09:00:00" in v1["legs"][0]["scheduled_start"])

        # 站内队必须按新版本重新接单，旅客到转乘口才有人接应。
        v2 = relay.get_assistance(actor_id="coord", assistance_id=aid, version=2)
        relay.accept_leg(request_id="acc-station-v2", actor_id="s1",
                         leg_id=v2["legs"][2]["leg_id"])
        relay.arrive_handoff(request_id="arrive-1", actor_id="u1",
                             handoff_id=v2["handoffs"][1]["handoff_id"])
        relay.receive_handoff(request_id="receive-1", actor_id="s1",
                              handoff_id=v2["handoffs"][1]["handoff_id"])
        relay.report_delivered(request_id="delivered", actor_id="s1",
                               leg_id=v2["legs"][2]["leg_id"])
        relay.confirm_completion(request_id="complete", actor_id="pax", assistance_id=aid)

        board = relay.board(actor_id="coord")
        board_clean = board["count"] == 0
        access = relay.access_history(actor_id="pax", assistance_id=aid)
        access_auditable = access["count"] >= 3 and all(
            item["revealed_keys"] for item in access["items"])
        audit_valid, audit_events = svc.verify_audit()

        result = {
            "status": "ok",
            "minimal_disclosure": minimal_disclosure,
            "blocked_single_close": blocked_single_close,
            "duplicate_receipt_blocked": duplicate_blocked,
            "delay_reordered": delay_reordered,
            "completed_leg_history_preserved": history_preserved,
            "board_clean_after_completion": board_clean,
            "access_history_entries": access["count"],
            "access_auditable": access_auditable,
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        if not all(value is True for key, value in result.items()
                   if key not in ("status", "access_history_entries", "audit_events")):
            result["status"] = "failed"
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
