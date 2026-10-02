"""接续协助平台的领域规则测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError)
from transport_coordination.relay import RelayService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class RelayCase(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        self.clock = FixedClock(self.t0)
        self.db = Database()
        self.svc = DomainService(self.db, self.clock)
        self.relay = RelayService(self.db, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.db.close()

    def _bootstrap(self):
        s = self.svc
        for org, name in [("rail", "铁路"), ("urban", "城运"), ("station", "站内队")]:
            s.register_organization(request_id=f"org-{org}", actor_id="bootstrap",
                                    organization_id=org, name=name)
        s.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                         display_name="管理员", role="admin", organization_id="rail")
        for aid, org, name, role in [
            ("coord", "rail", "协调员", "coordinator"),
            ("r1", "rail", "铁路员甲", "operator"),
            ("r2", "rail", "铁路员乙", "operator"),
            ("u1", "urban", "城运员", "operator"),
            ("s1", "station", "站内员", "operator"),
            ("pax", "station", "旅客", "passenger"),
            ("agent", "station", "授权代理", "operator"),
        ]:
            s.register_actor(request_id=f"actor-{aid}", actor_id="adm", new_actor_id=aid,
                             display_name=name, role=role, organization_id=org)
        for sid, org in [("hub", "rail"), ("metro", "urban"), ("hall", "station")]:
            s.register_site(request_id=f"site-{sid}", actor_id="adm", site_id=sid,
                            organization_id=org, name=sid, timezone_name="Asia/Shanghai")
        for rid, org, site, kind in [
            ("rw", "rail", "hub", "wheelchair"), ("ra", "rail", "hub", "attendant"),
            ("uw", "urban", "metro", "wheelchair"), ("ua", "urban", "metro", "attendant"),
            ("sw", "station", "hall", "wheelchair"), ("sa", "station", "hall", "attendant"),
        ]:
            self.relay.register_resource(
                request_id=f"res-{rid}",
                actor_id={"rail": "r1", "urban": "u1", "station": "s1"}[org],
                organization_id=org, site_id=site, kind=kind, identifier=rid,
                window_start=iso(self.t0 - timedelta(hours=2)),
                window_end=iso(self.t0 + timedelta(hours=12)))

    def leg(self, ordinal):
        hours = [(9, 10), (10, 11), (11, 12)][ordinal]
        sites = [("hub", "站台", "东换乘口"), ("metro", "东换乘口", "西大厅"),
                 ("hall", "西大厅", "出租站点")]
        site, frm, to = sites[ordinal]
        start = self.t0.replace(hour=0) + timedelta(hours=hours[0])
        end = self.t0.replace(hour=0) + timedelta(hours=hours[1])
        return {"site_id": site, "from_location": frm, "to_location": to,
                "scheduled_start": iso(start), "scheduled_end": iso(end),
                "acceptance_deadline": iso(start),
                "required_kinds": ["wheelchair", "attendant"]}

    def create_trip(self, request_id="trip", actor="pax", needs=None, legs=None,
                    passenger=None):
        needs = needs if needs is not None else [
            {"need_key": "wc", "category": "mobility", "detail": {"w": 1},
             "visibility": {"scope": "chain"}},
            {"need_key": "med", "category": "medical", "detail": {"m": 1},
             "visibility": {"scope": "leg", "ordinals": [0, 2]}},
        ]
        legs = legs if legs is not None else [self.leg(i) for i in range(3)]
        _, created = self.relay.create_assistance(
            request_id=request_id, actor_id=actor, passenger_actor_id=passenger,
            needs=needs, legs=legs)
        return created["assistance_id"]

    def ids(self, aid, version=None):
        state = self.relay.get_assistance(actor_id="coord", assistance_id=aid, version=version)
        return [l["leg_id"] for l in state["legs"]], [h["handoff_id"] for h in state["handoffs"]]

    def accept_all(self, aid, actors=("r1", "u1", "s1")):
        leg_ids, _ = self.ids(aid)
        for i, actor in enumerate(actors):
            self.relay.accept_leg(request_id=f"accept-{aid}-{actor}-{i}", actor_id=actor,
                                  leg_id=leg_ids[i])
        return leg_ids

    def run_to_handoff1(self, aid):
        """接单并完成第一次交接，返回 (v1腿, v1交接单)。"""
        leg_ids, handoff_ids = self.ids(aid)
        self.accept_all(aid)
        self.relay.start_leg(request_id=f"start-{aid}", actor_id="r1", leg_id=leg_ids[0])
        self.relay.arrive_handoff(request_id=f"arrive-{aid}", actor_id="r1",
                                  handoff_id=handoff_ids[0])
        self.relay.receive_handoff(request_id=f"recv-{aid}", actor_id="u1",
                                   handoff_id=handoff_ids[0])
        return leg_ids, handoff_ids


class ChainTest(RelayCase):
    def test_each_leg_must_accept_before_locking(self):
        aid = self.create_trip()
        leg_ids, _ = self.ids(aid)
        # 未接单不能开始
        with self.assertRaises(ConflictError):
            self.relay.start_leg(request_id="x", actor_id="r1", leg_id=leg_ids[0])
        self.relay.accept_leg(request_id="a0", actor_id="r1", leg_id=leg_ids[0])
        # 别的单位不能接不属于自己的段
        with self.assertRaises(PermissionDenied):
            self.relay.accept_leg(request_id="a1-wrong", actor_id="s1", leg_id=leg_ids[1])
        self.relay.accept_leg(request_id="a1", actor_id="u1", leg_id=leg_ids[1])
        self.relay.accept_leg(request_id="a2", actor_id="s1", leg_id=leg_ids[2])

    def test_inbound_cannot_close_handoff_alone(self):
        aid = self.create_trip()
        leg_ids, handoff_ids = self.ids(aid)
        self.accept_all(aid)
        self.relay.start_leg(request_id="st", actor_id="r1", leg_id=leg_ids[0])
        with self.assertRaises(ConflictError):
            self.relay.receive_handoff(request_id="early", actor_id="u1",
                                       handoff_id=handoff_ids[0])

    def test_duplicate_receipt_does_not_complete_handoff_twice(self):
        aid = self.create_trip()
        leg_ids, handoff_ids = self.run_to_handoff1(aid)
        # 相同 request_id 重放：回放原始回执，不产生第二次状态变化
        replay, _ = self.relay.receive_handoff(request_id=f"recv-{aid}", actor_id="u1",
                                               handoff_id=handoff_ids[0])
        self.assertTrue(replay.replayed)
        state = self.relay.get_assistance(actor_id="coord", assistance_id=aid)
        self.assertEqual("completed", state["handoffs"][0]["state"])
        # 新 request_id 也不能再次完成同一交接
        with self.assertRaises(ConflictError):
            self.relay.receive_handoff(request_id="recv-again", actor_id="u1",
                                       handoff_id=handoff_ids[0])

    def test_accept_is_idempotent(self):
        aid = self.create_trip()
        leg_ids, _ = self.ids(aid)
        first, body1 = self.relay.accept_leg(request_id="acc", actor_id="r1", leg_id=leg_ids[0])
        replay, body2 = self.relay.accept_leg(request_id="acc", actor_id="r1", leg_id=leg_ids[0])
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(body1["locked_resources"], body2["locked_resources"])
        # 资源只被锁一次
        locked = self.db.connection.execute(
            "SELECT COUNT(*) c FROM assist_resources WHERE locked_leg_id=?", (leg_ids[0],)).fetchone()
        self.assertEqual(len(body1["locked_resources"]), locked["c"])

    def test_same_org_handoff_requires_two_staff(self):
        # 两段同属铁路：同一人不能既送达又接走
        legs = [
            {"site_id": "hub", "from_location": "A", "to_location": "B",
             "scheduled_start": iso(self.t0.replace(hour=9)), "scheduled_end": iso(self.t0.replace(hour=10)),
             "acceptance_deadline": iso(self.t0.replace(hour=9)),
             "required_kinds": ["wheelchair", "attendant"]},
            {"site_id": "hub", "from_location": "B", "to_location": "C",
             "scheduled_start": iso(self.t0.replace(hour=10)), "scheduled_end": iso(self.t0.replace(hour=11)),
             "acceptance_deadline": iso(self.t0.replace(hour=10)),
             "required_kinds": ["wheelchair", "attendant"]},
        ]
        aid = self.create_trip(legs=legs, needs=[
            {"need_key": "wc", "category": "mobility", "detail": {"w": 1},
             "visibility": {"scope": "chain"}}])
        # 同单位两段需要两套互不冲突的设备
        for rid, kind in [("rw2", "wheelchair"), ("ra2", "attendant")]:
            self.relay.register_resource(
                request_id=f"res-{rid}", actor_id="r1", organization_id="rail",
                site_id="hub", kind=kind, identifier=rid,
                window_start=iso(self.t0 - timedelta(hours=2)),
                window_end=iso(self.t0 + timedelta(hours=12)))
        leg_ids, handoff_ids = self.ids(aid)
        self.relay.accept_leg(request_id="a0", actor_id="r1", leg_id=leg_ids[0])
        self.relay.accept_leg(request_id="a1", actor_id="r1", leg_id=leg_ids[1])
        self.relay.start_leg(request_id="st", actor_id="r1", leg_id=leg_ids[0])
        self.relay.arrive_handoff(request_id="ar", actor_id="r1", handoff_id=handoff_ids[0])
        with self.assertRaises(PermissionDenied):
            self.relay.receive_handoff(request_id="rx-same", actor_id="r1",
                                       handoff_id=handoff_ids[0])
        self.relay.receive_handoff(request_id="rx-two", actor_id="r2",
                                   handoff_id=handoff_ids[0])


class DisclosureTest(RelayCase):
    def test_health_need_cannot_be_chain_wide(self):
        with self.assertRaises(PermissionDenied):
            self.create_trip(needs=[
                {"need_key": "med", "category": "medical", "detail": {"x": 1},
                 "visibility": {"scope": "chain"}}])

    def test_unit_only_sees_authorized_needs(self):
        aid = self.create_trip()
        leg_ids, _ = self.ids(aid)
        urban = self.relay.reveal_leg_needs(actor_id="u1", leg_id=leg_ids[1])
        self.assertEqual(["wc"], [n["need_key"] for n in urban["needs"]])
        rail = self.relay.reveal_leg_needs(actor_id="r1", leg_id=leg_ids[0])
        self.assertEqual({"wc", "med"}, {n["need_key"] for n in rail["needs"]})

    def test_cross_unit_access_denied_and_not_logged(self):
        aid = self.create_trip()
        leg_ids, _ = self.ids(aid)
        with self.assertRaises(PermissionDenied):
            self.relay.reveal_leg_needs(actor_id="s1", leg_id=leg_ids[0])
        history = self.relay.access_history(actor_id="pax", assistance_id=aid)
        self.assertEqual(0, history["count"])

    def test_unit_scope_visibility(self):
        aid = self.create_trip(needs=[
            {"need_key": "wc", "category": "mobility", "detail": {"w": 1},
             "visibility": {"scope": "chain"}},
            {"need_key": "secret", "category": "communication", "detail": {"d": 1},
             "visibility": {"scope": "unit", "organization_ids": ["urban"]}},
        ])
        leg_ids, _ = self.ids(aid)
        self.assertIn("secret", [n["need_key"]
                                 for n in self.relay.reveal_leg_needs(actor_id="u1", leg_id=leg_ids[1])["needs"]])
        self.assertNotIn("secret", [n["need_key"]
                                    for n in self.relay.reveal_leg_needs(actor_id="r1", leg_id=leg_ids[0])["needs"]])

    def test_passenger_sees_full_access_history(self):
        aid = self.create_trip()
        leg_ids, _ = self.ids(aid)
        self.relay.reveal_leg_needs(actor_id="r1", leg_id=leg_ids[0])
        history = self.relay.access_history(actor_id="pax", assistance_id=aid)
        self.assertEqual(1, history["count"])
        self.assertEqual({"wc", "med"}, set(history["items"][0]["revealed_keys"]))
        # 其他单位不能查看访问履历
        with self.assertRaises(PermissionDenied):
            self.relay.access_history(actor_id="u1", assistance_id=aid)

    def test_authorized_agent_can_book(self):
        self.relay.grant_agent(request_id="grant", actor_id="pax",
                               passenger_actor_id="pax", agent_actor_id="agent")
        aid = self.create_trip(actor="agent", passenger="pax")
        state = self.relay.get_assistance(actor_id="coord", assistance_id=aid)
        self.assertEqual("pax", state["passenger_actor_id"])

    def test_unauthorized_agent_cannot_book(self):
        with self.assertRaises(PermissionDenied):
            self.create_trip(actor="agent")


class RevisionTest(RelayCase):
    def test_delay_keeps_completed_leg_record_intact(self):
        aid = self.create_trip()
        leg_ids, handoff_ids = self.run_to_handoff1(aid)
        _, delay = self.relay.report_delay(
            request_id="delay", actor_id="r1", leg_id=leg_ids[0], delay_minutes=60)
        self.assertEqual(2, delay["version"])
        states = [(l["state"], l["frozen"]) for l in delay["legs"]]
        self.assertEqual([("completed", True), ("in_progress", False), ("offered", False)], states)
        # 已完成段保留原始时刻
        self.assertIn("T09:00:00", delay["legs"][0]["scheduled_start"])
        self.assertIn("T12:00:00", delay["legs"][2]["scheduled_start"])
        # 历史版本仍可查且内容不变
        v1 = self.relay.get_assistance(actor_id="coord", assistance_id=aid, version=1)
        self.assertEqual("completed", v1["legs"][0]["state"])
        self.assertEqual(1, v1["version"])

    def test_delay_replay_is_stable(self):
        aid = self.create_trip()
        leg_ids, _ = self.run_to_handoff1(aid)
        first, b1 = self.relay.report_delay(request_id="d", actor_id="r1",
                                            leg_id=leg_ids[0], delay_minutes=30)
        replay, b2 = self.relay.report_delay(request_id="d", actor_id="r1",
                                            leg_id=leg_ids[0], delay_minutes=30)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(b1["version"], b2["version"])
        state = self.relay.get_assistance(actor_id="coord", assistance_id=aid)
        self.assertEqual(2, state["current_version"])

    def test_ticket_change_cannot_alter_completed_leg(self):
        aid = self.create_trip()
        leg_ids, _ = self.run_to_handoff1(aid)
        new_legs = [self.leg(i) for i in range(3)]
        # 改动已完成首段的时刻与交接位置：必须被拒绝且已完成段记录不动
        start = self.t0.replace(hour=8, minute=30)
        new_legs[0]["scheduled_start"] = iso(start)
        new_legs[0]["acceptance_deadline"] = iso(start)
        new_legs[1]["from_location"] = "被篡改的位置"
        with self.assertRaises((ConflictError, ValidationError)):
            self.relay.change_ticket(request_id="tc", actor_id="coord", assistance_id=aid,
                                    legs=new_legs)
        state = self.relay.get_assistance(actor_id="coord", assistance_id=aid)
        self.assertEqual(1, state["current_version"])

    def test_equipment_failure_reoffers_and_reopens_escalation_when_no_spare(self):
        aid = self.create_trip()
        leg_ids, _ = self.ids(aid)
        self.relay.accept_leg(request_id="a0", actor_id="r1", leg_id=leg_ids[0])
        state = self.relay.get_assistance(actor_id="r1", assistance_id=aid)
        locked = state["legs"][0]["locked_resource_id"]
        kind = self.db.connection.execute(
            "SELECT kind FROM assist_resources WHERE resource_id=?", (locked,)).fetchone()["kind"]
        # 若锁定的是 attendant，故障后仍有 wheelchair + 无 attendant 替代 -> 需要升级
        _, body = self.relay.report_equipment_failure(
            request_id="fault", actor_id="r1", resource_id=locked, note="升降平台故障")
        self.assertEqual("faulty", body["status"])
        self.assertEqual(2, body["revision"]["version"])
        self.assertTrue(body["requires_assignment"])
        escalations = self.relay.list_escalations(actor_id="coord", assistance_id=aid)
        self.assertTrue(any(e["level"] == 3 and e["reason"] == "no_replace_capacity"
                            for e in escalations["items"]))
        self.assertIn(kind, {"wheelchair", "attendant"})

    def test_no_show_then_recover(self):
        aid = self.create_trip()
        leg_ids, _ = self.run_to_handoff1(aid)
        _, body = self.relay.report_no_show(request_id="ns", actor_id="u1",
                                            leg_id=leg_ids[1], note="旅客未到")
        self.assertEqual("no_show", body["state"])
        escalations = self.relay.list_escalations(actor_id="coord")
        self.assertTrue(any(e["reason"] == "passenger_no_show" for e in escalations["items"]))
        self.relay.recover_no_show(request_id="rec", actor_id="u1", leg_id=leg_ids[1])
        state = self.relay.get_assistance(actor_id="coord", assistance_id=aid)
        self.assertEqual("in_progress", state["legs"][1]["state"])

    def test_takeover_freezes_units_and_resume_builds_new_version(self):
        aid = self.create_trip()
        leg_ids, _ = self.run_to_handoff1(aid)
        self.relay.takeover(request_id="to", actor_id="coord", assistance_id=aid,
                           note="旅客身体不适，人工接管")
        with self.assertRaises(ConflictError):
            self.relay.arrive_handoff(request_id="blocked", actor_id="u1",
                                      handoff_id=self.ids(aid)[1][1])
        _, resumed = self.relay.resume_from_takeover(
            request_id="resume", actor_id="coord", assistance_id=aid,
            legs=[self.leg(i) for i in range(3)])
        self.assertEqual(2, resumed["version"])
        self.assertEqual("completed", resumed["legs"][0]["state"])

    def test_operator_cannot_takeover(self):
        aid = self.create_trip()
        with self.assertRaises(PermissionDenied):
            self.relay.takeover(request_id="to", actor_id="u1", assistance_id=aid, note="x")


class TimeoutTest(RelayCase):
    def test_acceptance_timeout_swept(self):
        aid = self.create_trip()
        leg_ids, _ = self.ids(aid)
        # 时间推进到首段接单截止之后
        self.clock._value = self.t0.replace(hour=9) + timedelta(minutes=30)
        result = self.relay.sweep_timeouts()
        self.assertEqual(1, result["count"])
        # 此时再接单应被拒绝并保持升级
        with self.assertRaises(ConflictError):
            self.relay.accept_leg(request_id="late", actor_id="r1", leg_id=leg_ids[0])

    def test_handoff_overdue_escalates_to_emergency(self):
        aid = self.create_trip()
        leg_ids, handoff_ids = self.run_to_handoff1(aid)
        # 第二段把旅客带到交接位置（到达第二交接点）
        self.relay.arrive_handoff(request_id="ar1", actor_id="u1",
                                  handoff_id=handoff_ids[1])
        # 超过截止 31 分钟仍未有人接走
        self.clock._value = self.t0.replace(hour=11) + timedelta(minutes=31)
        self.relay.sweep_timeouts()
        esc = self.relay.list_escalations(actor_id="coord", assistance_id=aid)
        self.assertTrue(any(e["level"] == 3 and e["reason"] == "handoff_overdue"
                            for e in esc["items"]))
        board = self.relay.board(actor_id="coord")
        item = next(i for i in board["items"] if i["assistance_id"] == aid)
        self.assertEqual("critical", item["timeout_risk"])


class BoardTest(RelayCase):
    def test_board_shows_responsible_and_next_handoff(self):
        aid = self.create_trip()
        self.run_to_handoff1(aid)
        board = self.relay.board(actor_id="coord")
        item = next(i for i in board["items"] if i["assistance_id"] == aid)
        self.assertEqual(1, item["current_responsible"]["ordinal"])
        self.assertEqual("urban", item["current_responsible"]["organization_id"])
        self.assertEqual("西大厅", item["next_handoff"]["location"])
        self.assertEqual("station", item["next_handoff"]["inbound_organization_id"])

    def test_operator_board_scoped_to_own_unit(self):
        aid = self.create_trip()
        self.run_to_handoff1(aid)
        board = self.relay.board(actor_id="s1")
        item = next(i for i in board["items"] if i["assistance_id"] == aid)
        # 站内队只关心把旅客交给自己的那次交接
        self.assertEqual("station", item["current_responsible"]["organization_id"])
        # 铁路看板中该行程当前责任人不是铁路
        board_r = self.relay.board(actor_id="r1")
        rail_items = [i for i in board_r["items"] if i["assistance_id"] == aid]
        self.assertEqual(0, len(rail_items))


class FullTripTest(RelayCase):
    def test_full_trip_completes_and_releases_resources(self):
        aid = self.create_trip()
        leg_ids, handoff_ids = self.run_to_handoff1(aid)
        state = self.relay.get_assistance(actor_id="coord", assistance_id=aid)
        # 完成第二次交接
        self.relay.arrive_handoff(request_id="ar1", actor_id="u1",
                                  handoff_id=state["handoffs"][1]["handoff_id"])
        self.relay.receive_handoff(request_id="rx1", actor_id="s1",
                                   handoff_id=state["handoffs"][1]["handoff_id"])
        last = self.relay.get_assistance(actor_id="coord", assistance_id=aid)["legs"][2]
        self.relay.report_delivered(request_id="del", actor_id="s1", leg_id=last["leg_id"])
        self.relay.confirm_completion(request_id="done", actor_id="pax", assistance_id=aid)
        final = self.relay.get_assistance(actor_id="coord", assistance_id=aid)
        self.assertEqual("completed", final["status"])
        self.assertTrue(all(l["state"] == "completed" for l in final["legs"]))
        self.assertTrue(all(h["state"] == "completed" for h in final["handoffs"]))
        # 资源全部释放
        locked = self.db.connection.execute(
            "SELECT COUNT(*) c FROM assist_resources WHERE locked_leg_id IS NOT NULL").fetchone()
        self.assertEqual(0, locked["c"])
        # 审计链完整
        valid, _ = self.svc.verify_audit()
        self.assertTrue(valid)


if __name__ == "__main__":
    unittest.main()
