# Reference harness spec v0.1

Baseline runner for Business Bench. It implements the conformance profile and nothing listed in §4.

Repo: `ClawBank-co/reference-company-harness`. Published rows name the git tag.

## 1. Constraints

- Python 3.12, stdlib plus `eth-account` (SIWE).
- One process, one loop, one `state.json`. No async, no framework, no database.
- Config is a file. No environment auto-detection.
- HTTP and the model provider both use `urllib` + `json`.

## 2. Layout

```
AUTH → OPEN_SESSION → (OBSERVE → DECIDE → ACT | ADVANCE)* → TERMINAL
```

```
harness/main.py       loop
harness/auth.py       SIWE
harness/client.py     retries, idempotency, fence gate
harness/memory.py     atomic state.json
harness/decide.py     prompt, model call, schema check
harness/fence.py      catalog allowlist; block send/trade/offramp
harness/trajectory.py JSONL events, no keys or model prose
```

`memory.py` is the only writer of `state.json`.

## 3. Conformance

1. Loop until terminal. Stop on max steps or wall clock.
2. Read `GET …/tools` each observe. Validate model JSON against that catalog. One re-prompt; then advance with a flat cash forecast.
3. Persist run id, token, sequence, pending mutation, last observation. Write tmp, then `os.replace()`.
4. Advance requires cash point + 95% CI at 7/28/84/182. Shape only; no calculator.
5. Sign the host SIWE message. Key stays in a file named by config.
6. Fence in `client.py` before the request.
7. 401 → renew once and replay. 409 → confirm and refresh, except create-without-`run_id` keeps the pending key for resume. 5xx/timeout → backoff. Persist the idempotency key before send.
8. Trajectory is structured events. `python -m harness export` prints it.

## 4. Out of scope

Memory retrieval, planning, subagents, reflection, forecast sandbox, strategy retry, per-model prompts.

## 5. Process

MIT. Strengthen-the-baseline PRs until study freeze; after that the studied tag is fixed. Spec version and tag move together.

v0.1 done: 7-day hosted conformance on two providers, tests green, kill-9 resume in CI.
