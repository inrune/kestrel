"""
Kestrel wallet — a single secp256k1 keypair plus transaction building.

The wallet file stores the private key in plain hex. Treat it like cash:
anyone who reads the file can spend the coins.
"""

import json
import os
import time

from . import params
from .crypto_utils import (
    generate_private_key, private_to_public, pubkey_to_address,
    private_to_wif, wif_to_private,
)
from .transaction import Transaction, TxInput, TxOutput
from .blockchain import ValidationError


class Wallet:
    def __init__(self, private_key: bytes):
        self.private_key = private_key
        self.public_key = private_to_public(private_key)
        self.address = pubkey_to_address(self.public_key)

    # ---------------------------------------------------------- lifecycle

    @classmethod
    def create(cls) -> "Wallet":
        return cls(generate_private_key())

    @classmethod
    def from_wif(cls, wif: str) -> "Wallet":
        return cls(wif_to_private(wif))

    @classmethod
    def load(cls, path: str) -> "Wallet":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        key = bytes.fromhex(data["private_key"])
        if len(key) != 32:
            raise ValueError(f"{path}: private_key must be 32 bytes "
                             f"(64 hex chars), found {len(key)}")
        return cls(key)

    def save(self, path: str):
        """Write the wallet to disk without ever risking the existing one.

        This file *is* the money: if it is lost or truncated the coins are
        gone permanently, with no recovery. So the write is atomic — the
        data goes to a temporary file, is flushed all the way to the disk,
        and only then replaces the old file. A crash or power cut can
        therefore leave either the old wallet or the new one, never a
        half-written file.

        As a second safeguard, anything already at this path that is not
        this very wallet is moved aside first rather than overwritten — a
        wallet for a different address, and also a file that can't be read
        at all. A damaged wallet file is not "nothing worth keeping": the
        key is often still in it, recoverable by hand, and the old rule
        replaced such a file outright the moment an app started and made a
        new wallet in its place. Returns the path it was moved to, if any.
        """
        data = {
            "private_key": self.private_key.hex(),
            "public_key": self.public_key.hex(),
            "address": self.address,
            "wif": private_to_wif(self.private_key),
            "warning": "Anyone with this file can spend your KSL. Keep it secret.",
        }

        backup = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    existing = json.load(f)
                same = (isinstance(existing, dict) and
                        existing.get("private_key") == self.private_key.hex())
            except (OSError, ValueError, RecursionError):
                same = False
            if not same:
                stamp = int(time.time())
                backup = f"{path}.replaced-{stamp}.bak"
                n = 1
                while os.path.exists(backup):         # never clobber a backup
                    backup = f"{path}.replaced-{stamp}-{n}.bak"
                    n += 1
                os.replace(path, backup)
                try:
                    os.chmod(backup, 0o600)
                except OSError:
                    pass

        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:                                   # make the rename durable too
            dirfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass
        return backup

    # ----------------------------------------------------------- building

    def build_transaction(self, utxos: list[dict], to_address: str,
                          amount: int, fee: int = params.MIN_RELAY_FEE
                          ) -> Transaction:
        """Greedy coin selection over `utxos` (largest first), change back
        to this wallet. Amounts are in feathers."""
        if amount <= 0:
            raise ValidationError("amount must be positive")
        if fee < params.MIN_RELAY_FEE:
            raise ValidationError("fee below minimum relay fee")

        selected, total = [], 0
        for u in utxos:
            selected.append(u)
            total += u["amount"]
            if total >= amount + fee:
                break
        if total < amount + fee:
            raise ValidationError(
                f"insufficient spendable funds: have {total}, need {amount + fee}"
            )

        inputs = [TxInput(u["txid"], u["vout"]) for u in selected]
        outputs = [TxOutput(amount, to_address)]
        change = total - amount - fee
        if change > 0:
            outputs.append(TxOutput(change, self.address))

        tx = Transaction(inputs, outputs)
        for i in range(len(tx.inputs)):
            tx.sign_input(i, self.private_key, self.public_key)
        return tx


def format_ksl(feathers: int) -> str:
    sign = "-" if feathers < 0 else ""
    feathers = abs(feathers)
    return f"{sign}{feathers // params.COIN}.{feathers % params.COIN:08d} KSL"


def parse_ksl(s: str) -> int:
    """'12.5' or '12.5 KSL' -> feathers. Strict: digits and at most one
    decimal point, max 8 decimal places, no negatives, no silent rounding."""
    s = s.strip().upper().removesuffix("KSL").strip().replace(",", "")
    if "." in s:
        whole, frac = s.split(".", 1)
    else:
        whole, frac = s, ""
    if not (whole or frac):
        raise ValueError("no amount given")

    def digits(part):
        # str.isdigit() also says yes to "²" and "٣", which int() then
        # either rejects with a baffling message or quietly accepts
        return part.isascii() and part.isdigit()

    if (whole and not digits(whole)) or (frac and not digits(frac)):
        raise ValueError(f"bad amount {s!r} — use digits like 12.5")
    if len(frac) > 8:
        raise ValueError("amounts have at most 8 decimal places "
                         "(1 feather = 0.00000001 KSL)")
    frac = (frac + "00000000")[:8]
    return int(whole or "0") * params.COIN + int(frac)
