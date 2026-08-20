"""Atomic state.json persistence. The only writer of durable run memory."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any


SECRET_FIELDS = frozenset({"access_token"})


@dataclass
class PendingMutation:
    kind: str
    path: str
    idempotency_key: str
    body: dict[str, Any]


@dataclass
class RunnerState:
    phase: str = "new"
    run_id: str | None = None
    sequence: int = 0
    simulated_day: int = 0
    status: str | None = None
    scenario_id: str = "zhc-conformance-short-v0"
    access_token: str | None = None
    token_expires_at: str | None = None
    wallet_address: str | None = None
    pending: PendingMutation | None = None
    last_observation: dict[str, Any] | None = None
    last_action_result: dict[str, Any] | None = None
    last_tool: str | None = None
    step: int = 0
    elapsed_s: float = 0.0
    forecast_fallback: bool = False
    notes: list[str] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in SECRET_FIELDS:
            payload.pop(key, None)
        for key in ("last_observation", "last_action_result"):
            value = payload.get(key)
            if isinstance(value, dict):
                blob = json.dumps(value, default=str, sort_keys=True).encode("utf-8")
                payload[key] = {
                    "digest": hashlib.sha256(blob).hexdigest()[:16],
                    "keys": sorted(value),
                }
        return payload


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RunnerState | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        pending = raw.get("pending")
        if pending is not None:
            raw["pending"] = PendingMutation(**pending)
        return RunnerState(**raw)

    def save(self, state: RunnerState) -> None:
        payload = asdict(state)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
