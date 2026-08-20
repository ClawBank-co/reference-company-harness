"""Schema validator and forecast fallback."""

from __future__ import annotations

import unittest

from harness.decide import (
    ProbeCompleter,
    current_cash,
    decide,
    flat_forecasts,
    parse_json_object,
    validate_args,
    validate_forecasts,
)


CATALOG = [
    {
        "name": "set_prices",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["price_a"],
            "properties": {
                "price_a": {"type": "number"},
                "tier": {"type": "integer", "enum": [1, 2, 3]},
            },
        },
    }
]


class ScriptedCompleter:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)

    def complete(self, messages: list[dict[str, str]]) -> str:
        return self.outputs.pop(0)


class DecideTests(unittest.TestCase):
    def test_validate_args_table(self) -> None:
        schema = CATALOG[0]["input_schema"]
        cases = [
            ({"price_a": 20}, None),
            ({"price_a": 20, "tier": 2}, None),
            ({}, "$.price_a is required"),
            ({"price_a": "20"}, "$.price_a must be number"),
            ({"price_a": 20, "tier": 9}, "$.tier must be one of [1, 2, 3]"),
            ({"price_a": 20, "extra": 1}, "$.extra is not allowed"),
            ("not-object", "args must be an object"),
        ]
        for args, expected in cases:
            self.assertEqual(validate_args(schema, args), expected, args)

    def test_validate_forecasts(self) -> None:
        good = flat_forecasts(1_000_000)
        self.assertIsNone(validate_forecasts(good))
        bad_order = list(reversed(good))
        self.assertIsNotNone(validate_forecasts(bad_order))
        inverted = [
            {"horizon_days": 7, "point": 10, "lower": 20, "upper": 5},
            good[1],
            good[2],
            good[3],
        ]
        self.assertIn("lower <= point <= upper", validate_forecasts(inverted) or "")

    def test_parse_fenced_json(self) -> None:
        parsed = parse_json_object('Sure.\n```json\n{"action":"advance"}\n```\n')
        self.assertEqual(parsed["action"], "advance")

    def test_current_cash_from_dashboard(self) -> None:
        cash = current_cash({"data": {"dashboard": "=== Week 1 ===\n\nCash: $229,926\n"}})
        self.assertEqual(cash, 229926.0)

    def test_second_invalid_output_falls_back_to_flat_advance(self) -> None:
        completer = ScriptedCompleter(["not json", '{"action":"tool","tool":"nope"}'])
        decision, error = decide(
            completer,
            catalog=CATALOG,
            observation={"data": {"dashboard": "Cash: $1000000"}},
        )
        self.assertEqual(decision["action"], "advance")
        self.assertIsNotNone(error)
        self.assertEqual(decision["forecasts"][0]["point"], 1_000_000.0)

    def test_valid_tool_decision(self) -> None:
        completer = ScriptedCompleter(
            ['{"action":"tool","tool":"set_prices","args":{"price_a": 22}}']
        )
        decision, error = decide(
            completer,
            catalog=CATALOG,
            observation={"data": {}},
        )
        self.assertIsNone(error)
        self.assertEqual(decision["tool"], "set_prices")

    def test_probe_completer_acts_then_advances(self) -> None:
        catalog = [{"name": "get_cost_info", "input_schema": {"type": "object"}}]
        completer = ProbeCompleter()
        first, err1 = decide(
            completer,
            catalog=catalog,
            observation={"data": {"cash": 1_000_000}},
        )
        second, err2 = decide(
            completer,
            catalog=catalog,
            observation={"data": {"cash": 999_000}},
        )
        self.assertIsNone(err1)
        self.assertEqual(first["action"], "tool")
        self.assertEqual(first["tool"], "get_cost_info")
        self.assertIsNone(err2)
        self.assertEqual(second["action"], "advance")
        self.assertEqual(second["forecasts"][0]["point"], 999_000.0)
