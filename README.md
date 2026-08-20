# Reference company harness

Bounded runner for [ClawBank Business Bench](https://bench.clawbank.co). One process, one loop, one `state.json`. It meets the conformance profile and stops there.

Cite results by commit hash. Spec: [`SPEC.md`](SPEC.md). Host contract: `GET /v1/conformance`.

This is not `saas_bench.reference_runner`. That module is a protocol stub with canned actions. This repo calls a model.

Out of scope: memory retrieval, planning, subagents, a forecast calculator, per-model prompts. Put those in a different harness.

## Setup

Python 3.12. Extra dependency: `eth-account`.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.json config.json
python -c 'from eth_account import Account; print(Account.create().key.hex())' > key.hex
printf '%s\n' "$OPENAI_API_KEY" > model.key
```

`host` is `http://127.0.0.1:8000` or `https://bench.clawbank.co`. `scenario` is `conformance`, `growth`, or `full`.

## Run

```bash
python -m harness run --config config.json
python -m harness export --state-dir .harness-run
python -m unittest discover -s tests -v
```

Existing `state.json` means resume. A kill loses only the in-flight HTTP call. Terminal runs POST `kind=score_report` when `file_terminal_ticket` is true.

## License

MIT.
