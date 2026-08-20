"""401 renew+replay, 409-as-success, fence last gate, transient retry."""

from __future__ import annotations

from typing import Any
import unittest

from harness.client import BenchmarkClient, ConflictError, TransportResponse
from harness.fence import Fence, FenceBlock


class ScriptedTransport:
    def __init__(self, responses: list[TransportResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> TransportResponse:
        self.calls.append((method, path, dict(headers or {})))
        if not self.responses:
            raise AssertionError(f"unexpected {method} {path}")
        return self.responses.pop(0)


class ClientTests(unittest.TestCase):
    def test_401_renews_and_replays(self) -> None:
        tokens = {"value": "old"}
        auths = {"count": 0}
        transport = ScriptedTransport(
            [
                TransportResponse(401, {"detail": "invalid_bearer_token"}, "no"),
                TransportResponse(200, {"run_id": "run_1", "sequence": 0}, "ok"),
            ]
        )

        def reauth() -> None:
            auths["count"] += 1
            tokens["value"] = "new"

        client = BenchmarkClient(
            transport,
            fence=Fence({"get_cost_info"}),
            get_access_token=lambda: tokens["value"],
            reauthenticate=reauth,
            sleeper=lambda _: None,
        )
        body = client.get_run("run_1")
        self.assertEqual(body["run_id"], "run_1")
        self.assertEqual(auths["count"], 1)
        self.assertEqual(transport.calls[1][2]["Authorization"], "Bearer new")

    def test_409_is_conflict_error(self) -> None:
        transport = ScriptedTransport(
            [TransportResponse(409, {"detail": "sequence_conflict"}, "no")]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence(),
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            sleeper=lambda _: None,
        )
        with self.assertRaises(ConflictError) as ctx:
            client.get_run("run_1")
        self.assertEqual(ctx.exception.detail, "sequence_conflict")

    def test_execute_actions_is_fenced_before_wire(self) -> None:
        transport = ScriptedTransport([])
        client = BenchmarkClient(
            transport,
            fence=Fence({"get_cost_info"}),
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            sleeper=lambda _: None,
        )
        with self.assertRaises(FenceBlock):
            client.execute_actions(
                "run_1",
                idempotency_key="k" * 16,
                sequence=0,
                actions=[{"tool": "send", "arguments": {}}],
            )
        self.assertEqual(transport.calls, [])

    def test_5xx_retries_then_succeeds(self) -> None:
        sleeps: list[float] = []
        transport = ScriptedTransport(
            [
                TransportResponse(503, {"detail": "busy"}, "no"),
                TransportResponse(200, {"ok": True}, "ok"),
            ]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence(),
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            retries={"base_s": 1, "cap_s": 1, "max_attempts": 3},
            sleeper=sleeps.append,
        )
        body = client.get_score("run_1")
        self.assertEqual(body, {"ok": True})
        self.assertEqual(sleeps, [1.0])
