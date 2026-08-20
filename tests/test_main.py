"""Resume replays a pending mutation; 409 is treated as success."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any
import unittest

from eth_account import Account

from harness.client import BenchmarkClient, ConflictError, TransportResponse
from harness.fence import Fence
from harness.decide import ProbeCompleter
from harness.main import Runner, _tools_since_advance
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
        self.bodies: list[dict[str, Any] | None] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> TransportResponse:
        self.calls.append((method, path))
        self.bodies.append(json_body)
        return self.responses.pop(0)


def _empty_tickets() -> TransportResponse:
    return TransportResponse(
        200,
        {"tickets": [], "count": 0, "total": 0, "offset": 0, "limit": 1},
        "ok",
    )


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
        self.assertIn("advanced", runner.state.notes)
        self.assertEqual(
            transport.calls,
            [("POST", "/v1/runs/run_1/advance"), ("GET", "/v1/runs/run_1")],
        )

    def test_advance_422_does_not_crash_running_week(self) -> None:
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
                    idempotency_key="advance-bad-forecast-0001",
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
                TransportResponse(422, {"detail": "request_validation_failed"}, "no"),
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
        self.assertEqual(runner.state.phase, "running")
        self.assertEqual(runner.state.simulated_day, 0)
        self.assertIn("rejected:advance", runner.state.notes)
        self.assertNotIn("advanced", runner.state.notes)

    def test_max_steps_forces_last_advance_to_score_the_week(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                sequence=3,
                access_token="tok",
                status="running",
                step=3,
                scenario_id="zhc-conformance-short-v0",
                last_observation={"data": {"dashboard": "Cash: $1000000"}},
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 4,
                        "simulated_day": 7,
                        "status": "completed",
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {"primary_score": 987995.0, "terminal_state": "completed"},
                    "ok",
                ),
                TransportResponse(200, {"trajectory_digest": "abc"}, "ok"),
                _empty_tickets(),
                TransportResponse(201, {"ticket_id": "tkt_budget"}, "ok"),
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
            config={
                "scenario_id": "zhc-conformance-short-v0",
                "budgets": {"max_steps": 3},
            },
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=lambda messages: "",
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        state = runner.run_until_terminal()
        self.assertEqual(state.phase, "terminal")
        self.assertEqual(state.status, "completed")
        self.assertIn("ticket:score_report:tkt_budget", state.notes)
        self.assertEqual(
            transport.calls,
            [
                ("POST", "/v1/runs/run_1/advance"),
                ("GET", "/v1/runs/run_1/score"),
                ("GET", "/v1/runs/run_1/trajectory"),
                ("GET", "/v1/runs/run_1/tickets"),
                ("POST", "/v1/tickets"),
            ],
        )

    def test_action_409_while_running_counts_toward_cadence(self) -> None:
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
                        "actions": [{"tool": "set_prices", "arguments": {"price_a": 22}}],
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
                        "sequence": 3,
                        "simulated_day": 0,
                        "status": "running",
                    },
                    "ok",
                ),
            ]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence({"set_prices"}),
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
        self.assertIn("acted:set_prices", runner.state.notes)
        self.assertEqual(_tools_since_advance(runner.state.notes), 1)

    def test_action_400_does_not_crash_running_week(self) -> None:
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
                    idempotency_key="action-bad-args-0001",
                    body={
                        "sequence": 2,
                        "actions": [{"tool": "set_prices", "arguments": {"price_a": -1}}],
                    },
                ),
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(400, {"detail": "tool_not_published"}, "no"),
            ]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence({"set_prices"}),
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
        self.assertEqual(runner.state.phase, "running")
        self.assertIn("rejected:set_prices", runner.state.notes)
        self.assertEqual(_tools_since_advance(runner.state.notes), 1)

    def test_action_503_stops_without_raising(self) -> None:
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
                    idempotency_key="action-busy-0001",
                    body={
                        "sequence": 2,
                        "actions": [{"tool": "set_prices", "arguments": {}}],
                    },
                ),
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(503, {"detail": "run_worker_unavailable"}, "no"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 2,
                        "simulated_day": 0,
                        "status": "running",
                    },
                    "ok",
                ),
            ]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence({"set_prices"}),
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            sleeper=lambda _: None,
            retries={"max_attempts": 0},
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
        loaded = store.load()
        assert loaded is not None
        self.assertIsNone(loaded.pending)
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(loaded.run_id, "run_1")
        self.assertIn("host:unavailable", loaded.notes)

    def test_action_200_without_sequence_refreshes_and_keeps_week(self) -> None:
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
                    idempotency_key="action-soft-body-0001",
                    body={
                        "sequence": 2,
                        "actions": [{"tool": "get_cost_info", "arguments": {}}],
                    },
                ),
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(200, {"results": []}, "ok"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 3,
                        "simulated_day": 0,
                        "status": "running",
                    },
                    "ok",
                ),
            ]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence({"get_cost_info"}),
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
        self.assertEqual(runner.state.phase, "running")
        self.assertEqual(runner.state.sequence, 3)
        self.assertIn("acted:get_cost_info", runner.state.notes)

    def test_action_409_runtime_lost_stops_without_score(self) -> None:
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
                TransportResponse(409, {"detail": "runtime_lost"}, "no"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 2,
                        "simulated_day": 0,
                        "status": "failed",
                    },
                    "ok",
                ),
            ]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence({"get_cost_info"}),
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertIsNone(loaded.pending)
        self.assertEqual(
            transport.calls,
            [("POST", "/v1/runs/run_1/actions"), ("GET", "/v1/runs/run_1")],
        )

    def test_advance_409_completed_fetches_terminal(self) -> None:
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
                        "status": "completed",
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "primary_score": 999405.0,
                        "terminal_state": "completed",
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {"trajectory_digest": "abc"},
                    "ok",
                ),
                _empty_tickets(),
                TransportResponse(
                    201,
                    {"ticket_id": "tkt_1"},
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "completed")
        self.assertIn("ticket:score_report:tkt_1", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("POST", "/v1/runs/run_1/advance"),
                ("GET", "/v1/runs/run_1"),
                ("GET", "/v1/runs/run_1/score"),
                ("GET", "/v1/runs/run_1/trajectory"),
                ("GET", "/v1/runs/run_1/tickets"),
                ("POST", "/v1/tickets"),
            ],
        )

    def test_ticket_409_does_not_crash_completed_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="completed",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {"primary_score": 987995.0, "terminal_state": "completed"},
                    "ok",
                ),
                TransportResponse(200, {"trajectory_digest": "abc"}, "ok"),
                _empty_tickets(),
                TransportResponse(409, {"detail": "idempotency_key_conflict"}, "no"),
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "completed")
        self.assertIn("ticket:conflict", loaded.notes)

    def test_ticket_400_does_not_crash_completed_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="completed",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {"primary_score": 987995.0, "terminal_state": "completed"},
                    "ok",
                ),
                TransportResponse(200, {"trajectory_digest": "abc"}, "ok"),
                _empty_tickets(),
                TransportResponse(400, {"detail": "request_validation_failed"}, "no"),
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "completed")
        self.assertIn("ticket:failed", loaded.notes)

    def test_resume_files_missing_terminal_ticket(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="terminal",
                run_id="run_1",
                access_token="tok",
                status="completed",
                scenario_id="zhc-conformance-short-v0",
                notes=["terminal"],
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {"primary_score": 987995.0, "terminal_state": "completed"},
                    "ok",
                ),
                _empty_tickets(),
                TransportResponse(201, {"ticket_id": "tkt_retry"}, "ok"),
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
        runner.run_until_terminal()
        loaded = store.load()
        assert loaded is not None
        self.assertIn("ticket:score_report:tkt_retry", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_1/score"),
                ("GET", "/v1/runs/run_1/tickets"),
                ("POST", "/v1/tickets"),
            ],
        )

    def test_existing_host_score_report_skips_create(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="terminal",
                run_id="run_1",
                access_token="tok",
                status="completed",
                scenario_id="zhc-conformance-short-v0",
                notes=["terminal"],
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {"primary_score": 987995.0, "terminal_state": "completed"},
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "tickets": [
                            {
                                "ticket_id": "tkt_host",
                                "kind": "score_report",
                                "run_id": "run_1",
                            }
                        ],
                        "count": 1,
                        "total": 1,
                        "offset": 0,
                        "limit": 1,
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
        runner.run_until_terminal()
        loaded = store.load()
        assert loaded is not None
        self.assertIn("ticket:score_report:tkt_host", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_1/score"),
                ("GET", "/v1/runs/run_1/tickets"),
            ],
        )

    def test_other_run_score_report_does_not_skip_create(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="terminal",
                run_id="run_1",
                access_token="tok",
                status="completed",
                scenario_id="zhc-conformance-short-v0",
                notes=["terminal"],
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {"primary_score": 987995.0, "terminal_state": "completed"},
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "tickets": [
                            {
                                "ticket_id": "tkt_other",
                                "kind": "score_report",
                                "run_id": "run_other",
                            }
                        ],
                        "count": 1,
                        "total": 1,
                        "offset": 0,
                        "limit": 1,
                    },
                    "ok",
                ),
                TransportResponse(201, {"ticket_id": "tkt_this"}, "ok"),
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
        runner.run_until_terminal()
        loaded = store.load()
        assert loaded is not None
        self.assertIn("ticket:score_report:tkt_this", loaded.notes)
        self.assertNotIn("ticket:score_report:tkt_other", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_1/score"),
                ("GET", "/v1/runs/run_1/tickets"),
                ("POST", "/v1/tickets"),
            ],
        )

    def test_mid_run_ticket_does_not_skip_score_report(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="completed",
                scenario_id="zhc-conformance-short-v0",
                notes=["ticket:gym_change:tkt_clock"],
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {"primary_score": 987995.0, "terminal_state": "completed"},
                    "ok",
                ),
                TransportResponse(200, {"trajectory_digest": "abc"}, "ok"),
                _empty_tickets(),
                TransportResponse(201, {"ticket_id": "tkt_score"}, "ok"),
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
        loaded = store.load()
        assert loaded is not None
        self.assertIn("ticket:gym_change:tkt_clock", loaded.notes)
        self.assertIn("ticket:score_report:tkt_score", loaded.notes)

    def test_missing_clock_files_gym_change_ticket(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="running",
                sequence=0,
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 0,
                        "simulated_day": 0,
                        "status": "running",
                        "data": {"dashboard": "Cash: $1000000"},
                    },
                    "ok",
                ),
                _empty_tickets(),
                TransportResponse(201, {"ticket_id": "tkt_clock"}, "ok"),
                TransportResponse(
                    200,
                    {"tools": [{"name": "get_cost_info", "input_schema": {"type": "object"}}]},
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "sequence": 1,
                        "results": [
                            {"tool": "get_cost_info", "success": True, "output": "ok"}
                        ],
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

        class InspectCompleter:
            def complete(self, messages: list[dict[str, str]]) -> str:
                return '{"action":"tool","tool":"get_cost_info","args":{}}'

        runner = Runner(
            config={"scenario_id": "zhc-conformance-short-v0", "budgets": {"max_steps": 10}},
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=InspectCompleter(),
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertIn("ticket:gym_change:tkt_clock", loaded.notes)
        self.assertEqual(
            transport.calls[:4],
            [
                ("GET", "/v1/runs/run_1/observation"),
                ("GET", "/v1/runs/run_1/tickets"),
                ("POST", "/v1/tickets"),
                ("GET", "/v1/runs/run_1/tools"),
            ],
        )

    def test_empty_catalog_stops_without_deciding(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="running",
                sequence=0,
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 0,
                        "simulated_day": 0,
                        "status": "running",
                        "data": {
                            "dashboard": "Cash: $1000000",
                            "clock": {
                                "simulated_day": 0,
                                "advance_days": 7,
                                "tools_move_time": False,
                            },
                        },
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {"tools": []},
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 0,
                        "simulated_day": 0,
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
            completer=lambda messages: '{"action":"tool","tool":"set_prices","args":{}}',
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_1/observation"),
                ("GET", "/v1/runs/run_1/tools"),
                ("GET", "/v1/runs/run_1"),
            ],
        )

    def test_tools_payload_not_a_list_stops_without_deciding(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="running",
                sequence=0,
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 0,
                        "simulated_day": 0,
                        "status": "running",
                        "data": {
                            "dashboard": "Cash: $1000000",
                            "clock": {
                                "simulated_day": 0,
                                "advance_days": 7,
                                "tools_move_time": False,
                            },
                        },
                    },
                    "ok",
                ),
                TransportResponse(200, {"tools": "nope"}, "ok"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 0,
                        "simulated_day": 0,
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
            completer=lambda messages: '{"action":"tool","tool":"set_prices","args":{}}',
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")

    def test_tool_result_leak_files_docs_ticket(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="running",
                sequence=0,
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 0,
                        "simulated_day": 0,
                        "status": "running",
                        "data": {
                            "dashboard": "Cash: $1000000",
                            "clock": {
                                "simulated_day": 0,
                                "advance_days": 7,
                                "tools_move_time": False,
                            },
                        },
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "tools": [
                            {
                                "name": "get_cost_info",
                                "input_schema": {"type": "object"},
                            }
                        ]
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "sequence": 1,
                        "results": [
                            {
                                "tool": "get_cost_info",
                                "success": True,
                                "output": "Changes take effect on next_week.",
                            }
                        ],
                    },
                    "ok",
                ),
                _empty_tickets(),
                TransportResponse(201, {"ticket_id": "tkt_docs"}, "ok"),
            ]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence(),
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            sleeper=lambda _: None,
        )

        class InspectCompleter:
            def complete(self, messages: list[dict[str, str]]) -> str:
                return '{"action":"tool","tool":"get_cost_info","args":{}}'

        runner = Runner(
            config={"scenario_id": "zhc-conformance-short-v0", "budgets": {"max_steps": 10}},
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=InspectCompleter(),
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertIn("ticket:docs:tkt_docs", loaded.notes)
        output = loaded.last_action_result["results"][0]["output"]
        self.assertNotIn("next_week", output)
        self.assertEqual(transport.calls[-1], ("POST", "/v1/tickets"))

    def test_create_400_stops_without_raising(self) -> None:
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
                    idempotency_key="create-bad-body-0001",
                    body={
                        "track": "practice",
                        "scenario_id": "zhc-conformance-short-v0",
                        "participant_manifest": {"name": "t"},
                    },
                ),
            )
        )
        transport = ScriptedTransport(
            [TransportResponse(400, {"detail": "request_validation_failed"}, "no")]
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
        loaded = store.load()
        assert loaded is not None
        self.assertIsNone(loaded.pending)
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertIn("rejected:create", loaded.notes)

    def test_create_201_without_run_id_stops_without_raising(self) -> None:
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
                    idempotency_key="create-soft-body-0001",
                    body={
                        "track": "practice",
                        "scenario_id": "zhc-conformance-short-v0",
                        "participant_manifest": {"name": "t"},
                    },
                ),
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(201, {"status": "running"}, "ok"),
                TransportResponse(200, {"total": 0, "runs": []}, "ok"),
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
        loaded = store.load()
        assert loaded is not None
        self.assertIsNone(loaded.pending)
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertIn("rejected:create", loaded.notes)

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

    def test_cancelled_observation_stops_without_acting_or_scoring(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                sequence=1,
                access_token="tok",
                status="running",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 1,
                        "simulated_day": 0,
                        "status": "cancelled",
                        "data": {"dashboard": ""},
                    },
                    "ok",
                )
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
            completer=lambda messages: '{"action":"advance","rationale":"nope"}',
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "cancelled")
        self.assertIn("cancelled", loaded.notes)
        self.assertEqual(transport.calls, [("GET", "/v1/runs/run_1/observation")])

    def test_failed_status_stops_without_host_calls(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="failed",
            )
        )
        transport = ScriptedTransport([])
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(transport.calls, [])

    def test_malformed_observation_refreshes_and_stops_if_host_failed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="running",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(200, {"data": {"dashboard": ""}}, "ok"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 2,
                        "simulated_day": 0,
                        "status": "failed",
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_1/observation"),
                ("GET", "/v1/runs/run_1"),
            ],
        )

    def test_observation_409_runtime_lost_stops_without_score(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="running",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(409, {"detail": "runtime_lost"}, "no"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 2,
                        "simulated_day": 0,
                        "status": "failed",
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_1/observation"),
                ("GET", "/v1/runs/run_1"),
            ],
        )

    def test_observation_409_still_running_recovers_other_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_old",
                access_token="tok",
                status="running",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(409, {"detail": "runtime_lost"}, "no"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_old",
                        "sequence": 1,
                        "simulated_day": 0,
                        "status": "running",
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "total": 1,
                        "runs": [
                            {
                                "run_id": "run_new",
                                "sequence": 2,
                                "simulated_day": 0,
                                "status": "running",
                                "scenario_version": "zhc-conformance-short-v0",
                            }
                        ],
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "run_new")
        self.assertIn("recovered:run_new", loaded.notes)

    def test_observation_503_stops_without_raising(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_1",
                access_token="tok",
                status="running",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(503, {"detail": "run_worker_unavailable"}, "no"),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_1",
                        "sequence": 2,
                        "simulated_day": 0,
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
            retries={"max_attempts": 0},
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(loaded.run_id, "run_1")
        self.assertIn("host:unavailable", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_1/observation"),
                ("GET", "/v1/runs/run_1"),
            ],
        )

    def test_observation_404_clears_lost_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_lost",
                access_token="tok",
                status="running",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(404, {"detail": "run_not_found"}, "no"),
                TransportResponse(200, {"total": 0, "runs": []}, "ok"),
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
        loaded = store.load()
        assert loaded is not None
        self.assertIsNone(loaded.run_id)
        self.assertIn("lost-run:run_lost", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_lost/observation"),
                ("GET", "/v1/runs?limit=50"),
            ],
        )

        transport.responses.extend(
            [
                TransportResponse(200, {"total": 0, "runs": []}, "ok"),
                TransportResponse(
                    201,
                    {
                        "run_id": "run_fresh",
                        "sequence": 0,
                        "simulated_day": 0,
                        "status": "running",
                    },
                    "ok",
                ),
            ]
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "run_fresh")
        self.assertEqual(loaded.phase, "running")
        self.assertEqual(
            transport.calls[-2:],
            [("GET", "/v1/runs?limit=50"), ("POST", "/v1/runs")],
        )

    def test_observation_404_adopts_listed_running_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_old",
                access_token="tok",
                status="running",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(404, {"detail": "run_not_found"}, "no"),
                TransportResponse(
                    200,
                    {
                        "total": 1,
                        "runs": [
                            {
                                "run_id": "run_new",
                                "sequence": 2,
                                "simulated_day": 0,
                                "status": "running",
                                "scenario_version": "zhc-conformance-short-v0",
                            }
                        ],
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "run_new")
        self.assertEqual(loaded.sequence, 2)
        self.assertIn("recovered:run_new", loaded.notes)

    def test_score_404_stops_when_run_is_gone(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_done",
                access_token="tok",
                status="completed",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(404, {"detail": "run_not_found"}, "no"),
                TransportResponse(200, {"total": 0, "runs": []}, "ok"),
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertIn("lost-run:run_done", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_done/score"),
                ("GET", "/v1/runs?limit=50"),
            ],
        )

    def test_trajectory_409_keeps_score(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        digest = "ab" * 32
        store.save(
            RunnerState(
                phase="running",
                run_id="run_done",
                access_token="tok",
                status="completed",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "primary_score": 987995.0,
                        "terminal_state": "completed",
                        "receipt": "sha256:deadbeef",
                        "trajectory_digest": digest,
                    },
                    "ok",
                ),
                TransportResponse(
                    409, {"detail": "terminal_results_unavailable"}, "no"
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
            config={
                "scenario_id": "zhc-conformance-short-v0",
                "budgets": {"max_steps": 10},
                "file_terminal_ticket": False,
            },
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=lambda messages: "",
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "completed")
        self.assertIn("terminal", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_done/score"),
                ("GET", "/v1/runs/run_done/trajectory"),
            ],
        )

    def test_score_200_without_primary_score_keeps_week(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        digest = "cd" * 32
        store.save(
            RunnerState(
                phase="running",
                run_id="run_done",
                access_token="tok",
                status="completed",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "terminal_state": "completed",
                        "trajectory_digest": digest,
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {"trajectory_digest": digest},
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
            config={
                "scenario_id": "zhc-conformance-short-v0",
                "budgets": {"max_steps": 10},
                "file_terminal_ticket": False,
            },
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=lambda messages: "",
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "completed")
        self.assertIn("score:incomplete", loaded.notes)
        self.assertIn("terminal", loaded.notes)

    def test_score_409_stops_without_score(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_done",
                access_token="tok",
                status="completed",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    409, {"detail": "terminal_results_unavailable"}, "no"
                ),
                TransportResponse(
                    200,
                    {
                        "run_id": "run_done",
                        "sequence": 4,
                        "simulated_day": 7,
                        "status": "failed",
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_done/score"),
                ("GET", "/v1/runs/run_done"),
            ],
        )

    def test_score_503_keeps_completed_week(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_done",
                access_token="tok",
                status="completed",
            )
        )
        transport = ScriptedTransport(
            [TransportResponse(503, {"detail": "terminal_results_unavailable"}, "no")]
        )
        client = BenchmarkClient(
            transport,
            fence=Fence(),
            get_access_token=lambda: "tok",
            reauthenticate=lambda: None,
            sleeper=lambda _: None,
            retries={"max_attempts": 0},
        )
        runner = Runner(
            config={
                "scenario_id": "zhc-conformance-short-v0",
                "budgets": {"max_steps": 10},
                "file_terminal_ticket": False,
            },
            store=store,
            client=client,
            signer=_signer(tmp.name),
            completer=lambda messages: "",
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "completed")
        self.assertIn("score:unavailable", loaded.notes)

    def test_observation_404_pages_list_until_wanted_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_old",
                access_token="tok",
                status="running",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(404, {"detail": "run_not_found"}, "no"),
                TransportResponse(
                    200,
                    {
                        "total": 51,
                        "runs": [
                            {
                                "run_id": "run_done",
                                "sequence": 8,
                                "simulated_day": 7,
                                "status": "completed",
                                "scenario_version": "zhc-conformance-short-v0",
                            }
                        ],
                    },
                    "ok",
                ),
                TransportResponse(
                    200,
                    {
                        "total": 51,
                        "runs": [
                            {
                                "run_id": "run_new",
                                "sequence": 3,
                                "simulated_day": 0,
                                "status": "running",
                                "scenario_version": "zhc-conformance-short-v0",
                            }
                        ],
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "run_new")
        self.assertEqual(loaded.sequence, 3)
        self.assertIn("recovered:run_new", loaded.notes)
        self.assertEqual(
            transport.calls,
            [
                ("GET", "/v1/runs/run_old/observation"),
                ("GET", "/v1/runs?limit=50"),
                ("GET", "/v1/runs?limit=50&offset=1"),
            ],
        )

    def test_observation_404_does_not_readopt_same_listed_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="running",
                run_id="run_old",
                access_token="tok",
                status="running",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(404, {"detail": "run_not_found"}, "no"),
                TransportResponse(
                    200,
                    {
                        "total": 1,
                        "runs": [
                            {
                                "run_id": "run_old",
                                "sequence": 3,
                                "simulated_day": 0,
                                "status": "running",
                                "scenario_version": "zhc-conformance-short-v0",
                            }
                        ],
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
        loaded = store.load()
        assert loaded is not None
        self.assertIsNone(loaded.run_id)
        self.assertIn("lost-run:run_old", loaded.notes)
        self.assertNotIn("recovered:run_old", loaded.notes)

    def test_missing_run_id_lists_and_adopts_live_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="authenticated",
                access_token="tok",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "total": 1,
                        "runs": [
                            {
                                "run_id": "run_live",
                                "sequence": 4,
                                "simulated_day": 0,
                                "status": "running",
                                "scenario_version": "zhc-conformance-short-v0",
                            }
                        ],
                    },
                    "ok",
                )
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "run_live")
        self.assertEqual(loaded.sequence, 4)
        self.assertEqual(loaded.phase, "running")
        self.assertIn("resumed:run_live", loaded.notes)
        self.assertEqual(transport.calls, [("GET", "/v1/runs?limit=50")])

    def test_missing_run_id_creates_when_list_has_no_live_run(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="authenticated",
                access_token="tok",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(
                    200,
                    {
                        "total": 1,
                        "runs": [
                            {
                                "run_id": "run_done",
                                "sequence": 8,
                                "simulated_day": 7,
                                "status": "completed",
                                "scenario_version": "zhc-conformance-short-v0",
                            }
                        ],
                    },
                    "ok",
                ),
                TransportResponse(
                    201,
                    {
                        "run_id": "run_next",
                        "sequence": 0,
                        "simulated_day": 0,
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "run_next")
        self.assertEqual(
            transport.calls,
            [("GET", "/v1/runs?limit=50"), ("POST", "/v1/runs")],
        )

    def test_create_429_adopts_live_run(self) -> None:
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
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(429, {"detail": "run_capacity_reached"}, "no"),
                TransportResponse(
                    200,
                    {
                        "total": 1,
                        "runs": [
                            {
                                "run_id": "run_live",
                                "sequence": 1,
                                "simulated_day": 0,
                                "status": "queued",
                                "scenario_version": "zhc-conformance-short-v0",
                            }
                        ],
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
            retries={"max_attempts": 0},
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
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "run_live")
        self.assertIsNone(loaded.pending)
        self.assertIn("resumed:run_live", loaded.notes)
        self.assertEqual(
            transport.calls,
            [("POST", "/v1/runs"), ("GET", "/v1/runs?limit=50")],
        )

    def test_probe_create_publishes_protocol_probe_manifest(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(
            RunnerState(
                phase="authenticated",
                access_token="tok",
                scenario_id="zhc-conformance-short-v0",
            )
        )
        transport = ScriptedTransport(
            [
                TransportResponse(200, {"total": 0, "runs": []}, "ok"),
                TransportResponse(
                    201,
                    {
                        "run_id": "run_probe",
                        "sequence": 0,
                        "simulated_day": 0,
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
            completer=ProbeCompleter(),
            trajectory=Trajectory(Path(tmp.name) / "trajectory.jsonl"),
            clock=Clock(),
        )
        runner.step()
        create_body = next(
            body for body in transport.bodies if body and "participant_manifest" in body
        )
        self.assertEqual(create_body["participant_manifest"]["name"], "protocol-probe")
        self.assertEqual(
            create_body["participant_manifest"]["models"], ["protocol-probe"]
        )


class AuthTests(unittest.TestCase):
    def _runner(self, transport: ScriptedTransport) -> tuple[Runner, StateStore]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = StateStore(Path(tmp.name) / "state.json")
        store.save(RunnerState(phase="new"))
        client = BenchmarkClient(
            transport,
            fence=Fence(),
            get_access_token=lambda: None,
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
        return runner, store

    def test_verify_200_without_expires_at_authenticates(self) -> None:
        transport = ScriptedTransport(
            [
                TransportResponse(
                    201,
                    {"challenge_id": "ch_1", "message": "Sign in to ClawBank"},
                    "ok",
                ),
                TransportResponse(200, {"access_token": "tok"}, "ok"),
            ]
        )
        runner, store = self._runner(transport)
        runner.authenticate()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "authenticated")
        self.assertEqual(loaded.access_token, "tok")
        self.assertIsNone(loaded.token_expires_at)
        self.assertIn("authenticated", loaded.notes)
        self.assertNotIn("rejected:auth", loaded.notes)

    def test_verify_200_without_access_token_stops_without_raising(self) -> None:
        transport = ScriptedTransport(
            [
                TransportResponse(
                    201,
                    {"challenge_id": "ch_1", "message": "Sign in to ClawBank"},
                    "ok",
                ),
                TransportResponse(200, {"token_type": "Bearer"}, "ok"),
            ]
        )
        runner, store = self._runner(transport)
        runner.authenticate()
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.phase, "terminal")
        self.assertEqual(loaded.status, "failed")
        self.assertIn("rejected:auth", loaded.notes)


class ToolsSinceAdvanceTests(unittest.TestCase):
    def test_counts_tools_after_last_advance(self) -> None:
        self.assertEqual(_tools_since_advance([]), 0)
        self.assertEqual(
            _tools_since_advance(["acted:set_prices", "acted:set_capacity_tier"]),
            2,
        )
        self.assertEqual(
            _tools_since_advance(
                [
                    "acted:set_prices",
                    "advanced",
                    "acted:set_daily_spend",
                    "replayed:action",
                    "acted:set_model_tiers",
                ]
            ),
            2,
        )

    def test_inspect_tools_do_not_consume_mutation_budget(self) -> None:
        self.assertEqual(
            _tools_since_advance(
                [
                    "acted:get_cost_info",
                    "acted:set_prices",
                    "acted:set_capacity_tier",
                ]
            ),
            2,
        )
        self.assertEqual(
            _tools_since_advance(["acted:get_cost_info"] * 3),
            0,
        )
        self.assertEqual(
            _tools_since_advance(["acted:get_cost_info"] * 6),
            3,
        )
        self.assertEqual(
            _tools_since_advance(["acted:get_cost_info"] * 6, cadence="raw"),
            6,
        )
        self.assertEqual(
            _tools_since_advance(["fence:send", "fence:trade", "fence:offramp"]),
            3,
        )
        self.assertEqual(
            _tools_since_advance(["rejected:set_prices"] * 3),
            3,
        )
