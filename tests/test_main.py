"""Resume replays a pending mutation; 409 is treated as success."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any
import unittest

from eth_account import Account

from harness.client import BenchmarkClient, ConflictError, TransportResponse
from harness.fence import Fence
from harness.main import Runner
from harness.memory import PendingMutation, RunnerState, StateStore
from harness.trajectory import Trajectory


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        self.t += 1.0
        return self.t


class ScriptedTransport:
    def __init__(self, responses: list[TransportResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> TransportResponse:
        self.calls.append((method, path))
        return self.responses.pop(0)


def _signer(tmp: str):
    from harness.auth import LocalSigner

    path = Path(tmp) / "key.hex"
    account = Account.from_key("0x" + "ab" * 32)
    path.write_text("0x" + account.key.hex(), encoding="utf-8")
    return LocalSigner(path)


class ReplayTests(unittest.TestCase):
    def test_resume_replays_pending_action_with_same_key(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                sequence=2,
                access_token="tok",
                pending=PendingMutation(
                    kind="action",
                    path="/v1/runs/run_1/actions",
                    idempotency_key="action-same-key-0001",
                    body={
                        "sequence": 2,
                        "actions": [{"tool": "get_cost_info", "arguments": {}}],
                    },
                ),
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {"sequence": 3, "results": [{"tool": "get_cost_info", "success": True, "output": "ok"}]},
                    "ok",
                )
            ]
        )
        fence = Fence({"get_cost_info"})
        client = BenchmarkClient(
            transport,
            fence=fence,
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            sleeper=lambda _: None,
        )
        runner = Runner(
            config={"scenario_id": "zhc-conformance-short-v0", "budgets": {"max_steps": 10}},
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=lambda messages: "",
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        self.assertIsNone(runner.state.pending)
        self.assertEqual(runner.state.sequence, 3)
        self.assertEqual(transport.calls, [("POST", "/v1/runs/run_1/actions")])
        loaded = store.load()
        assert loaded is not None
        self.assertIsNone(loaded.pending)
        self.assertIn("replayed:action", loaded.notes)

    def test_409_on_advance_refreshes_and_clears_pending(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                sequence=4,
                access_token="tok",
                pending=PendingMutation(
                    kind="advance",
                    path="/v1/runs/run_1/advance",
                    idempotency_key="advance-same-key-0001",
                    body={
                        "sequence": 4,
                        "rationale": "hold",
                        "forecasts": [],
                    },
                ),
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(409, {"detail": "sequence_conflict"}, "no"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 5,
                        "simulated_day": 7,
                        "status": "running",
                    },
                    "ok",
                ),
            ]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence(),
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            sleeper=lambda _: None,
        )
        runner = Runner(
            config={"scenario_id": "zhc-conformance-short-v0", "budgets": {"max_steps": 10}},
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=lambda messages: "",
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        self.assertIsNone(runner.state.pending)
        self.assertEqual(runner.state.sequence, 5)
        self.assertEqual(
            transport.calls,
            [("POST", "/v1/runs/run_1/advance"), ("GET", "/v1/runs/run_1")],
        )

    def test_create_409_keeps_pending_key(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="authenticated",
                access_token="tok",
                pending=PendingMutation(
                    kind="create",
                    path="/v1/runs",
                    idempotency_key="create-same-key-0001",
                    body={
                        "track": "practice",
                        "scenario_id": "zhc-conformance-short-v0",
                        "participant_manifest": {"name": "t"},
                    },
                ),
            )
        )
        transport = ScriptedTransport(
            [TransportResponse(409, {"detail": "idempotency_key_conflict"}, "no")]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence(),
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            sleeper=lambda _: None,
        )
        runner = Runner(
            config={"scenario_id": "zhc-conformance-short-v0", "budgets": {"max_steps": 10}},
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=lambda messages: "",
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        with self.assertRaises(ConflictError):
            runner.step()
        loaded = store.load()
        assert loaded is not None
        assert loaded.pending is not None
        self.assertEqual(loaded.pending.idempotency_key, "create-same-key-0001")
