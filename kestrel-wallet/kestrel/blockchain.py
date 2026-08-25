"""
Kestrel consensus engine.

Maintains the chain, the UTXO set and the mempool, and enforces every
consensus rule: proof-of-work, difficulty retargeting, the 44,000,000 KSL
emission schedule, transaction validity and coinbase maturity.
"""

import json
import os
import time

from . import params
from .block import Block, build_genesis
from .transaction import Transaction


class ValidationError(Exception):
    pass


# Local relay policy (not consensus): max pending transactions a node keeps.
MAX_MEMPOOL = 10_000


class UTXO:
    __slots__ = ("amount", "address", "height", "coinbase")

    def __init__(self, amount: int, address: str, height: int, coinbase: bool):
        self.amount = amount
        self.address = address
        self.height = height
        self.coinbase = coinbase


class Blockchain:
    def __init__(self, data_dir: str = None, autoload: bool = True):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "kestrel-data")
        self.blocks: list[Block] = []
        self.utxos: dict[tuple, UTXO] = {}       # (txid, vout) -> UTXO
        self.mempool: dict[str, Transaction] = {}  # txid -> tx
        self.mempool_spends: set[tuple] = set()    # outpoints claimed by mempool

        if autoload and self._load():
            return
        self._init_genesis()

    # ------------------------------------------------------------- genesis

    def _init_genesis(self):
        genesis = build_genesis()
        if not genesis.has_valid_pow():
            raise ValidationError(
                "genesis proof-of-work invalid — params.GENESIS_NONCE is wrong"
            )
        self.blocks = [genesis]
        self.utxos = {}   # genesis coinbase is unspendable: fair launch, no premine
        # NB: no save() here — scratch chains (validation, sync) share data_dir
        # and must never overwrite the persisted chain. Saving happens on
        # add_block / maybe_replace.

    # -------------------------------------------------------------- basics

    @property
    def height(self) -> int:
        return self.blocks[-1].height

    @property
    def tip(self) -> Block:
        return self.blocks[-1]

    def total_work(self) -> int:
        return sum(b.work for b in self.blocks)

    def circulating_supply(self) -> int:
        return sum(u.amount for u in self.utxos.values())

    def median_time_past(self) -> int:
        times = sorted(b.timestamp for b in self.blocks[-params.MEDIAN_TIME_SPAN:])
        return times[len(times) // 2]

    # ------------------------------------------------------ monetary policy

    @staticmethod
    def block_subsidy(height: int) -> int:
        """25 KSL, halving every 880,000 blocks. Sums to <44,000,000 KSL."""
        halvings = height // params.HALVING_INTERVAL
        if halvings >= 64:
            return 0
        return params.INITIAL_REWARD >> halvings

    # ------------------------------------------------------------ difficulty

    def next_target(self) -> int:
        """Target the next block must meet. Bitcoin-style retarget every
        RETARGET_INTERVAL blocks, clamped to a 4x adjustment either way."""
        next_height = self.height + 1
        if next_height % params.RETARGET_INTERVAL != 0:
            return self.tip.target

        first = self.blocks[next_height - params.RETARGET_INTERVAL]
        actual = self.tip.timestamp - first.timestamp
        expected = params.TARGET_BLOCK_TIME * (params.RETARGET_INTERVAL - 1)
        actual = max(expected // 4, min(actual, expected * 4))

        new_target = self.tip.target * actual // expected
        return max(1, min(new_target, params.MAX_TARGET))

    @staticmethod
    def difficulty_of(target: int) -> float:
        return params.MAX_TARGET / target

    # -------------------------------------------------------- tx validation

    def validate_transaction(self, tx: Transaction, *, spent: set = None,
                             utxo_overlay: dict = None,
                             height: int = None) -> int:
        """Validate a non-coinbase transaction against the UTXO set.

        `spent` / `utxo_overlay` let block validation account for earlier
        transactions in the same block. Returns the fee in feathers.
        """
        ok, reason = tx.basic_check()
        if not ok:
            raise ValidationError(reason)
        if tx.is_coinbase:
            raise ValidationError("unexpected coinbase transaction")

        height = self.height + 1 if height is None else height
        spent = spent if spent is not None else set()
        overlay = utxo_overlay or {}

        total_in = 0
        for idx, txin in enumerate(tx.inputs):
            op = txin.outpoint
            if op in spent:
                raise ValidationError(f"double spend of {op}")
            utxo = overlay.get(op) or self.utxos.get(op)
            if utxo is None:
                raise ValidationError(f"input not found in UTXO set: {op}")
            if utxo.coinbase and height - utxo.height < params.COINBASE_MATURITY:
                raise ValidationError("coinbase output not yet mature")
            if not tx.verify_input_signature(idx, utxo.address):
                raise ValidationError(f"bad signature on input {idx}")
            total_in += utxo.amount

        if total_in < tx.total_output:
            raise ValidationError("inputs less than outputs")
        return total_in - tx.total_output

    # --------------------------------------------------------------- mempool

    def add_transaction(self, tx: Transaction) -> str:
        """Validate and admit a transaction to the mempool. Returns its txid."""
        txid = tx.txid
        if txid in self.mempool:
            raise ValidationError("already in mempool")
        if len(self.mempool) >= MAX_MEMPOOL:
            raise ValidationError("mempool full")
        fee = self.validate_transaction(tx, spent=self.mempool_spends)
        if fee < params.MIN_RELAY_FEE:
            raise ValidationError(
                f"fee {fee} below minimum relay fee {params.MIN_RELAY_FEE}"
            )
        self.mempool[txid] = tx
        self.mempool_spends.update(i.outpoint for i in tx.inputs)
        return txid

    def _prune_mempool(self):
        """Drop mempool entries confirmed or conflicted by the new tip."""
        stale = []
        for txid, tx in self.mempool.items():
            if any(i.outpoint not in self.utxos for i in tx.inputs):
                stale.append(txid)
        for txid in stale:
            tx = self.mempool.pop(txid)
            for i in tx.inputs:
                self.mempool_spends.discard(i.outpoint)

    # ------------------------------------------------------ block validation

    def validate_block(self, block: Block, prev: Block) -> None:
        """Raise ValidationError unless `block` is a valid successor of `prev`."""
        if block.height != prev.height + 1:
            raise ValidationError("bad height")
        if block.prev_hash != prev.block_id:
            raise ValidationError("prev_hash does not match tip")
        if block.size() > params.MAX_BLOCK_SIZE:
            raise ValidationError("block too large")
        if block.target != self.next_target():
            raise ValidationError("wrong difficulty target")
        if not block.has_valid_pow():
            raise ValidationError("insufficient proof of work")
        if block.timestamp <= self.median_time_past():
            raise ValidationError("timestamp not after median-time-past")
        if block.timestamp > time.time() + params.MAX_FUTURE_DRIFT:
            raise ValidationError("timestamp too far in the future")

        txs = block.transactions
        if not txs or not txs[0].is_coinbase:
            raise ValidationError("first transaction must be coinbase")
        if any(tx.is_coinbase for tx in txs[1:]):
            raise ValidationError("multiple coinbase transactions")

        # the coinbase gets the same context-free checks as everything else
        # (positive amounts, valid addresses, size) so a miner can neither
        # burn coins into garbage addresses nor smuggle arbitrary strings
        # into every explorer and dashboard on the network
        ok, reason = txs[0].basic_check()
        if not ok:
            raise ValidationError(f"coinbase: {reason}")
        # ... and must commit to this block's height (keeps coinbase txids
        # unique across heights — Bitcoin's BIP34 for the same reason)
        try:
            cb_data = json.loads(bytes.fromhex(txs[0].inputs[0].pubkey))
            cb_height = cb_data["height"]
        except (ValueError, TypeError, KeyError):
            raise ValidationError("coinbase does not commit to a height")
        if cb_height != block.height:
            raise ValidationError("coinbase commits to the wrong height")

        txids = [tx.txid for tx in txs]
        if len(set(txids)) != len(txids):
            raise ValidationError("duplicate txid in block")

        spent: set = set()
        overlay: dict = {}
        fees = 0
        for tx in txs[1:]:
            fees += self.validate_transaction(
                tx, spent=spent, utxo_overlay=overlay, height=block.height
            )
            spent.update(i.outpoint for i in tx.inputs)
            for vout, out in enumerate(tx.outputs):
                overlay[(tx.txid, vout)] = UTXO(
                    out.amount, out.address, block.height, coinbase=False
                )

        max_reward = self.block_subsidy(block.height) + fees
        if txs[0].total_output > max_reward:
            raise ValidationError(
                f"coinbase pays {txs[0].total_output}, max is {max_reward}"
            )

    def add_block(self, block: Block, *, save: bool = True) -> None:
        self.validate_block(block, self.tip)

        # spend inputs, create outputs
        for tx in block.transactions:
            if not tx.is_coinbase:
                for txin in tx.inputs:
                    del self.utxos[txin.outpoint]
            for vout, out in enumerate(tx.outputs):
                self.utxos[(tx.txid, vout)] = UTXO(
                    out.amount, out.address, block.height, tx.is_coinbase
                )

        self.blocks.append(block)
        self._prune_mempool()
        if save:
            self.save()

    # -------------------------------------------------------------- queries

    def balance(self, address: str) -> dict:
        confirmed = spendable = 0
        for utxo in self.utxos.values():
            if utxo.address != address:
                continue
            confirmed += utxo.amount
            if (not utxo.coinbase
                    or self.height + 1 - utxo.height >= params.COINBASE_MATURITY):
                spendable += utxo.amount
        return {"confirmed": confirmed, "spendable": spendable}

    def utxos_for(self, address: str, spendable_only: bool = True) -> list[dict]:
        out = []
        for (txid, vout), u in self.utxos.items():
            if u.address != address:
                continue
            if (txid, vout) in self.mempool_spends:
                continue
            mature = (not u.coinbase
                      or self.height + 1 - u.height >= params.COINBASE_MATURITY)
            if spendable_only and not mature:
                continue
            out.append({"txid": txid, "vout": vout, "amount": u.amount,
                        "height": u.height, "coinbase": u.coinbase})
        out.sort(key=lambda x: -x["amount"])
        return out

    # ------------------------------------------------------- chain adoption

    @classmethod
    def from_block_dicts(cls, block_dicts: list[dict],
                         data_dir: str = None) -> "Blockchain":
        """Rebuild and fully re-validate a chain from serialized blocks."""
        if not block_dicts:
            raise ValidationError("empty chain")
        chain = cls(data_dir=data_dir, autoload=False)
        genesis = Block.from_dict(block_dicts[0])
        if genesis.block_id != chain.blocks[0].block_id:
            raise ValidationError("foreign chain has a different genesis block")
        for d in block_dicts[1:]:
            chain.add_block(Block.from_dict(d), save=False)
        return chain

    @staticmethod
    def claimed_work(block_dicts: list[dict]) -> int:
        """Total work a serialized chain CLAIMS via its header targets.

        Costs one hex-parse per block — no hashing. Used as a cheap gate
        before the expensive full re-validation in maybe_replace, so a
        malicious peer can't make us scrypt-verify a million-block junk
        chain that could never win anyway.
        """
        total = 0
        try:
            for d in block_dicts:
                total += (1 << 256) // (int(d["target"], 16) + 1)
        except (KeyError, ValueError, TypeError):
            return 0
        return total

    def extend_with(self, block_dicts: list[dict]) -> int:
        """Fast path for sync: append blocks that build directly on our tip.

        Fully validates each block (same rules as add_block). Stops at the
        first block that doesn't fit. Returns how many blocks were added.
        Used when a peer is simply ahead of us on the same branch — no need
        to re-download and re-validate the whole chain from genesis.
        """
        added = 0
        for d in block_dicts:
            try:
                block = Block.from_dict(d)
            except (KeyError, ValueError, TypeError):
                break
            if block.prev_hash != self.tip.block_id:
                continue  # skip blocks below/askew of our tip
            try:
                self.add_block(block, save=False)
                added += 1
            except ValidationError:
                break
        if added:
            self.save()
        return added

    def maybe_replace(self, block_dicts: list[dict]) -> bool:
        """Adopt a fully-validated foreign chain iff it has more total work."""
        # cheap gate first: if even the CLAIMED work can't beat ours, skip
        # the expensive scrypt re-validation entirely (anti-DoS)
        if self.claimed_work(block_dicts) <= self.total_work():
            return False
        try:
            candidate = Blockchain.from_block_dicts(block_dicts,
                                                    data_dir=self.data_dir)
        except (ValidationError, KeyError, IndexError, TypeError, ValueError):
            return False   # malformed or invalid chain — never adopt
        if candidate.total_work() <= self.total_work():
            return False
        pending = list(self.mempool.values())
        self.blocks = candidate.blocks
        self.utxos = candidate.utxos
        self.mempool, self.mempool_spends = {}, set()
        for tx in pending:  # re-admit whatever is still valid
            try:
                self.add_transaction(tx)
            except ValidationError:
                pass
        self.save()
        return True

    def validate_full(self) -> bool:
        """Re-validate the entire chain from genesis. Used by tests/tools."""
        Blockchain.from_block_dicts([b.to_dict() for b in self.blocks],
                                    data_dir=self.data_dir)
        return True

    # ---------------------------------------------------------- persistence

    def save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        path = os.path.join(self.data_dir, "chain.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"magic": params.NETWORK_MAGIC,
                       "blocks": [b.to_dict() for b in self.blocks],
                       "mempool": [t.to_dict() for t in self.mempool.values()]}, f)
        os.replace(tmp, path)

    def _load(self) -> bool:
        path = os.path.join(self.data_dir, "chain.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("magic") != params.NETWORK_MAGIC:
                return False
            restored = Blockchain.from_block_dicts(data["blocks"],
                                                   data_dir=self.data_dir)
            self.blocks, self.utxos = restored.blocks, restored.utxos
            for tx_dict in data.get("mempool", []):
                try:
                    self.add_transaction(Transaction.from_dict(tx_dict))
                except ValidationError:
                    pass  # stale entries are dropped on reload
            return True
        except (ValidationError, KeyError, IndexError, TypeError,
                json.JSONDecodeError, ValueError):
            print("warning: stored chain failed validation, starting from genesis")
            return False
