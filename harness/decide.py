"""Prompt build, raw HTTPS LLM call, and stdlib schema / forecast validation."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TRANSIENT_LLM_STATUS = frozenset({429, 502, 503, 504})

HORIZONS = (7, 28, 84, 182)
LOCAL_ONLY_TOOLS = frozenset(
    {
        "next_week",
        "python_exec",
        "register_daily_calculation",
        "remove_daily_calculation",
        "list_daily_calculations",
        "get_tool_documentation",
    }
)

SYSTEM_PROMPT = """You are the bounded runner for a company operating Acme AI on ClawBank Business Bench.

Objective: maximize cash. Cash is the score. Bankruptcy (cash < 0) ends the run.

You talk to a hosted gym over REST. You do not invent tools. The live catalog is injected below.
Time moves only when you choose action=advance. Hosted mode has no next_week and no python_exec.
Read data.clock: tools_move_time is false; POST /advance moves advance_days (7).

Each turn return ONE JSON object, no markdown, no prose:

{"action":"tool","tool":"<catalog name>","args":{...}}
{"action":"advance","rationale":"<non-empty strategy>","forecasts":[
  {"horizon_days":7,"point":0,"lower":0,"upper":0},
  {"horizon_days":28,"point":0,"lower":0,"upper":0},
  {"horizon_days":84,"point":0,"lower":0,"upper":0},
  {"horizon_days":182,"point":0,"lower":0,"upper":0}
]}

Forecasts are cash dollars. Horizons must be exactly 7, 28, 84, 182 in that order. Each row must satisfy lower <= point <= upper.

Never call send, trade, or offramp. Never request real-money movement. One tool per turn.
Call get_cost_info before mutations or advance. Other get_* tools do not count as that inspect.
After at most 3 mutating tools since the last advance, you MUST action=advance.
Six tools of any kind in one week also require advance. Fenced tools count.
A week of only tool calls never moves time and never scores.

The gym documents tools as {"tool":"<name>","arguments":{...}}. That is the same as
{"action":"tool","tool":"<name>","args":{...}}. Do not put a catalog name in "action".
If last week's cash fell, cut advertising. Buying ads into a down week bankrupts the firm.
"""

RAW_SYSTEM_PROMPT = """You operate Acme AI on ClawBank Business Bench. Maximize cash. Bankruptcy (cash < 0) ends the run.

Talk to the hosted gym over REST. Use only the injected catalog. Time moves only with action=advance.

Return ONE JSON object, no markdown:

{"action":"tool","tool":"<catalog name>","args":{...}}
{"action":"advance","rationale":"<non-empty strategy>","forecasts":[
  {"horizon_days":7,"point":0,"lower":0,"upper":0},
  {"horizon_days":28,"point":0,"lower":0,"upper":0},
  {"horizon_days":84,"point":0,"lower":0,"upper":0},
  {"horizon_days":182,"point":0,"lower":0,"upper":0}
]}

