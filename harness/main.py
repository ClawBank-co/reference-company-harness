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
from harness.client import (
    BenchmarkClient,
    BenchmarkError,
    ConflictError,
    UrllibTransport,
)
from harness.decide import (
    LOCAL_ONLY_TOOLS,
    HttpCompleter,
    ProbeCompleter,
    current_cash,
    decide,
    flat_forecasts,
    host_omitted_clock,
    payload_leaks_local_protocol,
    published_tools,
    sanitize_catalog,
    sanitize_hosted_payload,
)
from harness.fence import Fence, FenceBlock
from harness.memory import PendingMutation, RunnerState, StateStore
from harness.policy import apply_runway, cash_fell, trim_log, week_note
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

PROBE_MANIFEST = {
    **MANIFEST,
    "name": "protocol-probe",
    "models": ["protocol-probe"],
}

THIN_MANIFEST = {
    **MANIFEST,
    "name": "thin-model",
    "framework": "thin-model",
}

TERMINAL = frozenset({"completed", "bankrupt"})
STOPPED = frozenset({"cancelled", "failed"})


def parse_observation_clock(payload: Any) -> tuple[int, int, str] | None:
    if not isinstance(payload, dict):
        return None
    try:
        sequence = int(payload["sequence"])
        simulated_day = int(payload["simulated_day"])
    except (KeyError, TypeError, ValueError):
        return None
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        return None
    return sequence, simulated_day, status


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
    rung = str(config.get("rung") or "reference").strip().lower()
    config["rung"] = "raw" if rung == "raw" else "reference"
    return config


