"""Validate shaping plans without SSH, root privileges, or network modification."""

import unittest

from artifact import network_node


class OwnedNetworkRules(unittest.TestCase):
    def setUp(self):
        network_node.WORKERS = {"127.0.0.1", "127.0.0.2", "127.0.0.3", "127.0.0.4"}

    def test_only_known_peer_destinations_are_shaped(self):
        commands = network_node.wan_commands("eth0", ["127.0.0.2", "127.0.0.3", "127.0.0.4"], 24.9)
        text = "\n".join(" ".join(command) for command in commands)
        for peer in ("127.0.0.2", "127.0.0.3", "127.0.0.4"):
            self.assertIn(peer + "/32", text)
        self.assertIn("dst_port 22", text)
        self.assertIn("src_port 22", text)
        self.assertIn("100mbit", text)

    def test_unknown_existing_rules_are_not_owned(self):
        self.assertFalse(network_node.safe_native_qdiscs([{"root": True, "kind": "htb", "handle": "1:"}]))
        self.assertTrue(network_node.safe_native_qdiscs([{"root": True, "kind": "noqueue", "handle": "0:"}]))

    def test_invalid_interface_is_rejected(self):
        with self.assertRaises(AssertionError):
            network_node.wan_commands("eth0; false", ["127.0.0.2", "127.0.0.3", "127.0.0.4"], 25)


if __name__ == "__main__":
    unittest.main()
