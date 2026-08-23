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


def _acquisition(observation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return {}
    data = observation.get("data") if isinstance(observation.get("data"), dict) else observation
    acquired = data.get("acquisition")
    return acquired if isinstance(acquired, dict) else {}


def ads_are_on(observation: dict[str, Any] | None) -> bool:
    current = _acquisition(observation).get("current_daily_spend")
    if not isinstance(current, dict):
        return False
    for groups in current.values():
        if not isinstance(groups, dict):
            continue
        for amount in groups.values():
            if isinstance(amount, (int, float)) and float(amount) > 0:
                return True
    return False


def dead_acquisition(observation: dict[str, Any] | None) -> bool:
    last_week = _acquisition(observation).get("last_week")
    if not isinstance(last_week, dict):
        return False
    spend = last_week.get("total_spend")
    leads = last_week.get("total_leads")
    if not isinstance(spend, (int, float)) or not isinstance(leads, (int, float)):
        return False
    return float(spend) > 0 and int(leads) == 0


def _raising_ads(decision: dict[str, Any]) -> bool:
    if decision.get("action") != "tool" or decision.get("tool") != AD_TOOL:
        return False
    args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
    return args.get("targeted_spend") != {}


def apply_runway(
    decision: dict[str, Any],
    *,
    cash: float,
    last_cash: float | None,
    observation: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """If cash is falling, thin, or ads bought zero leads, clear spend."""
    if decision.get("action") == "tool" and decision.get("tool") == AD_TOOL:
        args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
        if args.get("targeted_spend") == {}:
            return decision, None
    should_cut = (
        cash_fell(cash, last_cash)
        or cash < CRITICAL_CASH
        or dead_acquisition(observation)
    )
    if not should_cut:
        return decision, None
    if not _raising_ads(decision) and not ads_are_on(observation):
        return decision, None
    return (
        {
            "action": "tool",
            "tool": AD_TOOL,
            "args": {"targeted_spend": {}},
        },
        "runway:cut_ads",
    )
