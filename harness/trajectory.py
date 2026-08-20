"""Append-only JSONL audit log. No keys, tokens, signatures, or model prose."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

REDACT_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "api_key",
        "private_key",
        "signature",
        "bearer",
        "wallet_key",
    }
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.lower() in REDACT_KEYS:
                out[key] = "[redacted]"
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class Trajectory:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **_redact(fields),
        }
        if "reasoning" in record or "prose" in record or "message" in record:
            for banned in ("reasoning", "prose", "message"):
                record.pop(banned, None)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    def export_text(self) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")
