"""接续协助链的端到端规则测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.assistance import AssistanceService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, PermissionDenied, ValidationError
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

BASE = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)


def iso(hour: int, minute: int = 0) -> str:
    return (BASE.replace(hour=hour, minute=minute)).isoformat().replace("+00:00", "Z")


class AssistanceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(BASE)
        self.domain = DomainService(self.database, self.clock)
        self.svc = AssistanceService(self.database, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _bootstrap(self):
        d = self.domain
        for oid, name in (("ohub", "枢纽管理方"), ("orail", "铁路段"),
                          ("ourban", "城市客运"), ("oteam", "站内服务队")):
            d.register_organization(request_id=f"org-{oid}", actor_id="bootstrap",
                                    organization_id=oid, name=name)
        d.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-hub",
                         display_name="枢纽管理员", role="admin", organization_id="ohub")
        d.register_actor(request_id="coord", actor_id="admin-hub", new_actor_id="coord-1",
                         display_name="无障碍协调员", role="coordinator", organization_id="ohub")
        for org, prefix in (("orail", "rail"), ("ourban", "urb"), ("oteam", "team")):
            d.register_actor(request_id=f"disp-{prefix}", actor_id="admin-hub",
                             new_actor_id=f"disp-{prefix}", display_name=f"{prefix}值班员",
                             role="dispatcher", organization_id=org)
            d.register_actor(request_id=f"w-{prefix}", actor_id="admin-hub",
                             new_actor_id=f"w-{prefix}", display_name=f"{prefix}现场员",
                             role="worker", organization_id=org)

    ORG_DISPATCHER = {"orail": "rail", "ourban": "urb", "oteam": "team"}
    ORG_WORKER = {"orail": "rail", "ourban": "urb", "oteam": "team"}

    def _capability(self, org, point, kind, refs, request_id, start_hour=0, end_hour=23):
        prefix = self.ORG_DISPATCHER[org]
        return self.svc.declare_capability(
            request_id=request_id, actor_id=f"disp-{prefix}",
            organization_id=org, service_kind=kind, handover_point=point,
            equipment=[{"code": "wheelchair", "ref": ref} for ref in refs],
            window_start=iso(start_hour), window_end=iso(end_hour, 59))

    def _chain(self, request_id="chain-1", token="secret-token-007"):
        return self.svc.create_chain(
            request_id=request_id, actor_id="coord-1", passenger_ref="P-轮椅旅客-01",
            passenger_token=token,
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

    def _cover_all(self):
        self._capability("orail", "站台直梯口", "rail", ["rw-1"], "cap-1")
        self._capability("ourban", "换乘大厅服务台", "urban", ["uw-1"], "cap-2")
        self._capability("oteam", "换乘大厅服务台", "station_team", ["tw-1"], "cap-3")

    def _accept_all(self, chain_id):
        for seq, org in ((1, "orail"), (2, "ourban"), (3, "oteam")):
            prefix = self.ORG_WORKER[org]
            self.svc.accept_segment(
                request_id=f"accept-{chain_id}-{seq}", actor_id=f"disp-{prefix}",
                chain_id=chain_id, segment_seq=seq, assignee_actor_id=f"w-{prefix}")

    def test_full_handoff_chain_and_single_completion(self):
        self._cover_all()
        created = self._chain()
        chain_id = created["chain_id"]
        self.assertFalse(created["replayed"])
        self._accept_all(chain_id)

        self.svc.start_segment(request_id="start-1", actor_id="w-rail",
                               chain_id=chain_id, segment_seq=1)
        self.svc.handover_arrive(request_id="arr-1", actor_id="w-rail",
                                 chain_id=chain_id, boundary_seq=1)
        received = self.svc.handover_receive(request_id="rcv-1", actor_id="w-urb",
                                             chain_id=chain_id, boundary_seq=1)
        self.assertEqual("completed", received["state"])
        detail = self.svc.coordinator_view(actor_id="coord-1", chain_id=chain_id)
        seg1 = next(s for s in detail["segments"] if s["version"] == 1 and s["seq"] == 1)
        seg2 = next(s for s in detail["segments"] if s["version"] == 1 and s["seq"] == 2)
        self.assertEqual("completed", seg1["state"])
        self.assertTrue(seg1["immutable"])
        self.assertEqual("in_progress", seg2["state"])

        # 重复回执不能完成两次交接
        with self.assertRaises(ConflictError):
            self.svc.handover_receive(request_id="rcv-1-dup", actor_id="w-urb",
                                      chain_id=chain_id, boundary_seq=1)
        with self.assertRaises(ConflictError):
            self.svc.handover_arrive(request_id="arr-1-dup", actor_id="w-rail",
                                     chain_id=chain_id, boundary_seq=1)
        # 同一 request_id 重放只返回原回执
        replay = self.svc.handover_receive(request_id="rcv-1", actor_id="w-urb",
                                           chain_id=chain_id, boundary_seq=1)
        self.assertTrue(replay["replayed"])

        self.svc.handover_arrive(request_id="arr-2", actor_id="w-urb",
                                 chain_id=chain_id, boundary_seq=2)
        done = self.svc.handover_receive(request_id="rcv-2", actor_id="w-team",
                                         chain_id=chain_id, boundary_seq=2)
        self.assertTrue(done["chain_completed"])
        board = self.svc.duty_board(actor_id="coord-1")
        item = next(i for i in board["items"] if i["chain_id"] == chain_id)
        self.assertEqual("completed", item["status"])
        valid, _ = self.domain.verify_audit()
        self.assertTrue(valid)

    def test_minimum_disclosure_and_access_log(self):
        self._cover_all()
        chain_id = self._chain()["chain_id"]
        self._accept_all(chain_id)

        # 第三段单位看不到第一、二段才需要的健康信息
        needs3 = self.svc.access_segment_needs(actor_id="w-team", chain_id=chain_id, segment_seq=3)
        codes = {item["code"] for item in needs3["items"]}
        self.assertIn("wheelchair_user", codes)
        self.assertNotIn("oxygen", codes)

        needs1 = self.svc.access_segment_needs(actor_id="w-rail", chain_id=chain_id, segment_seq=1)
        self.assertIn("oxygen", {i["code"] for i in needs1["items"]})

        # 外单位不能取阅
        with self.assertRaises(PermissionDenied):
            self.svc.access_segment_needs(actor_id="w-urb", chain_id=chain_id, segment_seq=1)
        # 未被指派的同单位工人也不能取阅明细
        with self.assertRaises(PermissionDenied):
            self.svc.access_segment_needs(actor_id="w-team", chain_id=chain_id, segment_seq=2)

        log = self.svc.access_log(chain_id=chain_id, passenger_token="secret-token-007")
        accessed = {(e["actor_id"], tuple(e["need_codes"])) for e in log["entries"]}
        self.assertIn(("w-team", ("wheelchair_user",)), accessed)
        with self.assertRaises(PermissionDenied):
            self.svc.access_log(chain_id=chain_id, passenger_token="wrong-token")

    def test_delay_forces_redownstream_reaccept(self):
        self._cover_all()
        chain_id = self._chain()["chain_id"]
        self._accept_all(chain_id)
        self.svc.start_segment(request_id="start-1", actor_id="w-rail",
                               chain_id=chain_id, segment_seq=1)

        result = self.svc.report_delay(request_id="delay-1", actor_id="coord-1",
                                       chain_id=chain_id, segment_seq=1,
                                       new_end=iso(8, 50), reason="列车晚点20分钟")
        self.assertEqual(2, result["version"])
        detail = self.svc.coordinator_view(actor_id="coord-1", chain_id=chain_id)
        v2 = {s["seq"]: s for s in detail["segments"] if s["version"] == 2}
        self.assertEqual("in_progress", v2[1]["state"])
        self.assertEqual("proposed", v2[2]["state"])
        self.assertIsNone(v2[2]["assigned_actor_id"])
        self.assertEqual("proposed", v2[3]["state"])

        # 接方尚未重新承接：交接无法完成（原题中“无人接应”被规则阻断）
        self.svc.handover_arrive(request_id="arr-1", actor_id="w-rail",
                                 chain_id=chain_id, boundary_seq=1)
        with self.assertRaises(ConflictError):
            self.svc.handover_receive(request_id="rcv-1", actor_id="w-urb",
                                      chain_id=chain_id, boundary_seq=1)

        # 旧锁已释放：新版本仅在途的段1仍持锁，下游必须重新承接后才重新锁定
        locks = self.database.connection.execute(
            "SELECT state,COUNT(*) AS c FROM resource_locks WHERE chain_id=? GROUP BY state",
            (chain_id,)).fetchall()
        states = {row["state"]: row["c"] for row in locks}
        self.assertEqual(1, states.get("locked", 0))
        self.assertGreaterEqual(states.get("released", 0), 2)

        self.svc.accept_segment(request_id="re-accept-2", actor_id="disp-urb",
                                chain_id=chain_id, segment_seq=2, assignee_actor_id="w-urb")
        received = self.svc.handover_receive(request_id="rcv-1", actor_id="w-urb",
                                             chain_id=chain_id, boundary_seq=1)
        self.assertEqual("completed", received["state"])

    def test_timeout_sweep_escalates_handover(self):
        self._cover_all()
        chain_id = self._chain()["chain_id"]
        self._accept_all(chain_id)
        self.svc.start_segment(request_id="start-1", actor_id="w-rail",
                               chain_id=chain_id, segment_seq=1)
        # 列车到站 08:35，送出方 08:35 到达交接点，接方迟迟不确认
        self.clock._value = self.clock._value.replace(hour=0) + timedelta(hours=8, minutes=35)
        self.svc.handover_arrive(request_id="arr-1", actor_id="w-rail",
                                 chain_id=chain_id, boundary_seq=1)
        self.clock._value += timedelta(minutes=4)
        self.assertEqual(1, self.svc.sweep_timeouts())
        board = self.svc.duty_board(actor_id="coord-1")
        item = next(i for i in board["items"] if i["chain_id"] == chain_id)
        self.assertEqual("overdue", item["next_handover"]["timeout_risk"])
        kinds = {e["kind"] for e in item["escalations"]}
        self.assertIn("handover_incoming_missing", kinds)

    def test_equipment_failure_substitutes_or_resourcing(self):
        # 段2只有一台轮椅，故障后无替补 → resourcing + 一级升级
        self._capability("orail", "站台直梯口", "rail", ["rw-1"], "cap-1")
        self._capability("ourban", "站台直梯口", "urban", ["uw-1"], "cap-2", end_hour=9)
        self._capability("oteam", "换乘大厅服务台", "station_team", ["tw-1"], "cap-3")
        chain_id = self._chain()["chain_id"]
        self.svc.accept_segment(request_id="a1", actor_id="disp-rail",
                                chain_id=chain_id, segment_seq=1, assignee_actor_id="w-rail")
        self.svc.accept_segment(request_id="a2", actor_id="disp-urb",
                                chain_id=chain_id, segment_seq=2, assignee_actor_id="w-urb")
        self.svc.report_equipment_incident(
            request_id="inc-1", actor_id="w-urb", organization_id="ourban",
            handover_point="站台直梯口", resource_code="wheelchair", resource_ref="uw-1",
            reason="轮椅制动故障")
        detail = self.svc.coordinator_view(actor_id="coord-1", chain_id=chain_id)
        seg2 = next(s for s in detail["segments"] if s["version"] == 1 and s["seq"] == 2)
        self.assertEqual("resourcing", seg2["state"])
        board = self.svc.duty_board(actor_id="coord-1")
        item = next(i for i in board["items"] if i["chain_id"] == chain_id)
        self.assertIn("equipment_fault", {e["kind"] for e in item["escalations"]})

        # 补充一台设备并由值班员重新承接，升级关闭
        self._capability("ourban", "站台直梯口", "urban", ["uw-2"], "cap-2b", end_hour=9)
        self.svc.accept_segment(request_id="a2b", actor_id="disp-urb",
                                chain_id=chain_id, segment_seq=2, assignee_actor_id="w-urb")
        board = self.svc.duty_board(actor_id="coord-1")
        item = next(i for i in board["items"] if i["chain_id"] == chain_id)
        self.assertNotIn("equipment_fault", {e["kind"] for e in item["escalations"]})

    def test_no_show_then_resume_keeps_completed_records(self):
        self._cover_all()
        chain_id = self._chain()["chain_id"]
        self._accept_all(chain_id)
        self.svc.start_segment(request_id="start-1", actor_id="w-rail",
                               chain_id=chain_id, segment_seq=1)
        self.svc.handover_arrive(request_id="arr-1", actor_id="w-rail",
                                 chain_id=chain_id, boundary_seq=1)
        self.svc.handover_receive(request_id="rcv-1", actor_id="w-urb",
                                  chain_id=chain_id, boundary_seq=1)
        self.svc.mark_no_show(request_id="ns-1", actor_id="coord-1",
                              chain_id=chain_id, boundary_seq=2)
        detail = self.svc.coordinator_view(actor_id="coord-1", chain_id=chain_id)
        self.assertEqual("no_show", detail["status"])
        self.assertIn("passenger_no_show",
                      {e["kind"] for e in self.svc.duty_board(actor_id="coord-1")
                       ["items"][0]["escalations"]})

        resumed = self.svc.resume_after_no_show(request_id="resume-1", actor_id="coord-1",
                                                chain_id=chain_id)
        self.assertEqual(2, resumed["version"])
        history = self.svc.completed_history(chain_id=chain_id, passenger_token="secret-token-007")
        self.assertEqual([1], [s["seq"] for s in history["segments"]])
        self.assertEqual([1], [h["boundary_seq"] for h in history["handovers"]])
        self.assertTrue(all(s["immutable"] for s in history["segments"]))

    def test_rebook_after_completed_segment_preserves_originals(self):
        self._cover_all()
        chain_id = self._chain()["chain_id"]
        self._accept_all(chain_id)
        self.svc.start_segment(request_id="start-1", actor_id="w-rail",
                               chain_id=chain_id, segment_seq=1)
        self.svc.handover_arrive(request_id="arr-1", actor_id="w-rail",
                                 chain_id=chain_id, boundary_seq=1)
        self.svc.handover_receive(request_id="rcv-1", actor_id="w-urb",
                                  chain_id=chain_id, boundary_seq=1)
        result = self.svc.rebook(
            request_id="rb-1", actor_id="coord-1", chain_id=chain_id, from_seq=2,
            reason="城市客运改线",
            segments=[
                {"organization_id": "oteam", "service_kind": "station_team",
                 "board_location": "站台直梯口", "handover_location": "北广场无障碍电梯",
                 "scheduled_start": iso(8, 40), "scheduled_end": iso(9, 10),
                 "equipment_required": ["wheelchair"]},
                {"organization_id": "oteam", "service_kind": "station_team",
                 "board_location": "北广场无障碍电梯",
                 "scheduled_start": iso(9, 10), "scheduled_end": iso(9, 30),
                 "equipment_required": ["wheelchair"]},
            ])
        self.assertEqual(2, result["version"])
        self.assertEqual(3, result["segment_count"])
        detail = self.svc.coordinator_view(actor_id="coord-1", chain_id=chain_id)
        v2_seg1 = next(s for s in detail["segments"] if s["version"] == 2 and s["seq"] == 1)
        self.assertTrue(v2_seg1["immutable"])
        self.assertEqual("completed", v2_seg1["state"])
        history = self.svc.completed_history(chain_id=chain_id, actor_id="coord-1")
        # 已完成段始终是最初版本的原始记录
        self.assertEqual(1, history["segments"][0]["version"])
        self.assertEqual("orail", history["segments"][0]["organization_id"])

    def test_emergency_takeover_blocks_and_resolves(self):
        self._cover_all()
        chain_id = self._chain()["chain_id"]
        self._accept_all(chain_id)
        self.svc.emergency_takeover(request_id="em-1", actor_id="w-rail",
                                    chain_id=chain_id, reason="旅客突发不适")
        with self.assertRaises(ConflictError):
            self.svc.start_segment(request_id="start-1", actor_id="w-rail",
                                   chain_id=chain_id, segment_seq=1)
        board = self.svc.duty_board(actor_id="coord-1")
        item = next(i for i in board["items"] if i["chain_id"] == chain_id)
        self.assertEqual(3, item["escalations"][0]["level"])
        self.svc.assign_emergency_owner(request_id="em-own", actor_id="coord-1",
                                        chain_id=chain_id, owner_actor_id="coord-1")
        self.svc.resolve_emergency(request_id="em-ok", actor_id="coord-1", chain_id=chain_id)
        self.svc.start_segment(request_id="start-1", actor_id="w-rail",
                               chain_id=chain_id, segment_seq=1)

    def test_capability_gate_blocks_acceptance(self):
        # 只声明段1能力：段2无能力承接
        self._capability("orail", "站台直梯口", "rail", ["rw-1"], "cap-1")
        chain_id = self._chain()["chain_id"]
        self.svc.accept_segment(request_id="a1", actor_id="disp-rail",
                                chain_id=chain_id, segment_seq=1, assignee_actor_id="w-rail")
        with self.assertRaises(ConflictError):
            self.svc.accept_segment(request_id="a2", actor_id="disp-urb",
                                    chain_id=chain_id, segment_seq=2, assignee_actor_id="w-urb")

    def test_idempotent_replay_and_payload_conflict(self):
        self._cover_all()
        first = self._chain(request_id="chain-x")
        second = self._chain(request_id="chain-x")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["chain_id"], second["chain_id"])
        with self.assertRaises(ConflictError):
            self.svc.create_chain(
                request_id="chain-x", actor_id="coord-1", passenger_ref="P-02",
                passenger_token="secret-token-007",
                segments=[
                    {"organization_id": "orail", "service_kind": "rail",
                     "board_location": "其他位置", "handover_location": "东门",
                     "scheduled_start": iso(8), "scheduled_end": iso(8, 30),
                     "equipment_required": []},
                    {"organization_id": "oteam", "service_kind": "station_team",
                     "board_location": "东门", "scheduled_start": iso(9),
                     "scheduled_end": iso(9, 20), "equipment_required": []},
                ],
                needs=[{"code": "wheelchair_user", "detail": "轮椅", "sensitivity": "general"}])

    def test_health_need_cannot_be_whole_chain_visible(self):
        with self.assertRaises(ValidationError):
            self.svc.create_chain(
                request_id="chain-bad", actor_id="coord-1", passenger_ref="P-03",
                passenger_token="secret-token-009",
                segments=[
                    {"organization_id": "orail", "service_kind": "rail",
                     "board_location": "A", "scheduled_start": iso(8), "scheduled_end": iso(8, 30)},
                    {"organization_id": "oteam", "service_kind": "station_team",
                     "board_location": "B", "scheduled_start": iso(9), "scheduled_end": iso(9, 20)},
                ],
                needs=[{"code": "condition", "detail": "病史", "sensitivity": "health",
                        "visibility": "all"}])

    def test_duty_board_scoped_to_organization(self):
        self._cover_all()
        chain_id = self._chain()["chain_id"]
        self._accept_all(chain_id)
        self.svc.start_segment(request_id="start-1", actor_id="w-rail",
                               chain_id=chain_id, segment_seq=1)
        board = self.svc.duty_board(actor_id="w-urb")
        self.assertEqual(1, len(board["items"]))
        item = board["items"][0]
        self.assertEqual("orail", item["current_responsibility"]["organization_id"])
        self.assertIsNotNone(item["next_handover"])
        # 城市客运承担第2段，按最小披露可看到 oxygen；全程通用项同样可见
        self.assertIn("oxygen", item["need_codes_for_my_unit"])
        self.assertIn("wheelchair_user", item["need_codes_for_my_unit"])
        # 站内服务队（第3段）的值守板不得出现第1、2段的健康信息
        team_board = self.svc.duty_board(actor_id="w-team")
        team_item = next(i for i in team_board["items"] if i["chain_id"] == chain_id)
        self.assertNotIn("oxygen", team_item["need_codes_for_my_unit"])
        self.assertIn("wheelchair_user", team_item["need_codes_for_my_unit"])


if __name__ == "__main__":
    unittest.main()
