"""Trajectory must not contain keys, tokens, or model prose."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from harness.trajectory import Trajectory


class TrajectoryTests(unittest.TestCase):
    def test_redacts_secrets_and_drops_prose(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "trajectory.jsonl"
        log = Trajectory(path)
        log.append(
            "act",
            step=1,
            access_token="tok_secret",
            authorization="Bearer tok_secret",
            private_key="0xabc",
            signature="0xsig",
            reasoning="I think we should dump chain of thought",
            tool="set_prices",
        )
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("tok_secret", text)
        self.assertNotIn("0xabc", text)
        self.assertNotIn("0xsig", text)
        self.assertNotIn("chain of thought", text)
        self.assertIn("[redacted]", text)
        self.assertIn("set_prices", text)
        self.assertIn('"event": "act"', text)
