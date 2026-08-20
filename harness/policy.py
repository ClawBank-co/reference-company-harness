"""Baseline company policy for the reference rung. Not used by thin/raw."""

from __future__ import annotations

from typing import Any

AD_TOOL = "set_targeted_ad_spend"
CRITICAL_CASH = 250_000.0
LOG_KEEP = 12


def cash_fell(cash: float, last_cash: float | None) -> bool:
    return last_cash is not None and cash < last_cash


def week_note(
    *,
    day: int,
    cash: float,
    last_cash: float | None,
    last_tool: str | None,
) -> str:
    if last_cash is None:
        delta = "n/a"
    else:
        delta = str(int(round(cash - last_cash)))
    return (
        f"day={int(day)} cash={int(round(cash))} "
        f"delta={delta} last={last_tool or '-'}"
    )


def trim_log(lines: list[str], keep: int = LOG_KEEP) -> list[str]:
    return list(lines[-keep:])


def apply_runway(
    decision: dict[str, Any],
    *,
    cash: float,
    last_cash: float | None,
) -> tuple[dict[str, Any], str | None]:
    """If cash is falling or thin, clear ads instead of buying more."""
    if decision.get("action") != "tool" or decision.get("tool") != AD_TOOL:
        return decision, None
    critical = cash < CRITICAL_CASH
    if not cash_fell(cash, last_cash) and not critical:
        return decision, None
    args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
    spend = args.get("targeted_spend")
    if spend == {}:
        return decision, None
    return (
        {
            "action": "tool",
            "tool": AD_TOOL,
            "args": {"targeted_spend": {}},
        },
        "runway:cut_ads",
    )
