"""Last-gate policy allowlist. Blocks real-money surfaces and unknown tools."""

from __future__ import annotations

BLOCKED_EXACT = frozenset({"send", "trade", "offramp"})


class FenceBlock(RuntimeError):
    def __init__(self, tool: str, reason: str) -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(f"FENCE_BLOCK {tool}: {reason}")


class Fence:
    def __init__(self, allowlist: set[str] | None = None) -> None:
        self.allowlist: set[str] = set(allowlist or ())

    def set_allowlist(self, names: list[str]) -> None:
        self.allowlist = {name for name in names if name not in BLOCKED_EXACT}

    def check(self, tool: str) -> None:
        if tool in BLOCKED_EXACT:
            raise FenceBlock(tool, "real-money surface")
        if tool not in self.allowlist:
            raise FenceBlock(tool, "not in published catalog allowlist")
