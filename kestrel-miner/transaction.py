"""
Kestrel transactions — UTXO model.

A transaction spends previous outputs (inputs) and creates new outputs.
Serialization is canonical JSON (sorted keys, no whitespace) so that every
node computes identical txids for identical transactions.

Signature scheme (reference implementation, SIGHASH_ALL semantics):
every input signs sha256d(serialization-without-signatures-or-pubkeys),
committing to every outpoint spent and every output created. Public keys
are excluded from the digest — like Bitcoin's scriptSig they are carried
alongside the signature, and they are constrained separately by the rule
that each input's pubkey must hash to the address of the UTXO it spends.
This makes signing order-independent: inputs can be signed in any order.
"""

import json
import time

from . import params
from .crypto_utils import sha256d, sign, verify, pubkey_to_address, is_valid_address

COINBASE_TXID = "0" * 64
COINBASE_VOUT = -1


class TxInput:
    def __init__(self, txid: str, vout: int, pubkey: str = "", signature: str = ""):
        self.txid = str(txid)       # hex txid of the output being spent
        self.vout = int(vout)       # index of that output
        self.pubkey = pubkey        # hex compressed pubkey (or coinbase data)
        self.signature = signature  # hex DER signature

    @property
    def outpoint(self) -> tuple:
        return (self.txid, self.vout)

    def to_dict(self, include_signatures: bool = True) -> dict:
        d = {"txid": self.txid, "vout": self.vout}
        if include_signatures:
            d["pubkey"] = self.pubkey
            d["signature"] = self.signature
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TxInput":
        return cls(d["txid"], d["vout"], d.get("pubkey", ""), d.get("signature", ""))


class TxOutput:
    def __init__(self, amount: int, address: str):
        self.amount = int(amount)   # feathers
        self.address = address

    def to_dict(self) -> dict:
        return {"amount": self.amount, "address": self.address}

    @classmethod
    def from_dict(cls, d: dict) -> "TxOutput":
        return cls(d["amount"], d["address"])


class Transaction:
    def __init__(self, inputs, outputs, timestamp: int = None, version: int = 1):
        self.version = version
        self.inputs: list[TxInput] = inputs
        self.outputs: list[TxOutput] = outputs
        self.timestamp = int(timestamp if timestamp is not None else time.time())

    # ------------------------------------------------------- serialization

    def to_dict(self, include_signatures: bool = True) -> dict:
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "inputs": [i.to_dict(include_signatures) for i in self.inputs],
            "outputs": [o.to_dict() for o in self.outputs],
        }

    def serialize(self, include_signatures: bool = True) -> bytes:
        return json.dumps(
            self.to_dict(include_signatures), sort_keys=True, separators=(",", ":")
        ).encode()

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        return cls(
            inputs=[TxInput.from_dict(i) for i in d["inputs"]],
            outputs=[TxOutput.from_dict(o) for o in d["outputs"]],
            timestamp=d["timestamp"],
            version=d.get("version", 1),
        )

    # --------------------------------------------------------------- ids

    @property
    def txid(self) -> str:
        return sha256d(self.serialize(include_signatures=True)).hex()

    def sighash(self) -> bytes:
        """Digest each input signs: commits to everything except signatures
        and pubkeys, so signing is independent of field-setting order."""
        return sha256d(self.serialize(include_signatures=False))

    # ---------------------------------------------------------- coinbase

    @property
    def is_coinbase(self) -> bool:
        return (
            len(self.inputs) == 1
            and self.inputs[0].txid == COINBASE_TXID
            and self.inputs[0].vout == COINBASE_VOUT
        )

    @classmethod
    def coinbase(cls, height: int, value: int, address: str,
                 message: str = "", timestamp: int = None) -> "Transaction":
        """Block-reward transaction. Height in the data keeps txids unique."""
        data = json.dumps({"height": height, "msg": message}, sort_keys=True)
        cb_input = TxInput(COINBASE_TXID, COINBASE_VOUT, pubkey=data.encode().hex())
        return cls([cb_input], [TxOutput(value, address)], timestamp=timestamp)

    # -------------------------------------------------------------- signing

    def sign_input(self, index: int, private_key: bytes, pubkey: bytes) -> None:
        self.inputs[index].pubkey = pubkey.hex()
        self.inputs[index].signature = sign(private_key, self.sighash()).hex()

    def verify_input_signature(self, index: int, expected_address: str) -> bool:
        """Check the input's pubkey hashes to the UTXO's address and its
        signature is valid over this transaction's sighash."""
        txin = self.inputs[index]
        try:
            pubkey = bytes.fromhex(txin.pubkey)
            signature = bytes.fromhex(txin.signature)
        except ValueError:
            return False
        if pubkey_to_address(pubkey) != expected_address:
            return False
        return verify(pubkey, signature, self.sighash())

    # ------------------------------------------------------------- helpers

    @property
    def total_output(self) -> int:
        return sum(o.amount for o in self.outputs)

    def size(self) -> int:
        return len(self.serialize())

    def basic_check(self) -> tuple[bool, str]:
        """Context-free sanity checks."""
        if not self.inputs or not self.outputs:
            return False, "empty inputs or outputs"
        if self.size() > params.MAX_BLOCK_SIZE:
            return False, "transaction too large"
        seen = set()
        for i in self.inputs:
            if i.outpoint in seen:
                return False, "duplicate input outpoint"
            seen.add(i.outpoint)
        for o in self.outputs:
            if o.amount <= 0:
                return False, "non-positive output amount"
            if o.amount > params.MAX_SUPPLY:
                return False, "output exceeds max supply"
            if not is_valid_address(o.address):
                return False, f"invalid output address {o.address!r}"
        if self.total_output > params.MAX_SUPPLY:
            return False, "outputs exceed max supply"
        return True, "ok"
