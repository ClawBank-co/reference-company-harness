"""Fence blocks real-money names and anything missing from the catalog."""

from __future__ import annotations

import unittest

from harness.fence import Fence, FenceBlock


class FenceTests(unittest.TestCase):
    def test_allowlist_accepts_catalog_tool(self) -> None:
        fence = Fence()
        fence.set_allowlist(["set_prices", "send_enterprise_deal"])
        fence.check("set_prices")
        fence.check("send_enterprise_deal")

    def test_blocks_exact_real_money_names(self) -> None:
        fence = Fence({"send", "set_prices"})
        fence.set_allowlist(["send", "set_prices"])
        with self.assertRaises(FenceBlock) as ctx:
            fence.check("send")
        self.assertEqual(ctx.exception.reason, "real-money surface")

    def test_blocks_unknown_tool(self) -> None:
        fence = Fence()
        fence.set_allowlist(["get_cost_info"])
        with self.assertRaises(FenceBlock) as ctx:
            fence.check("trade")
        self.assertIn(ctx.exception.reason, {"real-money surface", "not in published catalog allowlist"})
        with self.assertRaises(FenceBlock):
            fence.check("invented_tool")
