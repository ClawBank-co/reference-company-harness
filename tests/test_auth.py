"""SIWE signer loads a key file and never exposes it."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from eth_account.messages import encode_defunct
from eth_account import Account

from harness.auth import LocalSigner


class AuthTests(unittest.TestCase):
    def test_sign_siwe_from_key_file(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        key_path = Path(tmp.name) / "key.hex"
        account = Account.from_key("0x" + "ab" * 32)
        key_path.write_text(account.key.hex(), encoding="utf-8")
        signer = LocalSigner(key_path)
        self.assertEqual(signer.address, account.address)
        message = "example.com wants you to sign in with your Ethereum account"
        signature = signer.sign_siwe_message(message)
        self.assertTrue(signature.startswith("0x"))
        self.assertEqual(len(signature), 132)
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
        self.assertEqual(recovered, account.address)
        self.assertNotIn(account.key.hex(), signer.address)
