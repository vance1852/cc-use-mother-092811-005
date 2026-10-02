"""接续协助平台的 HTTP 路由测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.api import route
from transport_coordination.clock import FixedClock
from transport_coordination.relay import RelayService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class RelayApiTest(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        self.database = Database()
        self.service = DomainService(self.database, FixedClock(self.t0))
        self.relay = RelayService(self.database, FixedClock(self.t0))
        self._seed()

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def _get(self, path, actor):
        return route(self.service, "GET", path, {}, {"X-Actor-Id": actor})

    def _seed(self):
        s = self.service
        for org in ("rail", "urban"):
            s.register_organization(request_id=f"o-{org}", actor_id="bootstrap",
                                    organization_id=org, name=org)
        s.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                         display_name="管理员", role="admin", organization_id="rail")
        for aid, org, role, name in [
            ("coord", "rail", "coordinator", "协调员"),
            ("r1", "rail", "operator", "铁路员"),
            ("u1", "urban", "operator", "城运员"),
            ("pax", "urban", "passenger", "旅客"),
        ]:
            s.register_actor(request_id=f"a-{aid}", actor_id="adm", new_actor_id=aid,
                             display_name=name, role=role, organization_id=org)
        s.register_site(request_id="s-hub", actor_id="adm", site_id="hub",
                        organization_id="rail", name="枢纽", timezone_name="Asia/Shanghai")
        s.register_site(request_id="s-metro", actor_id="adm", site_id="metro",
                        organization_id="urban", name="地铁", timezone_name="Asia/Shanghai")
        for rid, actor, org, site, kind in [
            ("rw", "r1", "rail", "hub", "wheelchair"), ("ra", "r1", "rail", "hub", "attendant"),
            ("uw", "u1", "urban", "metro", "wheelchair"), ("ua", "u1", "urban", "metro", "attendant"),
        ]:
            self._post("/relay/resources", {
                "request_id": f"res-{rid}", "organization_id": org, "site_id": site,
                "kind": kind, "identifier": rid,
                "window_start": iso(self.t0 - timedelta(hours=2)),
                "window_end": iso(self.t0 + timedelta(hours=12))}, actor)

    def _leg(self, site, frm, to, h1, h2):
        return {"site_id": site, "from_location": frm, "to_location": to,
                "scheduled_start": iso(self.t0.replace(hour=h1)),
                "scheduled_end": iso(self.t0.replace(hour=h2)),
                "acceptance_deadline": iso(self.t0.replace(hour=h1)),
                "required_kinds": ["wheelchair", "attendant"]}

    def _create(self):
        status, body = self._post("/assistances", {
            "request_id": "trip",
            "needs": [
                {"need_key": "wc", "category": "mobility", "detail": {"w": 1},
                 "visibility": {"scope": "chain"}},
                {"need_key": "med", "category": "medical", "detail": {"m": 1},
                 "visibility": {"scope": "leg", "ordinals": [0]}},
            ],
            "legs": [self._leg("hub", "站台", "口", 9, 10),
                     self._leg("metro", "口", "出口", 10, 11)],
        }, "pax")
        self.assertEqual(201, status, body)
        return body["result"]["assistance_id"]

    def test_create_and_board_flow(self):
        aid = self._create()
        status, detail = self._get(f"/assistances/{aid}", "coord")
        self.assertEqual(200, status)
        leg_ids = [l["leg_id"] for l in detail["legs"]]
        handoff_ids = [h["handoff_id"] for h in detail["handoffs"]]

        status, acc = self._post(f"/legs/{leg_ids[0]}/accept",
                                 {"request_id": "a0"}, "r1")
        self.assertEqual(201, status)
        self.assertEqual(2, len(acc["result"]["locked_resources"]))
        self._post(f"/legs/{leg_ids[1]}/accept", {"request_id": "a1"}, "u1")

        # 健康信息最小披露：城运段只看到 wc
        status, needs = self._get(f"/legs/{leg_ids[1]}/needs", "u1")
        self.assertEqual(200, status)
        self.assertEqual(["wc"], [n["need_key"] for n in needs["needs"]])

        self._post(f"/legs/{leg_ids[0]}/start", {"request_id": "st"}, "r1")
        status, _ = self._post(f"/handoffs/{handoff_ids[0]}/arrive",
                               {"request_id": "ar"}, "r1")
        self.assertEqual(201, status)
        # 重复回执
        status, first = self._post(f"/handoffs/{handoff_ids[0]}/receive",
                                   {"request_id": "rx"}, "u1")
        self.assertEqual(201, status)
        status, replay = self._post(f"/handoffs/{handoff_ids[0]}/receive",
                                    {"request_id": "rx"}, "u1")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        # 看板
        status, board = self._get("/board", "coord")
        self.assertEqual(200, status)
        item = next(i for i in board["items"] if i["assistance_id"] == aid)
        self.assertEqual("urban", item["current_responsible"]["organization_id"])
        self.assertIn(item["timeout_risk"], ("none", "near", "high", "critical"))

        # 旅客核对访问履历
        status, history = self._get(f"/assistances/{aid}/access-history", "pax")
        self.assertEqual(200, status)
        self.assertGreaterEqual(history["count"], 2)

    def test_health_endpoint_reports_chain(self):
        status, body = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])

    def test_cross_unit_needs_forbidden(self):
        aid = self._create()
        _, detail = self._get(f"/assistances/{aid}", "coord")
        leg0 = detail["legs"][0]["leg_id"]
        status, body = self._get(f"/legs/{leg0}/needs", "u1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", body["error"])

    def test_board_requires_staff(self):
        status, body = self._get("/board", "pax")
        self.assertEqual(403, status)


if __name__ == "__main__":
    unittest.main()
