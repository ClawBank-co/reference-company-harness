"""Atomic writes and leftover tmp files must not corrupt state."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from harness.memory import PendingMutation, RunnerState, StateStore


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.store = StateStore(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_load_round_trip(self) -> None:
        self.store.save(RunnerState(run_id="run_1", sequence=3, access_token="secret-token"))
        loaded = self.store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "run_1")
        self.assertEqual(loaded.sequence, 3)
        self.assertNotIn("access_token", loaded.to_public_dict())

    def test_crash_leaves_tmp_without_corrupting_state(self) -> None:
        self.store.save(RunnerState(run_id="good", sequence=4))
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("{not-json", encoding="utf-8")
        loaded = self.store.load()
        assert loaded is not None
        self.assertEqual(loaded.run_id, "good")
        self.assertEqual(loaded.sequence, 4)
        self.assertTrue(tmp.exists())

    def test_pending_survives_save(self) -> None:
        self.store.save(
            RunnerState(
                pending=PendingMutation(
                    kind="advance",
                    path="/v1/runs/r/advance",
                    idempotency_key="advance-abc",
                    body={"sequence": 1},
                )
            )
        )
        loaded = self.store.load()
        assert loaded is not None
        assert loaded.pending is not None
        self.assertEqual(loaded.pending.idempotency_key, "advance-abc")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["pending"]["kind"], "advance")
