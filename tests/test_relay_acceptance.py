import unittest

from transport_coordination.relay_acceptance import run


class RelayAcceptanceTest(unittest.TestCase):
    def test_relay_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["minimal_disclosure"])
        self.assertTrue(result["blocked_single_close"])
        self.assertTrue(result["duplicate_receipt_blocked"])
        self.assertTrue(result["delay_reordered"])
        self.assertTrue(result["completed_leg_history_preserved"])
        self.assertTrue(result["access_auditable"])


if __name__ == "__main__":
    unittest.main()
