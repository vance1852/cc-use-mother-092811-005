import json
import unittest
from datetime import datetime, timezone

from transport_coordination.api import route
from transport_coordination.assistance import AssistanceService
from transport_coordination.clock import FixedClock
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

BASE = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)


def iso(h, m=0):
    return BASE.replace(hour=h, minute=m).isoformat().replace("+00:00", "Z")


class AssistanceApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(BASE)
        self.domain = DomainService(self.database, self.clock)
        self.assistance = AssistanceService(self.database, self.clock)
        self._bootstrap()
        self.chain_id = self._scenario()

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="", token=""):
        return route(self.domain, method, path, body,
                     {"X-Actor-Id": actor, "X-Passenger-Token": token}, self.assistance)

    def _bootstrap(self):
        for oid, name in (("ohub", "枢纽"), ("orail", "铁路"), ("ourban", "城客"), ("oteam", "站内队")):
            self.domain.register_organization(request_id=f"o-{oid}", actor_id="bootstrap",
                                              organization_id=oid, name=name)
        self.domain.register_actor(request_id="ra", actor_id="bootstrap", new_actor_id="adm",
                                   display_name="管理员", role="admin", organization_id="ohub")
        self.domain.register_actor(request_id="rc", actor_id="adm", new_actor_id="coord",
                                   display_name="协调员", role="coordinator", organization_id="ohub")
        for org, p in (("orail", "r"), ("ourban", "u"), ("oteam", "t")):
            self.domain.register_actor(request_id=f"d-{p}", actor_id="adm", new_actor_id=f"disp-{p}",
                                       display_name=f"{p}值班", role="dispatcher", organization_id=org)
            self.domain.register_actor(request_id=f"w-{p}", actor_id="adm", new_actor_id=f"w-{p}",
                                       display_name=f"{p}现场", role="worker", organization_id=org)

    def _scenario(self):
        for rid, actor, org, point, kind in (
                ("c1", "disp-r", "orail", "P1", "rail"),
                ("c2", "disp-u", "ourban", "P2", "urban"),
                ("c3", "disp-t", "oteam", "P2", "station_team")):
            status, _ = self._call("POST", "/capabilities", {
                "request_id": rid, "organization_id": org, "service_kind": kind,
                "handover_point": point, "equipment": [{"code": "wheelchair", "ref": rid}],
                "window_start": iso(0), "window_end": iso(23, 59)}, actor=actor)
            self.assertEqual(201, status)
        status, payload = self._call("POST", "/chains", {
            "request_id": "ch", "passenger_ref": "P-1", "passenger_token": "token-secret-1",
            "segments": [
                {"organization_id": "orail", "service_kind": "rail", "board_location": "车厢",
                 "handover_location": "P1", "scheduled_start": iso(8), "scheduled_end": iso(8, 30),
                 "equipment_required": ["wheelchair"]},
                {"organization_id": "ourban", "service_kind": "urban", "board_location": "P1",
                 "handover_location": "P2", "scheduled_start": iso(8, 30), "scheduled_end": iso(9),
                 "equipment_required": ["wheelchair"]},
                {"organization_id": "oteam", "service_kind": "station_team", "board_location": "P2",
                 "scheduled_start": iso(9), "scheduled_end": iso(9, 20),
                 "equipment_required": ["wheelchair"]}],
            "needs": [
                {"code": "wheelchair_user", "detail": "全程轮椅", "sensitivity": "general"},
                {"code": "oxygen", "detail": "携氧", "sensitivity": "health",
                 "visibility": "legs", "segments": [1, 2]}]}, actor="coord")
        self.assertEqual(201, status)
        return payload["chain_id"]

    def test_accept_start_and_two_phase_handover_via_http(self):
        cid = self.chain_id
        for seq, p in ((1, "r"), (2, "u"), (3, "t")):
            status, payload = self._call("POST", f"/chains/{cid}/segments/{seq}/accept",
                                         {"request_id": f"acc-{seq}",
                                          "assignee_actor_id": f"w-{p}"}, actor=f"disp-{p}")
            self.assertEqual(201, status)
        status, _ = self._call("POST", f"/chains/{cid}/segments/1/start",
                               {"request_id": "s1"}, actor="w-r")
        self.assertEqual(201, status)
        status, _ = self._call("POST", f"/chains/{cid}/handovers/1/arrive",
                               {"request_id": "h1a"}, actor="w-r")
        self.assertEqual(201, status)
        status, payload = self._call("POST", f"/chains/{cid}/handovers/1/receive",
                                     {"request_id": "h1r"}, actor="w-u")
        self.assertEqual(201, status)
        self.assertFalse(payload["chain_completed"])

        # 重复回执 409
        status, payload = self._call("POST", f"/chains/{cid}/handovers/1/receive",
                                     {"request_id": "h1r-dup"}, actor="w-u")
        self.assertEqual(409, status)

        # 最小披露：站内队取不到 oxygen
        status, payload = self._call("GET", f"/chains/{cid}/needs?segment_seq=3", actor="w-t")
        self.assertEqual(200, status)
        self.assertEqual(["wheelchair_user"], [i["code"] for i in payload["items"]])

        # 旅客访问日志需要令牌
        status, _ = self._call("GET", f"/chains/{cid}/access-log", token="bad")
        self.assertEqual(403, status)
        status, payload = self._call("GET", f"/chains/{cid}/access-log", token="token-secret-1")
        self.assertEqual(200, status)
        self.assertTrue(payload["entries"])

    def test_duty_board_shows_responsibility_and_risk(self):
        cid = self.chain_id
        for seq, p in ((1, "r"), (2, "u"), (3, "t")):
            self._call("POST", f"/chains/{cid}/segments/{seq}/accept",
                       {"request_id": f"acc-{seq}", "assignee_actor_id": f"w-{p}"}, actor=f"disp-{p}")
        self._call("POST", f"/chains/{cid}/segments/1/start", {"request_id": "s1"}, actor="w-r")
        status, payload = self._call("GET", "/duty-board", actor="coord")
        self.assertEqual(200, status)
        item = next(i for i in payload["items"] if i["chain_id"] == cid)
        self.assertEqual("w-r", item["current_responsibility"]["actor_id"])
        self.assertEqual(1, item["next_handover"]["boundary_seq"])
        self.assertEqual("P1", item["next_handover"]["location"])
        self.assertIn(item["next_handover"]["timeout_risk"], {"none", "imminent", "overdue"})

    def test_emergency_route_blocks_then_resolves(self):
        cid = self.chain_id
        status, payload = self._call("POST", f"/chains/{cid}/emergency",
                                     {"request_id": "em", "reason": "突发情况"}, actor="w-r")
        self.assertEqual(201, status)
        status, payload = self._call("POST", f"/chains/{cid}/segments/1/start",
                                     {"request_id": "s1"}, actor="w-r")
        self.assertEqual(409, status)
        status, _ = self._call("POST", f"/chains/{cid}/emergency/owner",
                               {"request_id": "emo", "owner_actor_id": "coord"}, actor="coord")
        self.assertEqual(201, status)
        status, _ = self._call("POST", f"/chains/{cid}/emergency/resolve",
                               {"request_id": "emr"}, actor="coord")
        self.assertEqual(200, status)

    def test_delay_then_reaccept_via_http_and_history(self):
        cid = self.chain_id
        for seq, p in ((1, "r"), (2, "u"), (3, "t")):
            self._call("POST", f"/chains/{cid}/segments/{seq}/accept",
                       {"request_id": f"acc-{seq}", "assignee_actor_id": f"w-{p}"}, actor=f"disp-{p}")
        self._call("POST", f"/chains/{cid}/segments/1/start", {"request_id": "s1"}, actor="w-r")
        status, payload = self._call("POST", f"/chains/{cid}/delay", {
            "request_id": "d1", "segment_seq": 1, "new_end": iso(8, 50),
            "reason": "列车晚点"}, actor="coord")
        self.assertEqual(201, status)
        self.assertEqual(2, payload["version"])
        # 未重新承接前接方收人失败
        self._call("POST", f"/chains/{cid}/handovers/1/arrive", {"request_id": "a1"}, actor="w-r")
        status, _ = self._call("POST", f"/chains/{cid}/handovers/1/receive",
                               {"request_id": "r1"}, actor="w-u")
        self.assertEqual(409, status)
        # 重新承接后交接成功
        self._call("POST", f"/chains/{cid}/segments/2/accept",
                   {"request_id": "acc2b", "assignee_actor_id": "w-u"}, actor="disp-u")
        status, payload = self._call("POST", f"/chains/{cid}/handovers/1/receive",
                                     {"request_id": "r1b"}, actor="w-u")
        self.assertEqual(201, status)
        status, history = self._call("GET", f"/chains/{cid}/history", token="token-secret-1")
        self.assertEqual(200, status)
        self.assertEqual([1], [s["seq"] for s in history["segments"]])


if __name__ == "__main__":
    import unittest
    unittest.main()
