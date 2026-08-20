"""Prompt build, raw HTTPS LLM call, and stdlib schema / forecast validation."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HORIZONS = (7, 28, 84, 182)

SYSTEM_PROMPT = """You are the bounded runner for a company operating Acme AI on ClawBank Business Bench.

Objective: maximize cash. Cash is the score. Bankruptcy (cash < 0) ends the run.

You talk to a hosted gym over REST. You do not invent tools. The live catalog is injected below.
Time moves only when you choose action=advance. Hosted mode has no next_week and no python_exec.

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
    ) -> None:
        self.provider_url = provider_url
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    def complete(self, messages: list[dict[str, str]]) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "temperature": 0,
            }
        ).encode("utf-8")
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
            with urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"llm_http_{exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"llm_transport: {exc.reason}") from exc
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("llm_empty_choices")
        return str(choices[0].get("message", {}).get("content") or "")


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


def build_messages(
    *,
    catalog: list[dict[str, Any]],
    observation: dict[str, Any],
    last_action_result: dict[str, Any] | None,
    repair: str | None = None,
) -> list[dict[str, str]]:
    user = {
        "observation": observation,
        "last_action_result": last_action_result,
        "tools": catalog,
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user, default=str)},
    ]
    if repair:
        messages.append({"role": "user", "content": f"Previous output failed validation: {repair}. Return one valid JSON object."})
    return messages


def decide(
    completer: Completer,
    *,
    catalog: list[dict[str, Any]],
    observation: dict[str, Any],
    last_action_result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Parse one model decision. Second failure → flat-forecast advance."""
    tools = {item["name"]: item for item in catalog if "name" in item}
    error: str | None = None
    parsed: dict[str, Any] | None = None
    for attempt in range(2):
        messages = build_messages(
            catalog=catalog,
            observation=observation,
            last_action_result=last_action_result,
            repair=error,
        )
        raw = completer.complete(messages)
        try:
            parsed = parse_json_object(raw)
            error = _check_decision(parsed, tools)
        except (ValueError, json.JSONDecodeError) as exc:
            error = str(exc)
            parsed = None
        if error is None and parsed is not None:
            return parsed, None
    cash = current_cash(observation)
    fallback = {
        "action": "advance",
        "rationale": "Validation failed twice; advancing with a flat cash forecast.",
        "forecasts": flat_forecasts(cash),
    }
    return fallback, error or "invalid_model_output"


def _check_decision(parsed: dict[str, Any], tools: dict[str, dict[str, Any]]) -> str | None:
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
        schema = tools[name].get("input_schema") or {"type": "object"}
        return validate_args(schema, parsed.get("args", {}))
    return "action must be 'tool' or 'advance'"
