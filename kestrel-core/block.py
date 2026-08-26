"""
Kestrel blocks.

Like Litecoin, a block has two hashes:
- block id  = sha256d(header)  — used to link blocks and identify them
- pow hash  = scrypt(header)   — must be below the target for valid work
"""

import json

from . import params
from .crypto_utils import sha256d, scrypt_hash
from .transaction import Transaction


def merkle_root(txids: list[str]) -> str:
    """Bitcoin-style merkle tree over txids (duplicate last node if odd)."""
    if not txids:
        return "0" * 64
    layer = [bytes.fromhex(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [sha256d(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0].hex()


class Block:
    def __init__(self, height: int, prev_hash: str, timestamp: int,
                 target: int, nonce: int, transactions: list[Transaction],
                 version: int = 1):
        self.version = version
        self.height = height
        self.prev_hash = prev_hash
        self.timestamp = int(timestamp)
        self.target = int(target)
        self.nonce = int(nonce)
        self.transactions = transactions

    # -------------------------------------------------------------- header

    def header_dict(self) -> dict:
        return {
            "version": self.version,
            "height": self.height,
            "prev_hash": self.prev_hash,
            "merkle_root": self.merkle_root,
            "timestamp": self.timestamp,
            "target": f"{self.target:064x}",
            "nonce": self.nonce,
        }

    def header_bytes(self) -> bytes:
        return json.dumps(self.header_dict(), sort_keys=True,
                          separators=(",", ":")).encode()

    @property
    def merkle_root(self) -> str:
        return merkle_root([tx.txid for tx in self.transactions])

    @property
    def block_id(self) -> str:
        return sha256d(self.header_bytes()).hex()

    @property
    def pow_hash(self) -> str:
        return scrypt_hash(self.header_bytes()).hex()

    def has_valid_pow(self) -> bool:
        return int(self.pow_hash, 16) <= self.target

    @property
    def work(self) -> int:
        """Expected hashes to find this block; summed for chain-work comparison."""
        return (1 << 256) // (self.target + 1)

    def size(self) -> int:
        return len(self.serialize())

    # ------------------------------------------------------- serialization

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "height": self.height,
            "prev_hash": self.prev_hash,
            "timestamp": self.timestamp,
            "target": f"{self.target:064x}",
            "nonce": self.nonce,
            "transactions": [tx.to_dict() for tx in self.transactions],
        }

    def serialize(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True,
                          separators=(",", ":")).encode()

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        return cls(
            height=d["height"],
            prev_hash=d["prev_hash"],
            timestamp=d["timestamp"],
            target=int(d["target"], 16),
            nonce=d["nonce"],
            transactions=[Transaction.from_dict(t) for t in d["transactions"]],
            version=d.get("version", 1),
        )


def build_genesis() -> Block:
    """Deterministic genesis block from consensus parameters.

    Its coinbase output is unspendable by convention (never enters the UTXO
    set), so the money supply starts at zero — a fair launch with no premine.
    """
    coinbase = Transaction.coinbase(
        height=0,
        value=params.INITIAL_REWARD,
        address="K" * 34,  # unspendable placeholder address
        message=params.GENESIS_MESSAGE,
        timestamp=params.GENESIS_TIMESTAMP,
    )
    return Block(
        height=0,
        prev_hash="0" * 64,
        timestamp=params.GENESIS_TIMESTAMP,
        target=params.GENESIS_TARGET,
        nonce=params.GENESIS_NONCE,
        transactions=[coinbase],
    )
