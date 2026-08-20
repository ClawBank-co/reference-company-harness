"""SIWE sign-in. Key is loaded from a file path in config and never logged."""

from __future__ import annotations

from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct


class LocalSigner:
    def __init__(self, key_path: Path) -> None:
        raw = Path(key_path).read_text(encoding="utf-8").strip()
        if not raw:
            raise ValueError("wallet key file is empty")
        if not raw.startswith("0x"):
            raw = "0x" + raw
        self._account = Account.from_key(raw)

    @property
    def address(self) -> str:
        return self._account.address

    def sign_siwe_message(self, message: str) -> str:
        signed = Account.sign_message(encode_defunct(text=message), self._account.key)
        signature = signed.signature.hex()
        return signature if signature.startswith("0x") else f"0x{signature}"
