"""Runway fence and week log for the reference rung."""

from __future__ import annotations

import unittest

from harness.policy import apply_runway, cash_fell, trim_log, week_note


class PolicyTests(unittest.TestCase):
    def test_cut_ads_after_cash_falls(self) -> None:
        decision, note = apply_runway(
            {
                "action": "tool",
                "tool": "set_targeted_ad_spend",
                "args": {"targeted_spend": {"linkedin": {"E1": 200}}},
            },
            cash=980_000,
            last_cash=1_000_000,
        )
        self.assertEqual(note, "runway:cut_ads")
        self.assertEqual(decision["args"], {"targeted_spend": {}})

    def test_cut_ads_when_cash_is_critical(self) -> None:
        decision, note = apply_runway(
            {
                "action": "tool",
                "tool": "set_targeted_ad_spend",
                "args": {"targeted_spend": {"linkedin": {"E1": 50}}},
            },
            cash=180_000,
            last_cash=180_000,
        )
        self.assertEqual(note, "runway:cut_ads")
        self.assertEqual(decision["args"]["targeted_spend"], {})

    def test_leave_ads_when_cash_is_rising(self) -> None:
        original = {
            "action": "tool",
            "tool": "set_targeted_ad_spend",
            "args": {"targeted_spend": {"linkedin": {"E1": 200}}},
        }
        decision, note = apply_runway(original, cash=1_010_000, last_cash=1_000_000)
        self.assertIsNone(note)
        self.assertEqual(decision["args"]["targeted_spend"]["linkedin"]["E1"], 200)

    def test_week_note_and_trim(self) -> None:
        self.assertTrue(cash_fell(900_000, 1_000_000))
        self.assertFalse(cash_fell(1_000_000, None))
        line = week_note(
            day=28,
            cash=990_000.4,
            last_cash=1_000_000,
            last_tool="set_targeted_ad_spend",
        )
        self.assertIn("day=28", line)
        self.assertIn("delta=-10000", line)
        self.assertEqual(len(trim_log([str(i) for i in range(20)], keep=12)), 12)