def _tools_since_advance(notes: list[str], *, cadence: str = "reference") -> int:
    """Cadence pressure since the last advance.

    Mutating tools count toward the 3-tool advance cap. Inspect tools
    (names starting with get_) do not, so a week can read cost info
    before pricing. Fenced calls count as mutations so send/trade/offramp
    cannot burn max_steps. Six tools of any kind still force advance.
    """
    mutations = 0
    total = 0
    for note in reversed(notes):
        if note.startswith("advanced"):
            break
        if note.startswith("fence:") or note.startswith("rejected:"):
            total += 1
            mutations += 1
            continue
        if not note.startswith("acted:"):
            continue
        total += 1
        tool = note.split(":", 1)[1]
        if not tool.startswith("get_"):
            mutations += 1
    if cadence == "raw":
        return total
    if total >= 6:
        return 3
    return mutations


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
        manifest: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.signer = signer
        self.completer = completer
        if manifest is not None:
            self.manifest = manifest
        elif isinstance(completer, ProbeCompleter):
            self.manifest = PROBE_MANIFEST
        else:
            self.manifest = MANIFEST
        self.trajectory = trajectory
        self.clock = clock
        self.state = store.load() or RunnerState(
            scenario_id=config["scenario_id"],
            wallet_address=signer.address,
        )
        self.state.wallet_address = signer.address
        self.state.scenario_id = config["scenario_id"]
        self._loop_started = clock.monotonic()
        self._unusable_run_ids: set[str] = set()
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

    def _cadence(self) -> str:
        return "raw" if self.config.get("rung") == "raw" else "reference"

    def authenticate(self) -> None:
        try:
            challenge = self.client.create_challenge(self.signer.address)
        except BenchmarkError:
            self._fail_auth()
            return
        message = challenge.get("message") if isinstance(challenge, dict) else None
        challenge_id = (
            challenge.get("challenge_id") if isinstance(challenge, dict) else None
        )
        if not isinstance(message, str) or not message:
            self._fail_auth()
            return
        if not isinstance(challenge_id, str) or not challenge_id:
            self._fail_auth()
            return
        try:
            token = self.client.verify_challenge(
                challenge_id=challenge_id,
                wallet_address=self.signer.address,
                signature=self.signer.sign_siwe_message(message),
            )
        except BenchmarkError:
            self._fail_auth()
            return
        if not self._apply_session(token):
            self._fail_auth()

    def _apply_session(self, token: Any) -> bool:
        if not isinstance(token, dict):
            return False
        access = token.get("access_token")
        if not isinstance(access, str) or not access:
            return False
        self.state.access_token = access
        expires = token.get("expires_at")
        self.state.token_expires_at = expires if isinstance(expires, str) else None
        participant = token.get("participant")
        wallet = None
        if isinstance(participant, dict):
            wallet = participant.get("wallet_address")
        self.state.wallet_address = (
            wallet if isinstance(wallet, str) and wallet else self.signer.address
        )
        if self.state.phase == "new":
            self.state.phase = "authenticated"
        self.state.notes.append("authenticated")
        self.trajectory.append("auth", step=self.state.step)
        self.persist()
        return True

    def _fail_auth(self) -> None:
        if "rejected:auth" not in self.state.notes:
            self.state.notes.append("rejected:auth")
        if self.state.status not in TERMINAL:
            self.state.status = "failed"
        self._stop_without_score()

    def run_until_terminal(self) -> RunnerState:
        budgets = self.config.get("budgets") or {}
        max_steps = int(budgets.get("max_steps", 600))
        wall_s = float(budgets.get("wall_clock_h", 24)) * 3600
        while self.state.phase != "terminal":
            if self.state.pending is not None:
                self.step()
                continue
            if self.state.step >= max_steps:
                self._stop_on_budget("max_steps")
                break
            if self.state.elapsed_s >= wall_s:
                self._stop_on_budget("wall_clock")
                break
            self.step()
        self._ensure_terminal_ticket()
        return self.state

    def _stop_on_budget(self, reason: str) -> None:
        if self.state.status in TERMINAL:
            self._fetch_terminal()
            return
        if self.state.status in STOPPED:
            self._stop_without_score()
            return
        if self.state.run_id is not None and self.state.status == "running":
            try:
                cash = current_cash(self.state.last_observation or {})
                self._submit(
                    "advance",
                    f"/v1/runs/{self.state.run_id}/advance",
                    {
                        "sequence": self.state.sequence,
                        "rationale": (
                            f"Harness {reason} budget exhausted; "
                            "advance to score the week."
                        ),
                        "forecasts": flat_forecasts(cash),
                    },
                )
            except Exception:
                pass
        if self.state.phase == "terminal":
            return
        if self.state.status not in STOPPED | TERMINAL:
            self.state.status = "failed"
        self._file_gap_ticket(
            kind="blocked",
            title=f"Harness stopped on {reason}",
            body=(
                f"{reason} budget exhausted before the host went terminal. "
                f"run_id={self.state.run_id} "
                f"simulated_day={self.state.simulated_day}."
            ),
            tags=["loop", "budget", reason],
        )
        self._stop_without_score()

    def step(self) -> RunnerState:
        if self.state.pending is not None:
            self._replay_pending()
            return self.state
        if self.state.access_token is None or self.state.phase == "new":
            self.authenticate()
            return self.state
        if self.state.run_id is None:
            if self._adopt_live_run():
                return self.state
            self._submit(
                "create",
                "/v1/runs",
                {
                    "benchmark_version": "business-bench-saas-v0",
                    "track": self.config.get("track", "practice"),
                    "scenario_id": self.state.scenario_id,
                    "participant_manifest": self.manifest,
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
            if self.state.run_id is None:
                self.persist()
                return None
            if self.state.status in STOPPED:
                self._stop_without_score()
                return None
            if self.state.status in TERMINAL:
                self._fetch_terminal()
                return None
            if pending.kind == "action":
                tool = pending.body["actions"][0]["tool"]
                self.state.notes.append(f"acted:{tool}")
            elif pending.kind == "advance":
                self.state.notes.append("advanced")
            self.persist()
            return None
        except BenchmarkError as exc:
            if pending.kind in {"action", "advance"} and exc.status_code in {400, 422}:
                label = (
                    pending.body["actions"][0]["tool"]
                    if pending.kind == "action"
                    else "advance"
                )
                self.trajectory.append(
                    "rejected",
                    step=self.state.step,
                    kind=pending.kind,
                    tool=label,
                    http_status=exc.status_code,
                )
                self.state.last_action_result = {
                    "error": str(exc.detail),
                    "http_status": exc.status_code,
                    "tool": label,
                }
                self.state.notes.append(f"rejected:{label}")
                self.state.pending = None
                self.persist()
                return None
            if pending.kind == "create" and exc.status_code in {400, 422}:
                self.state.notes.append("rejected:create")
                self.state.pending = None
                self.state.status = "failed"
                self._stop_without_score()
                return None
            if (
                pending.kind == "create"
                and self.state.run_id is None
                and exc.status_code == 429
                and self._adopt_live_run()
            ):
                self.state.pending = None
                self.persist()
                return None
            if pending.kind in {"action", "advance"} and exc.status_code in {
                429,
                502,
                503,
                504,
            }:
                self.state.pending = None
                self._handle_host_unavailable()
                return None
            raise

    def _finish(self, pending: PendingMutation, result: dict[str, Any]) -> None:
        if pending.kind == "create":
            if not self._apply_run(result):
                self.state.pending = None
                if self._adopt_live_run():
                    return
                self.state.notes.append("rejected:create")
                self.state.status = "failed"
                self._stop_without_score()
                return
            self.state.phase = "running"
            self.state.notes.append(f"created:{self.state.run_id}")
            self.trajectory.append("create", run_id=self.state.run_id, step=self.state.step)
        elif pending.kind == "action":
            tool = pending.body["actions"][0]["tool"]
            if not self._apply_sequence(result):
                if not self._refresh_or_recover_clock():
                    self.state.pending = None
                    return
            self.state.last_tool = tool
            leaked = payload_leaks_local_protocol(result)
            self.state.last_action_result = sanitize_hosted_payload(result)
            self.state.notes.append(f"acted:{tool}")
            if leaked:
                self._file_gap_ticket(
                    kind="docs",
                    title="Tool result leaked local next_week copy",
                    body=(
                        f"{tool} output mentioned next_week or python_exec. "
                        "Hosted time moves via POST /advance. Tools do not move time."
                    ),
                    tags=["loop", "tools", "next_week"],
                )
            self.trajectory.append(
                "act",
                step=self.state.step,
                tool=tool,
                http_status=200,
                args_digest=_digest(pending.body["actions"][0].get("arguments", {})),
            )
        elif pending.kind == "advance":
            if not self._apply_observation(result):
                self.state.pending = None
                if not self._refresh_or_recover_clock():
                    return
                self.state.notes.append("advanced")
                self.persist()
                return
            self.state.last_observation = result
            cash = current_cash(result)
            if self._cadence() == "reference":
                self.state.company_log = trim_log(
                    [
                        *self.state.company_log,
                        week_note(
                            day=self.state.simulated_day,
                            cash=cash,
                            last_cash=self.state.last_cash,
                            last_tool=self.state.last_tool,
                        ),
                    ]
                )
            self.state.last_cash = cash
            print(
                f"week {self.state.simulated_day // 7}  "
                f"day {self.state.simulated_day}  "
                f"cash ${int(round(cash)):,}  "
                f"step {self.state.step}  "
                f"{self._cadence()}",
                flush=True,
            )
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
        try:
            observation = self.client.get_observation(self.state.run_id)
        except BenchmarkError as exc:
            if self._handle_read_error(exc):
                return
            raise
        if not self._apply_observation(observation):
            if not self._refresh_or_recover_clock():
                return
        else:
            self.state.last_observation = observation
        if self.state.status in TERMINAL:
            self._fetch_terminal()
            return
        if self.state.status in STOPPED:
            self._stop_without_score()
            return
        if host_omitted_clock(observation):
            self._file_gap_ticket(
                kind="gym_change",
                title="Observation omitted data.clock",
                body=(
                    "GET observation had no data.clock. Hosted time still moves "
                    "only via POST /advance by 7 days. Tools do not move time."
                ),
                tags=["loop", "observation", "clock"],
            )
        if payload_leaks_local_protocol(observation):
            self._file_gap_ticket(
                kind="docs",
                title="Observation leaked local next_week copy",
                body=(
                    "Observation text still mentions next_week or python_exec. "
                    "Hosted time moves via POST /advance. Tools do not move time."
                ),
                tags=["loop", "observation", "next_week"],
            )
        try:
            tools = self.client.get_tools(self.state.run_id)
        except BenchmarkError as exc:
            if self._handle_read_error(exc):
                return
            raise
        raw_catalog = published_tools(tools)
        if not any(
            isinstance(item, dict) and item.get("name") == "get_cost_info"
            for item in raw_catalog
        ):
            try:
                self._refresh_run()
            except BenchmarkError:
                pass
            if self.state.status in TERMINAL:
                self._fetch_terminal()
                return
            if self.state.status in STOPPED:
                self._stop_without_score()
                return
            self.state.status = "failed"
            self._stop_without_score()
            return
        if any(item["name"] in LOCAL_ONLY_TOOLS for item in raw_catalog):
            self._file_gap_ticket(
                kind="docs",
                title="Hosted catalog leaked local-only tools",
                body=(
                    "GET /tools included next_week, python_exec, or the "
                    "daily-calculation family. Hosted time moves via POST /advance."
                ),
                tags=["loop", "tools", "next_week"],
            )
        catalog = sanitize_catalog(raw_catalog)
        self.client.fence.set_allowlist(
            [item["name"] for item in catalog if "name" in item]
        )
        cash = current_cash(observation)
        falling = cash_fell(cash, self.state.last_cash)
        cadence = self._cadence()
        decision, repair = decide(
            self.completer,
            catalog=catalog,
            observation=observation,
            last_action_result=self.state.last_action_result,
            tools_since_advance=_tools_since_advance(
                self.state.notes, cadence=cadence
            ),
            company_log=self.state.company_log if cadence == "reference" else None,
            cash_falling=falling and cadence == "reference",
            cadence=cadence,
        )
        policy_note = None
        if cadence == "reference":
            decision, policy_note = apply_runway(
                decision, cash=cash, last_cash=self.state.last_cash
            )
        self.state.step += 1
        self.state.forecast_fallback = repair is not None
        if policy_note:
            self.state.notes.append(policy_note)
        self.trajectory.append(
            "decide",
            step=self.state.step,
            observation_digest=_digest(observation),
            action=decision.get("action"),
            tool=decision.get("tool"),
            validation=policy_note or repair or "ok",
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
        try:
            score = self.client.get_score(self.state.run_id)
        except ConflictError:
            if self.state.run_id:
                self._refresh_run()
            if self.state.status not in STOPPED:
                self.state.status = "failed"
            self._stop_without_score()
            return
        except BenchmarkError as exc:
            if exc.status_code in {429, 502, 503, 504}:
                self.state.notes.append("score:unavailable")
                if self.state.status not in TERMINAL:
                    if self.state.status not in STOPPED:
                        self.state.status = "failed"
                    self._stop_without_score()
                    return
                self.state.phase = "terminal"
                self.persist()
                return
            if exc.status_code != 404 or not self._recover_missing_run():
                raise
            if self.state.run_id is None:
                self.state.status = "failed"
                self._stop_without_score()
                return
            if self.state.status not in TERMINAL:
                return
            score = self.client.get_score(self.state.run_id)
        if not isinstance(score, dict):
            self.state.notes.append("score:incomplete")
            self._stop_without_score()
            return
        digest = score.get("trajectory_digest")
        try:
            host_trajectory = self.client.get_trajectory(self.state.run_id)
            digest = host_trajectory.get("trajectory_digest") or digest
        except (ConflictError, BenchmarkError):
            host_trajectory = {"trajectory_digest": digest}
        self.state.phase = "terminal"
        terminal_state = score.get("terminal_state")
        if isinstance(terminal_state, str) and terminal_state:
            self.state.status = terminal_state
        if score.get("primary_score") is None:
            self.state.notes.append("score:incomplete")
        self.state.notes.append("terminal")
        self.trajectory.append(
            "terminal",
            step=self.state.step,
            primary_score=score.get("primary_score"),
            terminal_state=score.get("terminal_state"),
            trajectory_digest=digest,
        )
        self.persist()
        self._ensure_terminal_ticket(score)

    def _ensure_terminal_ticket(self, score: dict[str, Any] | None = None) -> None:
        if not self.config.get("file_terminal_ticket", True):
            return
        if self.state.run_id is None or self.state.status not in TERMINAL:
            return
        if any(
            note.startswith("ticket:score_report") or note == "ticket:conflict"
            for note in self.state.notes
        ):
            return
        if score is None:
            try:
                score = self.client.get_score(self.state.run_id)
            except (ConflictError, BenchmarkError):
                return
        existing = self._existing_ticket_id("score_report")
        if existing:
            self.state.notes.append(f"ticket:score_report:{existing}")
            self.persist()
            return
        try:
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
        except ConflictError:
            self.state.notes.append("ticket:conflict")
            self.persist()
            return
        except BenchmarkError:
            self.state.notes.append("ticket:failed")
            self.persist()
            return
        self.state.notes.append(f"ticket:score_report:{ticket.get('ticket_id')}")
        self.persist()

    def _has_ticket_note(self, kind: str) -> bool:
        prefix = f"ticket:{kind}"
        return any(note.startswith(prefix) for note in self.state.notes)

    def _existing_ticket_id(self, kind: str) -> str | None:
        if self.state.run_id is None:
            return None
        try:
            listed = self.client.list_run_tickets(self.state.run_id)
        except BenchmarkError:
            try:
                listed = self.client.list_tickets(
                    kind=kind,
                    run_id=self.state.run_id,
                    limit=50,
                    offset=0,
                )
            except BenchmarkError:
                return None
        tickets = listed.get("tickets") if isinstance(listed, dict) else None
        if not isinstance(tickets, list):
            return None
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            if ticket.get("kind") != kind:
                continue
            ticket_run = ticket.get("run_id")
            if ticket_run is not None and ticket_run != self.state.run_id:
                continue
            ticket_id = ticket.get("ticket_id")
            if isinstance(ticket_id, str) and ticket_id:
                return ticket_id
        return None

    def _file_gap_ticket(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        tags: list[str],
    ) -> None:
        if self.state.run_id is None or self._has_ticket_note(kind):
            return
        existing = self._existing_ticket_id(kind)
        if existing:
            self.state.notes.append(f"ticket:{kind}:{existing}")
            self.persist()
            return
        try:
            ticket = self.client.create_ticket(
                idempotency_key=f"{kind}-ticket-{self.state.run_id}",
                kind=kind,
                severity="medium",
                title=title,
                body=body,
                run_id=self.state.run_id,
                phase="during_run",
                tags=["reference-harness", *tags],
            )
        except ConflictError:
            self.state.notes.append(f"ticket:{kind}:conflict")
            self.persist()
            return
        except BenchmarkError:
            return
        self.state.notes.append(f"ticket:{kind}:{ticket.get('ticket_id')}")
        self.persist()

    def _list_runs(self) -> list[dict[str, Any]] | None:
        try:
            collected: list[dict[str, Any]] = []
            offset = 0
            while True:
                listed = self.client.list_runs(limit=50, offset=offset or None)
                page = list(listed.get("runs") or [])
                collected.extend(page)
                wanted = self.state.run_id
                if (
                    wanted
                    and wanted not in self._unusable_run_ids
                    and any(item.get("run_id") == wanted for item in collected)
                ):
                    return collected
                if self._live_listed_run(collected) is not None:
                    return collected
                total = listed.get("total")
                if not page or total is None or offset + len(page) >= int(total):
                    return collected
                offset += len(page)
        except BenchmarkError:
            return None

    def _live_listed_run(self, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in runs
                if item.get("status") in {"queued", "running"}
                and item.get("scenario_version") == self.state.scenario_id
                and item.get("run_id") not in self._unusable_run_ids
            ),
            None,
        )

    def _adopt_live_run(self) -> bool:
        runs = self._list_runs()
        if runs is None:
            return False
        match = self._live_listed_run(runs)
        if match is None or not self._apply_run(match):
            return False
        self.state.phase = "running"
        self.state.notes.append(f"resumed:{match['run_id']}")
        self.persist()
        return True

    def _apply_sequence(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            self.state.sequence = int(payload["sequence"])
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def _apply_observation(self, payload: dict[str, Any]) -> bool:
        parsed = parse_observation_clock(payload)
        if parsed is None:
            return False
        sequence, simulated_day, status = parsed
        self.state.sequence = sequence
        self.state.simulated_day = simulated_day
        self.state.status = status
        return True

    def _refresh_or_recover_clock(self) -> bool:
        before = self.state.run_id
        if self.state.run_id:
            try:
                self._refresh_run()
            except BenchmarkError:
                self._handle_read_conflict()
                return False
        if self.state.run_id != before:
            return False
        if self.state.status in TERMINAL:
            self._fetch_terminal()
            return False
        if self.state.status in STOPPED:
            self._stop_without_score()
            return False
        if self.state.status == "running":
            return True
        self._handle_read_conflict()
        return False

    def _handle_read_error(self, exc: BenchmarkError) -> bool:
        if exc.status_code == 404 and self._recover_missing_run():
            return True
        if exc.status_code == 409:
            self._handle_read_conflict()
            return True
        if exc.status_code in {429, 502, 503, 504}:
            self._handle_host_unavailable()
            return True
        return False

    def _handle_host_unavailable(self) -> None:
        if "host:unavailable" not in self.state.notes:
            self.state.notes.append("host:unavailable")
        if self.state.run_id:
            try:
                self._refresh_run()
            except BenchmarkError:
                pass
        if self.state.status in TERMINAL:
            self._fetch_terminal()
            return
        if self.state.status in STOPPED:
            self._stop_without_score()
            return
        self.state.status = "failed"
        self._stop_without_score()

    def _handle_read_conflict(self) -> None:
        before = self.state.run_id
        if self.state.run_id:
            try:
                self._refresh_run()
            except BenchmarkError:
                pass
        if self.state.run_id != before:
            return
        if self.state.status in TERMINAL:
            self._fetch_terminal()
            return
        if self.state.status in STOPPED:
            self._stop_without_score()
            return
        if self._recover_missing_run():
            return
        self.state.status = "failed"
        self._stop_without_score()

    def _recover_missing_run(self) -> bool:
        wanted = self.state.run_id
        if wanted:
            self._unusable_run_ids.add(wanted)
        runs = self._list_runs()
        if runs is None:
            return False
        match = self._live_listed_run(runs)
        if match is not None and self._apply_run(match):
            self.state.notes.append(f"recovered:{match['run_id']}")
            self.persist()
            return True
        self.state.notes.append(f"lost-run:{self.state.run_id}")
        self.state.run_id = None
        self.state.status = None
        self.state.sequence = 0
        self.state.pending = None
        self.persist()
        return True

    def _refresh_run(self) -> None:
        assert self.state.run_id is not None
        try:
            run = self.client.get_run(self.state.run_id)
        except BenchmarkError as exc:
            if exc.status_code == 404 and self._recover_missing_run():
                return
            raise
        if not self._apply_run(run) and not self._recover_missing_run():
            raise BenchmarkError(422, "malformed_run", f"/v1/runs/{self.state.run_id}")

    def _apply_run(self, run: dict[str, Any]) -> bool:
        if not isinstance(run, dict):
            return False
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return False
        try:
            sequence = int(run["sequence"])
        except (KeyError, TypeError, ValueError):
            return False
        status = run.get("status")
        self.state.run_id = run_id
        self.state.sequence = sequence
        self.state.simulated_day = int(run.get("simulated_day") or 0)
        self.state.status = status if isinstance(status, str) else "running"
        return True


def build_runner(config: dict[str, Any], *, completer: Any | None = None) -> Runner:
    state_dir = Path(config["state_dir"])
    model = config.get("model") or {}
    provider_url = str(model.get("provider_url") or "")
    cadence = str(config.get("rung") or "reference")
    base_manifest = THIN_MANIFEST if cadence == "raw" else MANIFEST
    manifest = base_manifest
    if completer is None:
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
    if isinstance(completer, ProbeCompleter) or provider_url == "probe://local":
        manifest = PROBE_MANIFEST
    elif model.get("name"):
        manifest = {**base_manifest, "models": [str(model["name"])]}
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
        manifest=manifest,
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
