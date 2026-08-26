"""
Kestrel miner — assembles candidate blocks and grinds scrypt nonces.

`find_pow` is the engine: a multi-threaded nonce search (hashlib.scrypt
releases the GIL, so extra threads use real CPU cores). `mine_block` wraps
it for one-shot use; GUI apps call `find_pow` directly with a stop event.
"""

import json
import os
import threading
import time

from . import params
from .block import Block
from .blockchain import Blockchain, ValidationError
from .crypto_utils import scrypt_hash
from .transaction import Transaction


def default_threads() -> int:
    """A polite default: all cores but one, at least 1."""
    return max(1, (os.cpu_count() or 2) - 1)


def assemble_candidate(chain: Blockchain, miner_address: str,
                       message: str = "") -> Block:
    """Build a candidate block: fee-sorted mempool transactions plus a
    coinbase paying subsidy + fees to the miner."""
    height = chain.height + 1

    # order mempool by fee rate, greedily fill the block
    entries = []
    for tx in chain.mempool.values():
        try:
            fee = chain.validate_transaction(tx, height=height)
        except ValidationError:
            continue
        entries.append((fee / max(tx.size(), 1), fee, tx))
    entries.sort(key=lambda e: -e[0])

    selected, fees, size_budget = [], 0, params.MAX_BLOCK_SIZE - 2_000
    spent = set()
    for _, fee, tx in entries:
        if any(i.outpoint in spent for i in tx.inputs):
            continue
        if tx.size() > size_budget:
            continue
        selected.append(tx)
        spent.update(i.outpoint for i in tx.inputs)
        fees += fee
        size_budget -= tx.size()

    coinbase = Transaction.coinbase(
        height=height,
        value=chain.block_subsidy(height) + fees,
        address=miner_address,
        message=message or f"kestrel-miner/{params.PROTOCOL_VERSION}",
    )
    timestamp = max(int(time.time()), chain.median_time_past() + 1)
    return Block(
        height=height,
        prev_hash=chain.tip.block_id,
        timestamp=timestamp,
        target=chain.next_target(),
        nonce=0,
        transactions=[coinbase] + selected,
    )


def find_pow(block: Block, *, threads: int = 1, stop: threading.Event = None,
             max_seconds: float = None, on_progress=None) -> bool:
    """Multi-threaded proof-of-work search on `block`.

    Thread i grinds nonces i, i+T, i+2T, … The block header (including the
    merkle root) is serialized once and only the nonce field changes, so
    every core spends its time in scrypt, not in JSON.

    Returns True with block.nonce set to a solution; False if `stop` was
    set or `max_seconds` ran out (caller then reassembles a fresh candidate
    — that also picks up new mempool transactions and a fresh timestamp).
    `on_progress(hashes_per_second)` is called about twice a second.
    """
    threads = max(1, int(threads))
    stop = stop or threading.Event()
    target = block.target
    header = block.header_dict()          # merkle root computed once
    found = threading.Event()
    winner = [None]
    counts = [0] * threads
    start_nonce = block.nonce

    def worker(wid: int):
        hd = dict(header)
        nonce = start_nonce + wid
        n = 0
        while not (stop.is_set() or found.is_set()):
            hd["nonce"] = nonce
            data = json.dumps(hd, sort_keys=True,
                              separators=(",", ":")).encode()
            if int.from_bytes(scrypt_hash(data), "big") <= target:
                winner[0] = nonce
                found.set()
                return
            nonce += threads
            n += 1
            if n % 64 == 0:
                counts[wid] += 64

    pool = [threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(threads)]
    t0 = time.time()
    for t in pool:
        t.start()

    last_total, last_t = 0, t0
    while not found.is_set():
        if stop.is_set():
            break
        if max_seconds is not None and time.time() - t0 >= max_seconds:
            break
        found.wait(0.5)
        if on_progress:
            now = time.time()
            total = sum(counts)
            rate = (total - last_total) / max(now - last_t, 1e-9)
            last_total, last_t = total, now
            on_progress(rate)

    ok = found.is_set() and winner[0] is not None
    found.set()  # release any workers still spinning
    for t in pool:
        t.join(timeout=2)
    if ok:
        block.nonce = winner[0]
        return True
    return False


def mine_block(chain: Blockchain, miner_address: str, *,
               quiet: bool = False, message: str = "",
               threads: int = 1) -> Block:
    """Proof-of-work search. Returns the block after adding it to the chain."""
    started = time.time()
    rate = [0.0]

    def note(r):
        rate[0] = r

    while True:
        block = assemble_candidate(chain, miner_address, message)
        # 30-second rounds: each round refreshes timestamp + mempool
        if find_pow(block, threads=threads, max_seconds=30, on_progress=note):
            break

    chain.add_block(block)
    if not quiet:
        dt = max(time.time() - started, 1e-9)
        print(f"  mined block {block.height}  "
              f"txs={len(block.transactions)}  nonce={block.nonce}  "
              f"{rate[0]:,.0f} H/s  {dt:.1f}s  pow={block.pow_hash[:16]}…")
    return block


def mine(chain: Blockchain, miner_address: str, count: int = 1,
         quiet: bool = False, threads: int = 1) -> list[Block]:
    return [mine_block(chain, miner_address, quiet=quiet, threads=threads)
            for _ in range(count)]
