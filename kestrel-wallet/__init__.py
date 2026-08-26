"""Kestrel (KSL) — fast, light, decentralized money. Reference implementation."""

from . import params
from .blockchain import Blockchain, ValidationError
from .block import Block, build_genesis
from .transaction import Transaction, TxInput, TxOutput
from .wallet import Wallet, format_ksl, parse_ksl
from .miner import mine, mine_block, find_pow, default_threads
from .node import Node

__version__ = "1.4.5"
__all__ = [
    "params", "Blockchain", "ValidationError", "Block", "build_genesis",
    "Transaction", "TxInput", "TxOutput", "Wallet", "format_ksl",
    "parse_ksl", "mine", "mine_block", "find_pow", "default_threads", "Node",
]