Forecast horizons must be 7, 28, 84, 182. Never call send, trade, or offramp.
This is the thin rung: no company policy, no weekly memory, no ad cap.
"""


class Completer(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


class ProbeCompleter:
    """One catalog probe, then flat-forecast advances. Not a scored baseline."""

    def __init__(self) -> None:
        self._acted = False

    def complete(self, messages: list[dict[str, str]]) -> str:
        user = json.loads(messages[1]["content"])
        tools = {item["name"] for item in user.get("tools") or [] if "name" in item}
        if not self._acted and "get_cost_info" in tools:
            self._acted = True
            return json.dumps(
                {"action": "tool", "tool": "get_cost_info", "args": {}}
            )
        cash = current_cash(user.get("observation") or {})
        return json.dumps(
            {
                "action": "advance",
                "rationale": "Protocol probe advance.",
                "forecasts": flat_forecasts(cash),
            }
        )


class HttpCompleter:
    def __init__(
        self,
        *,
        provider_url: str,
        model: str,
        api_key: str,
        max_tokens: int = 4096,
        timeout_s: float = 120.0,
        retries: int = 5,
        sleeper: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.provider_url = provider_url
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.retries = max(1, int(retries))
        self._sleeper = sleeper
        self._opener = opener

    def complete(self, messages: list[dict[str, str]]) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        last_error: Exception | None = None
        payload: dict[str, Any] | None = None
        for attempt in range(self.retries):
            request = Request(
                self.provider_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with self._opener(request, timeout=self.timeout_s) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as exc:
                last_error = exc
                if exc.code in TRANSIENT_LLM_STATUS and attempt + 1 < self.retries:
                    self._sleeper(min(60.0, 2.0**attempt))
                    continue
                raise RuntimeError(f"llm_http_{exc.code}") from exc
            except URLError as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    self._sleeper(min(60.0, 2.0**attempt))
                    continue
                raise RuntimeError(f"llm_transport: {exc.reason}") from exc
        if payload is None:
            raise RuntimeError("llm_retries_exhausted") from last_error
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("llm_empty_choices")
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content or "")


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
    if fenced:
        stripped = fenced.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("model output is not a JSON object")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model output is not a JSON object")
    return parsed


def validate_args(schema: dict[str, Any], args: Any) -> str | None:
    """Return an error string, or None if args match a small JSON Schema subset."""
    if not isinstance(args, dict):
        return "args must be an object"
    return _validate(schema or {"type": "object"}, args, path="$")


def _validate(schema: dict[str, Any], value: Any, path: str) -> str | None:
    expected = schema.get("type")
    if expected == "object" or (expected is None and "properties" in schema):
        if not isinstance(value, dict):
            return f"{path} must be object"
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                return f"{path}.{key} is required"
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                error = _validate(properties[key], item, f"{path}.{key}")
                if error:
                    return error
            elif additional is False:
                return f"{path}.{key} is not allowed"
        return None
    if expected == "array":
        if not isinstance(value, list):
            return f"{path} must be array"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = _validate(item_schema, item, f"{path}[{index}]")
                if error:
                    return error
        return None
    if expected == "string":
        if not isinstance(value, str):
            return f"{path} must be string"
    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{path} must be integer"
    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{path} must be number"
    elif expected == "boolean":
        if not isinstance(value, bool):
            return f"{path} must be boolean"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']}"
    return None


def validate_forecasts(forecasts: Any) -> str | None:
    if not isinstance(forecasts, list) or len(forecasts) != 4:
        return "forecasts must be a list of 4 objects"
    seen: list[int] = []
    for index, row in enumerate(forecasts):
        if not isinstance(row, dict):
            return f"forecasts[{index}] must be an object"
        try:
            horizon = int(row["horizon_days"])
            point = float(row["point"])
            lower = float(row["lower"])
            upper = float(row["upper"])
        except (KeyError, TypeError, ValueError):
            return f"forecasts[{index}] needs horizon_days, point, lower, upper"
        if not lower <= point <= upper:
            return f"forecasts[{index}] must satisfy lower <= point <= upper"
        seen.append(horizon)
    if seen != list(HORIZONS):
        return "forecast horizons must be [7, 28, 84, 182]"
    return None


def current_cash(observation: dict[str, Any], default: float = 1_000_000.0) -> float:
    data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
    for key in ("cash", "cash_balance", "final_cash"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    dashboard = data.get("dashboard")
    if isinstance(dashboard, dict) and isinstance(dashboard.get("cash"), (int, float)):
        return float(dashboard["cash"])
    text = dashboard if isinstance(dashboard, str) else ""
    match = re.search(r"Cash:\s*\$?(-?[\d,]+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1).replace(",", ""))
    return default


def flat_forecasts(cash: float) -> list[dict[str, float | int]]:
    return [
        {"horizon_days": horizon, "point": cash, "lower": cash, "upper": cash}
        for horizon in HORIZONS
    ]


def rewrite_local_protocol_text(text: str) -> str:
    """Live/stale gyms still mention next_week; the hosted clock is POST /advance."""
    rewritten = text
    replacements = (
        (
            "Changes take effect on next_week.",
            "Changes take effect on POST /advance.",
        ),
        ("when next_week is called", "when you POST /advance"),
        (
            "call next_week to advance.",
            "POST /advance to move 7 days. Tools do not move time.",
        ),
        ("then call next_week", "then POST /advance"),
        ("call next_week", "POST /advance"),
        ("next_week", "POST /advance"),
        ("via python_exec()", "via list_all_tables or describe_tables"),
        ("python_exec / query", "describe_tables"),
        ("python_exec", "list_all_tables"),
    )
    for old, new in replacements:
        rewritten = rewritten.replace(old, new)
    return rewritten


def payload_leaks_local_protocol(value: Any) -> bool:
    """True when a live/stale host still teaches next_week or python_exec."""
    if isinstance(value, str):
        return any(
            token in value
            for token in LOCAL_ONLY_TOOLS
        )
    if isinstance(value, list):
        return any(payload_leaks_local_protocol(item) for item in value)
    if isinstance(value, dict):
        return any(
            key in LOCAL_ONLY_TOOLS or payload_leaks_local_protocol(item)
            for key, item in value.items()
        )
    return False


def sanitize_hosted_payload(value: Any) -> Any:
    if isinstance(value, str):
        return rewrite_local_protocol_text(value)
    if isinstance(value, list):
        return [sanitize_hosted_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: sanitize_hosted_payload(item)
            for key, item in value.items()
            if key not in LOCAL_ONLY_TOOLS
        }
    return value


def published_tools(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("tools")
    elif isinstance(payload, list):
        raw = payload
    else:
        return []
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]


def sanitize_catalog(catalog: Any) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in published_tools(catalog):
        name = item.get("name")
        if name in LOCAL_ONLY_TOOLS:
            continue
        cleaned.append(sanitize_hosted_payload(item))
    return cleaned


_RESERVED_DECISION_KEYS = frozenset(
    {
        "action",
        "args",
        "arguments",
        "comment",
        "forecasts",
        "name",
        "rationale",
        "reasoning",
        "thought",
        "tool",
        "tool_calls",
    }
)


def _as_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.startswith("{"):
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _catalog_name(name: Any, tools: dict[str, dict[str, Any]]) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    if name in tools:
        return name
    lowered = {key.lower(): key for key in tools}
    return lowered.get(name.strip().lower())


def _tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("input_schema") or tool.get("inputSchema")
    return schema if isinstance(schema, dict) else {"type": "object"}


def _hoist_schema_args(parsed: dict[str, Any], tools: dict[str, dict[str, Any]]) -> None:
    name = _catalog_name(parsed.get("tool"), tools)
    if name is None:
        return
    properties = _tool_schema(tools[name]).get("properties")
    if not isinstance(properties, dict):
        return
    args = parsed.get("args")
    if not isinstance(args, dict):
        args = {}
        parsed["args"] = args
    for key, value in list(parsed.items()):
        if key in _RESERVED_DECISION_KEYS or key not in properties or key in args:
            continue
        args[key] = value


def normalize_decision(
    parsed: dict[str, Any], tools: dict[str, dict[str, Any]]
) -> None:
    """Accept gym example_call and function-call shapes as action=tool."""
    if (
        parsed.get("action") is None
        and parsed.get("tool") is None
        and parsed.get("forecasts") is None
        and parsed.get("name") is None
        and parsed.get("tool_calls") is None
    ):
        catalog_keys = [
            key for key in parsed if _catalog_name(key, tools) is not None
        ]
        if len(catalog_keys) == 1:
            name = _catalog_name(catalog_keys[0], tools)
            payload = _as_object(parsed.get(catalog_keys[0]))
            parsed["action"] = "tool"
            parsed["tool"] = name
            if payload is not None:
                parsed["args"] = payload

    calls = parsed.get("tool_calls")
    if isinstance(calls, list) and calls and isinstance(calls[0], dict):
        first = calls[0]
        fn = first.get("function") if isinstance(first.get("function"), dict) else first
        name = _catalog_name(fn.get("name") if isinstance(fn, dict) else None, tools)
        if name is not None:
            parsed["action"] = "tool"
            parsed["tool"] = name
            args = _as_object(
                (fn.get("arguments") if isinstance(fn, dict) else None)
                or (fn.get("args") if isinstance(fn, dict) else None)
            )
            if args is not None:
                parsed["args"] = args

    if "args" not in parsed or parsed.get("args") in (None, {}):
        args = _as_object(parsed.get("arguments"))
        if args is None:
            args = _as_object(parsed.get("args"))
        if args is not None:
            parsed["args"] = args

    named = _catalog_name(parsed.get("name"), tools)
    if named is not None:
        parsed.setdefault("tool", named)

    action = parsed.get("action")
    if isinstance(action, str):
        stripped = action.strip()
        catalog = _catalog_name(stripped, tools)
        if catalog is not None:
            parsed.setdefault("tool", catalog)
            parsed["action"] = "tool"
        else:
            parsed["action"] = stripped.lower()

    if parsed.get("tool") and not isinstance(parsed.get("args"), dict):
        parsed["args"] = {}

    if parsed.get("action") not in {"tool", "advance"}:
        if _catalog_name(parsed.get("tool"), tools) is not None:
            parsed["action"] = "tool"
        elif parsed.get("forecasts") is not None:
            parsed["action"] = "advance"

    if parsed.get("action") == "tool":
        catalog = _catalog_name(parsed.get("tool"), tools)
        if catalog is not None:
            parsed["tool"] = catalog
        _hoist_schema_args(parsed, tools)


def last_action_tool(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return None
    rows = result.get("results")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        name = rows[0].get("tool")
        if isinstance(name, str) and name:
            return name
    return None


def needs_inspect(
    *,
    catalog_names: set[str],
    last_action_result: dict[str, Any] | None,
    tools_since_advance: int,
) -> bool:
    """True when this week has not inspected and get_cost_info is published."""
    if tools_since_advance >= 3 or "get_cost_info" not in catalog_names:
        return False
    if tools_since_advance > 0:
        return False
    return last_action_tool(last_action_result) != "get_cost_info"


def host_omitted_clock(observation: Any) -> bool:
    if not isinstance(observation, dict):
        return True
    data = observation.get("data")
    if not isinstance(data, dict):
        return True
    clock = data.get("clock")
    return not (
        isinstance(clock, dict)
        and clock.get("advance_days") == 7
        and clock.get("tools_move_time") is False
        and "simulated_day" in clock
    )


def ensure_observation_clock(observation: Any) -> Any:
    """Live gyms omit data.clock; the hosted week is still 7 days via POST /advance."""
    if not isinstance(observation, dict):
        return observation
    obs = dict(observation)
    data = dict(obs["data"]) if isinstance(obs.get("data"), dict) else {}
    clock = data.get("clock") if isinstance(data.get("clock"), dict) else {}
    try:
        day = int(obs.get("simulated_day", clock.get("simulated_day") or 0))
    except (TypeError, ValueError):
        day = 0
    if (
        clock.get("advance_days") != 7
        or clock.get("tools_move_time") is not False
        or "simulated_day" not in clock
    ):
        data["clock"] = {
            "simulated_day": day,
            "advance_days": 7,
            "tools_move_time": False,
        }
    obs["data"] = data
    return obs


def build_messages(
    *,
    catalog: list[dict[str, Any]],
    observation: dict[str, Any],
    last_action_result: dict[str, Any] | None,
    repair: str | None = None,
    tools_since_advance: int = 0,
    company_log: list[str] | None = None,
    cash_falling: bool = False,
    cadence: str = "reference",
) -> list[dict[str, str]]:
    user = {
        "observation": ensure_observation_clock(sanitize_hosted_payload(observation)),
        "last_action_result": sanitize_hosted_payload(last_action_result),
        "tools": sanitize_catalog(catalog),
        "tools_since_advance": tools_since_advance,
    }
    if cadence == "reference" and company_log:
        user["company_log"] = list(company_log)
    prompt = RAW_SYSTEM_PROMPT if cadence == "raw" else SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(user, default=str)},
    ]
    catalog_names = {
        item["name"] for item in catalog if isinstance(item.get("name"), str)
    }
    if cadence == "reference" and needs_inspect(
        catalog_names=catalog_names,
        last_action_result=last_action_result,
        tools_since_advance=tools_since_advance,
    ):
        messages.append(
            {
                "role": "user",
                "content": (
                    "This week has no get_cost_info yet. Call get_cost_info "
                    "before mutating or advancing."
                ),
            }
        )
    advance_after = 3 if cadence == "reference" else 12
    if tools_since_advance >= advance_after:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Return action=advance with cash forecasts. Do not call a tool."
                ),
            }
        )
    if cadence == "reference" and cash_falling:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Last week cash fell. Cut advertising. Do not raise "
                    "set_targeted_ad_spend. Change prices or capacity, or advance."
                ),
            }
        )
    if repair:
        messages.append({"role": "user", "content": f"Previous output failed validation: {repair}. Return one valid JSON object."})
    return messages


def decide(
    completer: Completer,
    *,
    catalog: list[dict[str, Any]],
    observation: dict[str, Any],
    last_action_result: dict[str, Any] | None = None,
    tools_since_advance: int = 0,
    company_log: list[str] | None = None,
    cash_falling: bool = False,
    cadence: str = "reference",
) -> tuple[dict[str, Any], str | None]:
    """Parse one model decision. Second failure keeps an intended catalog tool."""
    tools = {item["name"]: item for item in catalog if "name" in item}
    error: str | None = None
    parsed: dict[str, Any] | None = None
    advance_after = 3 if cadence == "reference" else 12
    must_advance = tools_since_advance >= advance_after
    inspect_first = cadence == "reference" and needs_inspect(
        catalog_names=set(tools),
        last_action_result=last_action_result,
        tools_since_advance=tools_since_advance,
    )
    for attempt in range(2):
        messages = build_messages(
            catalog=catalog,
            observation=observation,
            last_action_result=last_action_result,
            repair=error,
            tools_since_advance=tools_since_advance,
            company_log=company_log,
            cash_falling=cash_falling,
            cadence=cadence,
        )
        raw = completer.complete(messages)
        try:
            parsed = parse_json_object(raw)
            error = _check_decision(parsed, tools)
        except (ValueError, json.JSONDecodeError) as exc:
            error = str(exc)
            parsed = None
        if error is None and parsed is not None:
            if must_advance and parsed.get("action") != "advance":
                error = "must advance after 3 tools this week"
                parsed = None
                continue
            if inspect_first and parsed.get("tool") != "get_cost_info":
                error = "inspect with get_cost_info before mutating or advancing"
                parsed = None
                continue
            return parsed, None
    cash = current_cash(observation)
    if must_advance:
        return (
            {
                "action": "advance",
                "rationale": "Three tools already ran this week; advancing time.",
                "forecasts": flat_forecasts(cash),
            },
            error or "must_advance",
        )
    intended = (
        parsed.get("tool")
        if isinstance(parsed, dict) and parsed.get("action") == "tool"
        else None
    )
    if (
        intended in tools
        and not inspect_first
        and intended != "get_cost_info"
    ):
        args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
        return (
            {"action": "tool", "tool": intended, "args": args},
            error or "invalid_model_output",
        )
    if "get_cost_info" in tools:
        return (
            {"action": "tool", "tool": "get_cost_info", "args": {}},
            error or "invalid_model_output",
        )
    fallback = {
        "action": "advance",
        "rationale": "Validation failed twice; advancing with a flat cash forecast.",
        "forecasts": flat_forecasts(cash),
    }
    return fallback, error or "invalid_model_output"


def _check_decision(parsed: dict[str, Any], tools: dict[str, dict[str, Any]]) -> str | None:
    normalize_decision(parsed, tools)
    action = parsed.get("action")
    if action == "advance":
        rationale = parsed.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            return "advance requires a non-empty rationale"
        return validate_forecasts(parsed.get("forecasts"))
    if action == "tool":
        name = parsed.get("tool")
        if not isinstance(name, str) or name not in tools:
            return f"unknown tool {name!r}"
        return validate_args(_tool_schema(tools[name]), parsed.get("args", {}))
    return f"action must be 'tool' or 'advance', got {action!r}"
