"""Kestrel (KSL) — fast, light, decentralized money. Reference implementation.

`kestrel.ui` is deliberately NOT imported here: it needs tkinter, which a
headless node has no reason to have. The desktop apps import it directly.
"""

# Defined before anything is imported, so every module in the package —
# the node reports it to its peers — can read it while the package is
# still being put together.
__version__ = "1.4.9"

from . import params                                            # noqa: E402
from .blockchain import Blockchain, ValidationError             # noqa: E402
from .block import Block, build_genesis                         # noqa: E402
from .transaction import Transaction, TxInput, TxOutput         # noqa: E402
from .wallet import Wallet, format_ksl, parse_ksl               # noqa: E402
from .miner import mine, mine_block, find_pow, default_threads  # noqa: E402
from .node import Node                                          # noqa: E402

__all__ = [
    "params", "Blockchain", "ValidationError", "Block", "build_genesis",
    "Transaction", "TxInput", "TxOutput", "Wallet", "format_ksl",
    "parse_ksl", "mine", "mine_block", "find_pow", "default_threads", "Node",
]
