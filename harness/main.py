"""AUTH → OPEN_SESSION → OBSERVE → DECIDE → ACT|ADVANCE."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
from typing import Any
import uuid

from harness.auth import LocalSigner
from harness.client import BenchmarkClient, ConflictError, UrllibTransport
from harness.decide import (
    HttpCompleter,
    ProbeCompleter,
    current_cash,
    decide,
    flat_forecasts,
)
from harness.fence import Fence, FenceBlock
from harness.memory import PendingMutation, RunnerState, StateStore
from harness.trajectory import Trajectory

SCENARIOS = {
    "conformance": "zhc-conformance-short-v0",
    "growth": "business-bench-growth-short-v0",
    "full": "business-bench-default-v0",
}

MANIFEST = {
    "name": "reference-company-harness",
    "framework": "reference-company-harness",
    "framework_version": "0.1.0",
    "adapter_version": "0.1.0",
    "memory_mode": "run-scoped",
    "network_mode": "restricted",
}

TERMINAL = frozenset({"completed", "bankrupt"})
STOPPED = frozenset({"cancelled", "failed"})


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    root = Path(path).resolve().parent
    for key in ("wallet_key_file", "state_dir"):
        if key in config:
            value = Path(config[key])
            config[key] = str(value if value.is_absolute() else root / value)
    model = config.setdefault("model", {})
    if "api_key_file" in model:
        value = Path(model["api_key_file"])
        model["api_key_file"] = str(value if value.is_absolute() else root / value)
    raw_scenario = str(config.get("scenario", "conformance"))
    config["scenario_id"] = SCENARIOS.get(raw_scenario, raw_scenario)
    return config


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, default=str, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _idempotency(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"[:48]


class Runner:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        store: StateStore,
        signer: LocalSigner,
        completer: Any,
        trajectory: Trajectory,
        client: BenchmarkClient | None = None,
        transport: UrllibTransport | None = None,
        fence: Fence | None = None,
        retries: dict[str, float] | None = None,
        clock: Any = time,
    ) -> None:
        self.config = config
        self.store = store
        self.signer = signer
        self.completer = completer
        self.trajectory = trajectory
        self.clock = clock
        self.state = store.load() or RunnerState(
            scenario_id=config["scenario_id"],
            wallet_address=signer.address,
        )
        self.state.wallet_address = signer.address
        self.state.scenario_id = config["scenario_id"]
        self._loop_started = clock.monotonic()
        self.client = client or BenchmarkClient(
            transport or UrllibTransport(config["host"]),
            fence=fence or Fence(),
            get_access_token=lambda: self.state.access_token,
            reauthenticate=self.authenticate,
            retries=retries,
        )

    def persist(self) -> None:
        self.state.elapsed_s += self.clock.monotonic() - self._loop_started
        self._loop_started = self.clock.monotonic()
        self.store.save(self.state)

    def authenticate(self) -> None:
        challenge = self.client.create_challenge(self.signer.address)
        signature = self.signer.sign_siwe_message(challenge["message"])
        token = self.client.verify_challenge(
            challenge_id=challenge["challenge_id"],
            wallet_address=self.signer.address,
            signature=signature,
        )
        self.state.access_token = token["access_token"]
        self.state.token_expires_at = token.get("expires_at")
        participant = token.get("participant") or {}
        self.state.wallet_address = participant.get(
            "wallet_address", self.signer.address
        )
        if self.state.phase == "new":
            self.state.phase = "authenticated"
        self.state.notes.append("authenticated")
        self.trajectory.append("auth", step=self.state.step)
        self.persist()

    def run_until_terminal(self) -> RunnerState:
        budgets = self.config.get("budgets") or {}
        max_steps = int(budgets.get("max_steps", 600))
        wall_s = float(budgets.get("wall_clock_h", 24)) * 3600
        while self.state.phase != "terminal":
            if self.state.step >= max_steps:
                raise RuntimeError("max_steps exceeded")
            if self.state.elapsed_s >= wall_s:
                raise RuntimeError("wall_clock exceeded")
            self.step()
        return self.state

    def step(self) -> RunnerState:
        if self.state.pending is not None:
            self._replay_pending()
            return self.state
        if self.state.access_token is None or self.state.phase == "new":
            self.authenticate()
            return self.state
        if self.state.run_id is None:
            self._submit(
                "create",
                "/v1/runs",
                {
                    "benchmark_version": "business-bench-saas-v0",
                    "track": self.config.get("track", "practice"),
                    "scenario_id": self.state.scenario_id,
                    "participant_manifest": MANIFEST,
                },
            )
            return self.state
        if self.state.status in TERMINAL:
            self._fetch_terminal()
            return self.state
        if self.state.status in STOPPED:
            self._stop_without_score()
            return self.state
        self._observe_decide()
        return self.state

    def _queue(self, kind: str, path: str, body: dict[str, Any]) -> PendingMutation:
        pending = PendingMutation(
            kind=kind,
            path=path,
            idempotency_key=_idempotency(kind),
            body=body,
        )
        self.state.pending = pending
        self.persist()
        return pending

    def _dispatch(self, pending: PendingMutation) -> dict[str, Any] | None:
        try:
            if pending.kind == "create":
                return self.client.create_run(
                    idempotency_key=pending.idempotency_key,
                    participant_manifest=pending.body["participant_manifest"],
                    scenario_id=pending.body["scenario_id"],
                    track=pending.body["track"],
                )
            if pending.kind == "action":
                assert self.state.run_id is not None
                return self.client.execute_actions(
                    self.state.run_id,
                    idempotency_key=pending.idempotency_key,
                    sequence=pending.body["sequence"],
                    actions=pending.body["actions"],
                )
            if pending.kind == "advance":
                assert self.state.run_id is not None
                return self.client.advance(
                    self.state.run_id,
                    idempotency_key=pending.idempotency_key,
                    sequence=pending.body["sequence"],
                    rationale=pending.body["rationale"],
                    forecasts=pending.body["forecasts"],
                )
            raise RuntimeError(f"unknown pending mutation: {pending.kind}")
        except FenceBlock as exc:
            self.trajectory.append("FENCE_BLOCK", step=self.state.step, tool=exc.tool)
            self.state.last_action_result = {"error": str(exc)}
            self.state.notes.append(f"fence:{exc.tool}")
            self.state.pending = None
            self.persist()
            return None
        except ConflictError:
            # Create has no run_id yet: keep the key so resume can replay.
            if pending.kind == "create" and self.state.run_id is None:
                raise
            if self.state.run_id:
                self._refresh_run()
            self.state.pending = None
            self.persist()
            return None

    def _finish(self, pending: PendingMutation, result: dict[str, Any]) -> None:
        if pending.kind == "create":
            self._apply_run(result)
            self.state.phase = "running"
            self.state.notes.append(f"created:{result['run_id']}")
            self.trajectory.append("create", run_id=result["run_id"], step=self.state.step)
        elif pending.kind == "action":
            tool = pending.body["actions"][0]["tool"]
            self.state.sequence = int(result["sequence"])
            self.state.last_tool = tool
            self.state.last_action_result = result
            self.state.notes.append(f"acted:{tool}")
            self.trajectory.append(
                "act",
                step=self.state.step,
                tool=tool,
                http_status=200,
                args_digest=_digest(pending.body["actions"][0].get("arguments", {})),
            )
        elif pending.kind == "advance":
            self.state.sequence = int(result["sequence"])
            self.state.simulated_day = int(result["simulated_day"])
            self.state.status = result["status"]
            self.state.last_observation = result
            self.trajectory.append(
                "advance",
                step=self.state.step,
                http_status=200,
                forecast=pending.body["forecasts"],
                forecast_fallback=self.state.forecast_fallback,
            )
            if self.state.status in TERMINAL:
                self.state.pending = None
                self._fetch_terminal()
                return
            if self.state.status in STOPPED:
                self.state.pending = None
                self._stop_without_score()
                return
            self.state.notes.append("advanced")
        self.state.pending = None
        self.persist()

    def _submit(self, kind: str, path: str, body: dict[str, Any]) -> None:
        pending = self._queue(kind, path, body)
        result = self._dispatch(pending)
        if result is not None:
            self._finish(pending, result)

    def _replay_pending(self) -> None:
        pending = self.state.pending
        assert pending is not None
        result = self._dispatch(pending)
        if result is None:
            return
        self.state.notes.append(f"replayed:{pending.kind}")
        self.trajectory.append("replay", kind=pending.kind, step=self.state.step)
        self._finish(pending, result)

    def _observe_decide(self) -> None:
        assert self.state.run_id is not None
        observation = self.client.get_observation(self.state.run_id)
        self.state.sequence = int(observation["sequence"])
        self.state.simulated_day = int(observation["simulated_day"])
        self.state.status = observation["status"]
        self.state.last_observation = observation
        if self.state.status in TERMINAL:
            self._fetch_terminal()
            return
        if self.state.status in STOPPED:
            self._stop_without_score()
            return
        tools = self.client.get_tools(self.state.run_id)
        catalog = list(tools.get("tools") or [])
        self.client.fence.set_allowlist(
            [item["name"] for item in catalog if "name" in item]
        )
        decision, repair = decide(
            self.completer,
            catalog=catalog,
            observation=observation,
            last_action_result=self.state.last_action_result,
        )
        self.state.step += 1
        self.state.forecast_fallback = repair is not None
        self.trajectory.append(
            "decide",
            step=self.state.step,
            observation_digest=_digest(observation),
            action=decision.get("action"),
            tool=decision.get("tool"),
            validation=repair or "ok",
            forecast_fallback=repair is not None,
        )
        if decision.get("action") == "tool":
            self._submit(
                "action",
                f"/v1/runs/{self.state.run_id}/actions",
                {
                    "sequence": self.state.sequence,
                    "actions": [
                        {
                            "tool": str(decision["tool"]),
                            "arguments": dict(decision.get("args") or {}),
                        }
                    ],
                },
            )
            return
        forecasts = list(
            decision.get("forecasts") or flat_forecasts(current_cash(observation))
        )
        self._submit(
            "advance",
            f"/v1/runs/{self.state.run_id}/advance",
            {
                "sequence": self.state.sequence,
                "rationale": str(decision.get("rationale") or "Advance."),
                "forecasts": forecasts,
            },
        )

    def _stop_without_score(self) -> None:
        self.state.phase = "terminal"
        self.state.notes.append(self.state.status or "stopped")
        self.trajectory.append(
            "stopped",
            step=self.state.step,
            status=self.state.status,
        )
        self.persist()

    def _fetch_terminal(self) -> None:
        assert self.state.run_id is not None
        score = self.client.get_score(self.state.run_id)
        host_trajectory = self.client.get_trajectory(self.state.run_id)
        self.state.phase = "terminal"
        self.state.status = score.get("terminal_state", self.state.status)
        self.state.notes.append("terminal")
        self.trajectory.append(
            "terminal",
            step=self.state.step,
            primary_score=score.get("primary_score"),
            terminal_state=score.get("terminal_state"),
            trajectory_digest=host_trajectory.get("trajectory_digest"),
        )
        self.persist()
        if not self.config.get("file_terminal_ticket", True):
            return
        ticket = self.client.create_ticket(
            idempotency_key=f"terminal-ticket-{self.state.run_id}",
            kind="score_report",
            severity="low",
            title=f"{self.state.scenario_id} {score.get('terminal_state')}",
            body=(
                f"scenario_id={self.state.scenario_id} "
                f"terminal_state={score.get('terminal_state')} "
                f"primary_score={score.get('primary_score')} "
                f"simulated_day={self.state.simulated_day}"
            ),
            run_id=self.state.run_id,
            phase="after_run",
            tags=["reference-harness", "score-report"],
            score_snapshot={
                "source": "host",
                "primary_score": score.get("primary_score"),
                "terminal_state": score.get("terminal_state"),
                "simulated_day": self.state.simulated_day,
            },
        )
        self.state.notes.append(f"ticket:{ticket.get('ticket_id')}")
        self.persist()

    def _refresh_run(self) -> None:
        assert self.state.run_id is not None
        self._apply_run(self.client.get_run(self.state.run_id))

    def _apply_run(self, run: dict[str, Any]) -> None:
        self.state.run_id = run["run_id"]
        self.state.sequence = int(run["sequence"])
        self.state.simulated_day = int(run.get("simulated_day") or 0)
        self.state.status = run.get("status")


def build_runner(config: dict[str, Any], *, completer: Any | None = None) -> Runner:
    state_dir = Path(config["state_dir"])
    model = config.get("model") or {}
    if completer is None:
        provider_url = str(model.get("provider_url") or "")
        if provider_url == "probe://local":
            completer = ProbeCompleter()
        else:
            api_key = Path(model["api_key_file"]).read_text(encoding="utf-8").strip()
            completer = HttpCompleter(
                provider_url=provider_url,
                model=model["name"],
                api_key=api_key,
                max_tokens=int(model.get("max_tokens", 4096)),
                timeout_s=float(
                    (config.get("budgets") or {}).get("call_timeout_s", 120)
                ),
            )
    return Runner(
        config=config,
        store=StateStore(state_dir / "state.json"),
        signer=LocalSigner(Path(config["wallet_key_file"])),
        completer=completer,
        trajectory=Trajectory(state_dir / "trajectory.jsonl"),
        transport=UrllibTransport(
            config["host"],
            timeout_s=float((config.get("budgets") or {}).get("call_timeout_s", 120)),
        ),
        fence=Fence(),
        retries=config.get("retries"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reference company harness")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Operate until the host reports a terminal state")
    run_p.add_argument("--config", type=Path, required=True)
    export_p = sub.add_parser("export", help="Print trajectory.jsonl")
    export_p.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "export":
        print(Trajectory(Path(args.state_dir) / "trajectory.jsonl").export_text(), end="")
        return 0
    runner = build_runner(load_config(args.config))
    print(json.dumps(runner.run_until_terminal().to_public_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
