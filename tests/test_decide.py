"""Schema validator and forecast fallback."""

from __future__ import annotations

from io import BytesIO
import json
import unittest
from urllib.error import HTTPError

from harness.decide import (
    SYSTEM_PROMPT,
    HttpCompleter,
    ProbeCompleter,
    build_messages,
    current_cash,
    decide,
    flat_forecasts,
    normalize_decision,
    parse_json_object,
    payload_leaks_local_protocol,
    published_tools,
    sanitize_catalog,
    sanitize_hosted_payload,
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
    def test_system_prompt_reads_observation_clock(self) -> None:
        self.assertIn("data.clock", SYSTEM_PROMPT)
        self.assertIn("tools_move_time", SYSTEM_PROMPT)
        self.assertIn("advance_days", SYSTEM_PROMPT)
        self.assertIn("mutating tools", SYSTEM_PROMPT)
        self.assertIn("Fenced tools count", SYSTEM_PROMPT)
        self.assertIn("Call get_cost_info before mutations", SYSTEM_PROMPT)
        self.assertIn('"arguments"', SYSTEM_PROMPT)

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

    def test_published_tools_ignores_non_list_catalogs(self) -> None:
        self.assertEqual(published_tools({"tools": "nope"}), [])
        self.assertEqual(published_tools({"tools": {"name": "get_cost_info"}}), [])
        self.assertEqual(
            published_tools({"tools": ["get_cost_info", {"name": "get_cost_info"}]}),
            [{"name": "get_cost_info"}],
        )
        self.assertEqual(sanitize_catalog(["get_cost_info", {"name": "next_week"}]), [])

    def test_sanitize_drops_local_protocol_leaks(self) -> None:
        catalog = sanitize_catalog(
            [
                {
                    "name": "next_week",
                    "description": "Advance via next_week",
                },
                {
                    "name": "list_daily_calculations",
                    "description": "List all registered daily calculations.",
                },
                {
                    "name": "set_prices",
                    "description": "Changes take effect on next_week.",
                },
            ]
        )
        self.assertEqual([item["name"] for item in catalog], ["set_prices"])
        self.assertNotIn("next_week", catalog[0]["description"])
        payload = sanitize_hosted_payload(
            {
                "output": "Documentation for all tools",
                "data": {
                    "next_week": {"description": "call next_week"},
                    "set_prices": {"impact": "Changes take effect on next_week."},
                },
            }
        )
        self.assertNotIn("next_week", payload["data"])
        self.assertIn("POST /advance", payload["data"]["set_prices"]["impact"])
        messages = build_messages(
            catalog=[{"name": "next_week"}, {"name": "get_cost_info"}],
            observation={
                "data": {
                    "dashboard": "then call next_week to advance.",
                    "clock": {
                        "simulated_day": 0,
                        "advance_days": 7,
                        "tools_move_time": False,
                    },
                }
            },
            last_action_result={"data": {"python_exec": {"name": "python_exec"}}},
        )
        user = json.loads(messages[1]["content"])
        self.assertEqual([item["name"] for item in user["tools"]], ["get_cost_info"])
        self.assertNotIn("next_week", user["observation"]["data"]["dashboard"])
        self.assertNotIn("python_exec", user["last_action_result"]["data"])
        self.assertTrue(
            payload_leaks_local_protocol(
                {"output": "Changes take effect on next_week."}
            )
        )
        self.assertFalse(payload_leaks_local_protocol({"output": "POST /advance"}))

    def test_build_messages_injects_clock_when_host_omits_it(self) -> None:
        messages = build_messages(
            catalog=[{"name": "get_cost_info"}],
            observation={
                "simulated_day": 0,
                "data": {"dashboard": "Cash: $1000000", "time_advance": "POST /advance"},
            },
            last_action_result=None,
        )
        user = json.loads(messages[1]["content"])
        self.assertEqual(
            user["observation"]["data"]["clock"],
            {
                "simulated_day": 0,
                "advance_days": 7,
                "tools_move_time": False,
            },
        )

    def test_current_cash_from_dashboard(self) -> None:
        cash = current_cash({"data": {"dashboard": "=== Week 1 ===\n\nCash: $229,926\n"}})
        self.assertEqual(cash, 229926.0)

    def test_missing_action_with_forecasts_is_advance(self) -> None:
        completer = ScriptedCompleter(
            [
                json.dumps(
                    {
                        "rationale": "Hold cash.",
                        "forecasts": flat_forecasts(1_000_000),
                    }
                )
            ]
        )
        decision, error = decide(
            completer,
            catalog=CATALOG,
            observation={"data": {"dashboard": "Cash: $1000000"}},
        )
        self.assertIsNone(error)
        self.assertEqual(decision["action"], "advance")

    def test_invalid_output_prefers_cost_info_over_horizon_advance(self) -> None:
        catalog = [
            {"name": "get_cost_info", "input_schema": {"type": "object"}},
            CATALOG[0],
        ]
        completer = ScriptedCompleter(["not json", '{"action":"wait"}'])
        decision, error = decide(
            completer,
            catalog=catalog,
            observation={"data": {"dashboard": "Cash: $1000000"}},
        )
        self.assertIsNotNone(error)
        self.assertEqual(decision["action"], "tool")
        self.assertEqual(decision["tool"], "get_cost_info")

    def test_three_tools_without_advance_forces_advance(self) -> None:
        completer = ScriptedCompleter(
            [
                '{"action":"tool","tool":"set_prices","args":{"price_a": 22}}',
                '{"action":"tool","tool":"set_prices","args":{"price_a": 23}}',
            ]
        )
        decision, error = decide(
            completer,
            catalog=CATALOG,
            observation={"data": {"dashboard": "Cash: $1000000"}},
            tools_since_advance=3,
        )
        self.assertEqual(decision["action"], "advance")
        self.assertIsNotNone(error)
        self.assertEqual(decision["forecasts"][0]["point"], 1_000_000.0)

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

    def test_first_week_mutation_requires_inspect(self) -> None:
        catalog = [
            {"name": "get_cost_info", "input_schema": {"type": "object"}},
            CATALOG[0],
        ]
        completer = ScriptedCompleter(
            [
                '{"action":"tool","tool":"set_prices","args":{"price_a": 22}}',
                '{"action":"tool","tool":"set_prices","args":{"price_a": 23}}',
            ]
        )
        decision, error = decide(
            completer,
            catalog=catalog,
            observation={"data": {"dashboard": "Cash: $1000000"}},
        )
        self.assertEqual(decision["action"], "tool")
        self.assertEqual(decision["tool"], "get_cost_info")
        self.assertIsNotNone(error)

    def test_docs_get_does_not_satisfy_cost_inspect(self) -> None:
        catalog = [
            {"name": "get_cost_info", "input_schema": {"type": "object"}},
            {"name": "get_tool_documentation", "input_schema": {"type": "object"}},
            CATALOG[0],
        ]
        completer = ScriptedCompleter(
            [
                '{"action":"tool","tool":"get_tool_documentation","args":{}}',
                '{"action":"tool","tool":"get_tool_documentation","args":{}}',
            ]
        )
        decision, error = decide(
            completer,
            catalog=catalog,
            observation={"data": {"dashboard": "Cash: $1000000"}},
            last_action_result={
                "results": [
                    {
                        "tool": "get_tool_documentation",
                        "success": True,
                        "output": "docs",
                    }
                ]
            },
        )
        self.assertEqual(decision["action"], "tool")
        self.assertEqual(decision["tool"], "get_cost_info")
        self.assertIsNotNone(error)

    def test_mutation_allowed_after_inspect(self) -> None:
        completer = ScriptedCompleter(
            ['{"action":"tool","tool":"set_prices","args":{"price_a": 22}}']
        )
        decision, error = decide(
            completer,
            catalog=[
                {"name": "get_cost_info", "input_schema": {"type": "object"}},
                CATALOG[0],
            ],
            observation={"data": {}},
            last_action_result={
                "results": [{"tool": "get_cost_info", "success": True, "output": "ok"}]
            },
        )
        self.assertIsNone(error)
        self.assertEqual(decision["tool"], "set_prices")

    def test_gym_example_call_is_a_tool_after_inspect(self) -> None:
        catalog = [
            {"name": "get_cost_info", "input_schema": {"type": "object"}},
            CATALOG[0],
        ]
        inspected = {
            "results": [{"tool": "get_cost_info", "success": True, "output": "ok"}]
        }
        cases = [
            {"tool": "set_prices", "arguments": {"price_a": 22}},
            {"action": "set_prices", "arguments": {"price_a": 22}},
            {"action": "set_prices", "price_a": 22},
            {"set_prices": {"price_a": 22}},
            {
                "tool_calls": [
                    {
                        "function": {
                            "name": "set_prices",
                            "arguments": '{"price_a": 22}',
                        }
                    }
                ]
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                decision, error = decide(
                    ScriptedCompleter([json.dumps(payload)]),
                    catalog=catalog,
                    observation={"data": {}},
                    last_action_result=inspected,
                )
                self.assertIsNone(error, payload)
                self.assertEqual(decision["action"], "tool")
                self.assertEqual(decision["tool"], "set_prices")
                self.assertEqual(decision["args"], {"price_a": 22})

    def test_near_miss_mutation_is_not_replaced_with_cost_info(self) -> None:
        catalog = [
            {"name": "get_cost_info", "input_schema": {"type": "object"}},
            CATALOG[0],
        ]
        completer = ScriptedCompleter(
            [
                '{"action":"set_prices","args":{"price_a":"22"}}',
                '{"action":"set_prices","args":{"price_a":"23"}}',
            ]
        )
        decision, error = decide(
            completer,
            catalog=catalog,
            observation={"data": {}},
            last_action_result={
                "results": [{"tool": "get_cost_info", "success": True, "output": "ok"}]
            },
        )
        self.assertIsNotNone(error)
        self.assertEqual(decision["tool"], "set_prices")
        self.assertEqual(decision["args"], {"price_a": "23"})

    def test_normalize_decision_maps_catalog_action(self) -> None:
        parsed = {"action": "set_prices", "arguments": {"price_a": 22}}
        normalize_decision(parsed, {item["name"]: item for item in CATALOG})
        self.assertEqual(parsed["action"], "tool")
        self.assertEqual(parsed["tool"], "set_prices")
        self.assertEqual(parsed["args"], {"price_a": 22})

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
            last_action_result={
                "results": [{"tool": "get_cost_info", "success": True, "output": "ok"}]
            },
        )
        self.assertIsNone(err1)
        self.assertEqual(first["action"], "tool")
        self.assertEqual(first["tool"], "get_cost_info")
        self.assertIsNone(err2)
        self.assertEqual(second["action"], "advance")
        self.assertEqual(second["forecasts"][0]["point"], 999_000.0)

    def test_http_completer_retries_transient_429(self) -> None:
        class FlakyOpener:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self, request, timeout=None):
                self.calls += 1
                if self.calls < 3:
                    raise HTTPError(
                        str(request.full_url),
                        429,
                        "rate limited",
                        hdrs=None,
                        fp=BytesIO(),
                    )
                payload = {
                    "choices": [
                        {"message": {"content": '{"action":"advance"}'}}
                    ]
                }

                class _Response:
                    def read(self_inner):
                        return json.dumps(payload).encode("utf-8")

                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *args):
                        return False

                return _Response()

        opener = FlakyOpener()
        sleeps: list[float] = []
        completer = HttpCompleter(
            provider_url="https://example.test/v1/chat/completions",
            model="gpt-4.1-mini",
            api_key="sk-test",
            opener=opener,
            sleeper=sleeps.append,
        )
        text = completer.complete([{"role": "user", "content": "{}"}])
        self.assertEqual(text, '{"action":"advance"}')
        self.assertEqual(opener.calls, 3)
        self.assertEqual(sleeps, [1.0, 2.0])
