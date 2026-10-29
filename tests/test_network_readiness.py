import unittest
from tools.network_readiness import evaluate

class NetworkReadinessTests(unittest.TestCase):
    def test_private_lan(self):
        result = evaluate(["192.168.0.109"], 5000, "192.168.0.109")
        self.assertTrue(result["ready_for_qr"])
        self.assertEqual(result["qr_base_url"], "http://192.168.0.109:5000")

    def test_mismatched_ip_blocks_qr(self):
        self.assertFalse(evaluate(["192.168.0.109"], 5000, "192.168.0.107")["ready_for_qr"])

    def test_no_address_blocks_qr(self):
        self.assertFalse(evaluate([], 5000)["ready_for_qr"])

    def test_invalid_port_blocks_qr(self):
        self.assertFalse(evaluate(["192.168.0.109"], 70000)["ready_for_qr"])

    def test_loopback_not_lan(self):
        self.assertFalse(evaluate(["127.0.0.1"], 5000)["ready_for_qr"])

if __name__ == "__main__":
    unittest.main()
