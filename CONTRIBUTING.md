# Contributing

Two ways to make this better, with different rules.

## Fork it (most people)

If you want a *better* harness, fork the repo and change the policy layer — see "Make it better" in the [README](README.md). Your fork competes on the [public board](https://bench.clawbank.co/results) under its own tag. It does not need to come back here.

## PR the baseline (this repo)

PRs that strengthen the baseline are welcome **until study freeze** (`SPEC.md` §5). After freeze, the studied tag is fixed and improvements land on the next tag.

Ground rules:

- Keep the surface small: Python 3.12, stdlib + `eth-account`, one process, one loop, one `state.json`. No async, no framework, no database.
- Keep the `raw` rung thin. It is everyone's control group.
- Keep the money fence. No PR may enable send / trade / offramp tools.
- Don't hard-code tool names; the catalog comes from the host.
- Out-of-scope features (memory retrieval, planning, subagents, forecast calculators, per-model prompts) belong in forks, not here.

Before opening a PR:

```bash
python -m unittest discover -s tests -v   # must stay green (CI runs the same)
```

Add tests for behavior you change. If your change alters what a published run would do, say so in the PR description — reproducibility is the product.
