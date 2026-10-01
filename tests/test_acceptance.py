import unittest

from transport_coordination.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("completed", result["chain_status"])
        # 晚点重排后最终版本为 2
        self.assertEqual(2, result["final_version"])
        # 最小披露：站内服务队看不到仅对第 1、2 段披露的健康项
        self.assertFalse(result["team_sees_health_item"])
        # 三段与两次交接全部完成且不可变
        self.assertEqual(3, result["completed_segments"])
        self.assertEqual(2, result["completed_handovers"])
        self.assertTrue(result["immutable_segments"])
        # 旅客访问日志记录了每一次需求取阅
        self.assertGreaterEqual(result["access_entries"], 1)


if __name__ == "__main__":
    unittest.main()
