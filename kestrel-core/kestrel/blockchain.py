"""
Kestrel consensus engine.

Maintains the chain, the UTXO set and the mempool, and enforces every
consensus rule: proof-of-work, difficulty retargeting, the 44,000,000 KSL
emission schedule, transaction validity and coinbase maturity.
"""

import json
import math
import os
import time

from . import params
from .block import Block, build_genesis, merkle_root
from .transaction import Transaction


class ValidationError(Exception):
    pass


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True             # math.isfinite overflows on huge ints
    return isinstance(v, float) and math.isfinite(v)


# Anything a malformed block or transaction can raise while being parsed.
# Network input is hostile by default, so "could not parse it" has to be a
# rejection, never an exception that escapes into a sync loop or a handler.
MALFORMED = (KeyError, IndexError, TypeError, ValueError, AttributeError,
             ArithmeticError, RecursionError)


# Local relay policy (not consensus): max pending transactions a node keeps.
MAX_MEMPOOL = 10_000

# How long a transaction may sit unconfirmed before a node forgets it.
# Not a consensus rule — each node decides for itself — but without one a
# payment that no miner ever picks up stays "pending" forever, and every
# wallet on the network keeps telling its owner the money is on the way.
# Bitcoin Core uses 14 days; Kestrel's blocks are two minutes, so a day is
# already ~720 chances to be mined. After this the sender's coins are
# spendable again and the recipient is told it did not happen.
MEMPOOL_TTL = 24 * 60 * 60          # 24 hours

# A transaction this node deliberately dropped is remembered for a while,
# so the next mempool sync with a peer that still holds it doesn't simply
# hand it straight back. Without this, expiry cannot work on a network of
# more than one node: every round would resurrect what the last round
# expired, and the payment would hang forever.
MEMPOOL_FORGET = 6 * 60 * 60        # 6 hours

# How many recent blocks keep "undo" data: the coins each one spent, so the
# UTXO set can be wound back to any of those heights without replaying the
# chain from genesis. Switching to a heavier fork used to re-verify every
# block ever mined — ~0.4ms each, so half a minute at today's height and
# growing by two minutes a year, all of it with the node locked. With undo
# data it costs only the blocks after the fork. Ten thousand blocks is two
# weeks; a fork deeper than that falls back to the full re-verification,
# which is still correct, just slow. It is a few hundred KB of memory.
UNDO_DEPTH = 10_000


class UTXO:
    __slots__ = ("amount", "address", "height", "coinbase")

    def __init__(self, amount: int, address: str, height: int, coinbase: bool):
        self.amount = amount
        self.address = address
        self.height = height
        self.coinbase = coinbase


class SwitchPlan:
    """How to get from our chain to a heavier one, worked out cheaply.

    Made by Blockchain.plan_switch while holding the chain's lock; `build`
    does the expensive part — full validation of everything after the
    fork — and needs no lock at all, because it only touches a private
    copy. Blockchain.adopt then swaps the result in, under the lock again,
    if it still has more work than we do by then.
    """

    __slots__ = ("fork", "prefix_id", "suffix", "base", "full", "data_dir")

    def __init__(self, fork, prefix_id, suffix=None, base=None, full=None,
                 data_dir=None):
        self.fork = fork              # first height that differs
        self.prefix_id = prefix_id    # our block at fork-1, which both share
        self.suffix = suffix or []    # their blocks from `fork` upward
        self.base = base              # our chain wound back to fork-1
        self.full = full              # fallback: their whole chain
        self.data_dir = data_dir

    def build(self) -> "Blockchain":
        """Validate the candidate chain. Raises ValidationError if it fails."""
        try:
            if self.base is None:
                return Blockchain.from_block_dicts(self.full,
                                                   data_dir=self.data_dir)
            cand, self.base = self.base, None          # single use
            for d in self.suffix:
                cand.add_block(Block.from_dict(d), save=False)
            return cand
        except ValidationError:
            raise
        except MALFORMED as e:
            raise ValidationError(
                f"malformed block ({type(e).__name__}: {e})") from None


class Blockchain:
    def __init__(self, data_dir: str = None, autoload: bool = True):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "kestrel-data")
        self.blocks: list[Block] = []
        self._utxos: dict[tuple, UTXO] = {}       # (txid, vout) -> UTXO
        self.mempool: dict[str, Transaction] = {}  # txid -> tx
        self.mempool_spends: set[tuple] = set()    # outpoints claimed by mempool
        self.mempool_seen: dict[str, float] = {}   # txid -> when we first saw it
        self.mempool_dropped: dict[str, float] = {}  # txid -> when we gave up
        self._written = 0          # blocks already on disk (append cursor)
        self._offsets = []         # byte offset of each stored block's line
        self._file_end = 0         # where the next line goes
        self.stopped_at = None     # (height, why) if a load stopped early

        # Derived state, kept so that ordinary questions — how much work,
        # how many coins, what does this address hold — cost what the
        # answer is worth rather than a walk over the whole chain. Every
        # peer asks for the first two every fifteen seconds.
        self._work: list[int] = []  # cumulative work, parallel to blocks
        self._work_last = None      # the block _work[-1] was computed for
        self._supply = None         # sum of every UTXO, None when unknown
        self._by_addr = None        # address -> {outpoint: UTXO}, or None
        self._undo: dict[int, dict] = {}   # height -> {outpoint: UTXO spent}
        self._undo_floor = 1        # undo is complete for heights >= this
        self.version = 0            # bumps whenever the chain state changes

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
        self._undo, self._undo_floor = {}, 1
        # NB: no save() here — scratch chains (validation, sync) share data_dir
        # and must never overwrite the persisted chain. Saving happens on
        # add_block / maybe_replace.

    # -------------------------------------------------------------- basics

    @property
    def utxos(self) -> dict:
        return self._utxos

    @utxos.setter
    def utxos(self, value: dict):
        # Whoever replaces the UTXO set wholesale invalidates everything
        # derived from it; add_block keeps those up to date incrementally.
        self._utxos = value
        self._supply = None
        self._by_addr = None
        self.version += 1

    @property
    def height(self) -> int:
        return self.blocks[-1].height

    @property
    def tip(self) -> Block:
        return self.blocks[-1]

    def _sync_work(self) -> list:
        """Cumulative work per height, extended rather than recomputed.

        Anything that swaps `blocks` for another list is detected by
        identity — the block the cache last described is no longer where
        it was — and the list is rebuilt from scratch, which is correct
        whatever happened.
        """
        w, blocks = self._work, self.blocks
        n = len(w)
        if not (n and n <= len(blocks) and blocks[n - 1] is self._work_last):
            w, n = [], 0
        total = w[-1] if w else 0
        for b in blocks[n:]:
            total += b.work
            w.append(total)
        self._work = w
        self._work_last = blocks[-1]
        return w

    def total_work(self) -> int:
        return self._sync_work()[-1]

    def work_at(self, height: int) -> int:
        """Total work of our chain up to and including `height`."""
        return self._sync_work()[height]

    def circulating_supply(self) -> int:
        if self._supply is None:
            self._supply = sum(u.amount for u in self._utxos.values())
        return self._supply

    def _addr_map(self) -> dict:
        """address -> {outpoint: UTXO}. Built once, then kept current."""
        if self._by_addr is None:
            m: dict[str, dict] = {}
            for op, u in self._utxos.items():
                if type(u.address) is str:
                    m.setdefault(u.address, {})[op] = u
            self._by_addr = m
        return self._by_addr

    def address_totals(self) -> dict:
        """address -> confirmed balance, for every address holding coins."""
        return {a: sum(u.amount for u in outs.values())
                for a, outs in self._addr_map().items()}

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
                             height: int = None,
                             verify_signatures: bool = True) -> int:
        """Validate a non-coinbase transaction against the UTXO set.

        `spent` / `utxo_overlay` let block validation account for earlier
        transactions in the same block. Returns the fee in feathers.

        `verify_signatures=False` skips the ECDSA checks. It is only for
        re-checking a transaction this node already admitted and verified:
        a signature cannot decay, but maturity, the UTXO set and fee policy
        all can. Never use it on anything arriving from outside.
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
            utxo = overlay.get(op) or self._utxos.get(op)
            if utxo is None:
                raise ValidationError(f"input not found in UTXO set: {op}")
            if utxo.coinbase and height - utxo.height < params.COINBASE_MATURITY:
                raise ValidationError("coinbase output not yet mature")
            if verify_signatures and \
                    not tx.verify_input_signature(idx, utxo.address):
                raise ValidationError(f"bad signature on input {idx}")
            total_in += utxo.amount

        if total_in < tx.total_output:
            raise ValidationError("inputs less than outputs")
        return total_in - tx.total_output

    # --------------------------------------------------------------- mempool

    def add_transaction(self, tx: Transaction, *, seen: float = None,
                        allow_readmit: bool = False) -> str:
        """Validate and admit a transaction to the mempool. Returns its txid.

        `seen` preserves the original first-seen time across a reload or a
        reorg, so an old transaction cannot refresh its own expiry clock
        simply by being re-added.
        """
        txid = tx.txid
        if txid in self.mempool:
            raise ValidationError("already in mempool")
        if not allow_readmit and self._recently_dropped(txid):
            raise ValidationError("recently expired or rejected here")
        if len(self.mempool) >= MAX_MEMPOOL:
            raise ValidationError("mempool full")
        fee = self.validate_transaction(tx, spent=self.mempool_spends)
        if fee < params.MIN_RELAY_FEE:
            raise ValidationError(
                f"fee {fee} below minimum relay fee {params.MIN_RELAY_FEE}"
            )
        self.mempool[txid] = tx
        self.mempool_spends.update(i.outpoint for i in tx.inputs)
        self.mempool_seen[txid] = min(seen or time.time(), time.time())
        self.mempool_dropped.pop(txid, None)
        return txid

    # -- mempool hygiene ---------------------------------------------------
    #
    # A pending transaction used to be re-examined in exactly one place: the
    # reload path, where every entry is validated again from scratch and the
    # ones that no longer hold are quietly dropped. While the app was
    # *running*, the only check was "are this transaction's inputs still
    # unspent" — so anything that had gone bad for any other reason, and
    # anything that was simply never going to be mined, sat in the mempool
    # forever telling the owner their money was on the way. Closing the app
    # and opening it again was the only way to find out the truth.
    #
    # These two now do the same work. `revalidate_mempool` is the reload
    # check, runnable at any moment; `_prune_mempool` calls it on every new
    # tip. What the app shows while running is what it shows after a restart.

    def _recently_dropped(self, txid: str) -> bool:
        at = self.mempool_dropped.get(txid)
        if at is None:
            return False
        if time.time() - at > MEMPOOL_FORGET:
            self.mempool_dropped.pop(txid, None)
            return False
        return True

    CONFIRMED = "confirmed"
    REPLACED = "replaced by another payment"

    def _forget(self, txid: str, tx: Transaction, reason: str):
        self.mempool.pop(txid, None)
        self.mempool_seen.pop(txid, None)
        for i in tx.inputs:
            self.mempool_spends.discard(i.outpoint)
        # "confirmed" is a success, not a rejection — never block a re-add
        if reason != self.CONFIRMED:
            self.mempool_dropped[txid] = time.time()

    def _why_gone(self, txid: str, tx: Transaction, included: set = None):
        """This transaction's inputs are spent. By it, or by something else?

        The block being applied is the reliable answer and is passed in.
        Failing that, a transaction that was mined has outputs of its own
        in the UTXO set unless every one has since been spent, so an
        unspent output is proof it confirmed. When neither is available
        the honest answer is that it is no longer valid, rather than
        guessing 'confirmed' and telling someone money arrived.
        """
        if included is not None:
            return self.CONFIRMED if txid in included else self.REPLACED
        if any((txid, v) in self._utxos for v in range(len(tx.outputs))):
            return self.CONFIRMED
        return self.REPLACED

    def revalidate_mempool(self, *, now: float = None,
                           included: set = None) -> list[tuple]:
        """Re-check every pending transaction. Returns [(txid, reason), …].

        Reasons are 'confirmed' (it made it into a block), 'replaced by
        another payment' (something else spent its inputs first),
        'expired' (no miner took it within MEMPOOL_TTL), or a validation
        message.

        `included` is the set of txids in the block that has just been
        applied. Without it, "the inputs are gone" is ambiguous: it means
        either this payment was mined, or a conflicting one was. They were
        both reported as 'confirmed', so a payment that lost a double
        spend was recorded as a success and the recipient was never told —
        the money simply never arrived and nothing anywhere said why.
        """
        now = now or time.time()
        dropped = []
        # Rebuild the spend set from scratch rather than editing it in place:
        # a set that drifts out of step with the mempool silently rejects
        # perfectly good payments as double spends.
        keep: dict[str, Transaction] = {}
        spends: set[tuple] = set()
        for txid, tx in list(self.mempool.items()):
            seen = self.mempool_seen.get(txid, now)
            gone = any(i.outpoint not in self._utxos for i in tx.inputs)
            if gone:
                why = self._why_gone(txid, tx, included)
                dropped.append((txid, why))
                self._forget(txid, tx, why)
                continue
            if now - seen > MEMPOOL_TTL:
                dropped.append((txid, "expired"))
                self._forget(txid, tx, "expired")
                continue
            try:
                # signatures were checked when it was admitted and cannot
                # decay; skipping them keeps this cheap enough to run on
                # every single block, which is what makes it self-correcting
                fee = self.validate_transaction(tx, spent=spends,
                                                verify_signatures=False)
                if fee < params.MIN_RELAY_FEE:
                    raise ValidationError("fee below the minimum relay fee")
            except ValidationError as e:
                dropped.append((txid, str(e)))
                self._forget(txid, tx, str(e))
                continue
            keep[txid] = tx
            spends.update(i.outpoint for i in tx.inputs)
        self.mempool = keep
        self.mempool_spends = spends
        self.mempool_seen = {t: self.mempool_seen.get(t, now) for t in keep}
        # let the drop list age out so it can't grow without bound
        cutoff = now - MEMPOOL_FORGET
        self.mempool_dropped = {t: a for t, a in self.mempool_dropped.items()
                                if a > cutoff}
        return dropped

    def mempool_age(self, txid: str, *, now: float = None) -> float:
        """Seconds this transaction has been waiting, or 0.0 if unknown."""
        seen = self.mempool_seen.get(txid)
        return max((now or time.time()) - seen, 0.0) if seen else 0.0

    def _prune_mempool(self, included: set = None):
        """Re-examine the mempool against the new tip."""
        return self.revalidate_mempool(included=included)

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
        for tx, txid in zip(txs[1:], txids[1:]):
            fees += self.validate_transaction(
                tx, spent=spent, utxo_overlay=overlay, height=block.height
            )
            spent.update(i.outpoint for i in tx.inputs)
            for vout, out in enumerate(tx.outputs):
                overlay[(txid, vout)] = UTXO(
                    out.amount, out.address, block.height, coinbase=False
                )

        max_reward = self.block_subsidy(block.height) + fees
        if txs[0].total_output > max_reward:
            raise ValidationError(
                f"coinbase pays {txs[0].total_output}, max is {max_reward}"
            )

    def _apply(self, block: Block) -> None:
        """Spend a validated block's inputs and create its outputs.

        Records what it spent, so the block can be undone later without
        replaying the chain, and keeps the supply and the per-address view
        current instead of throwing them away.
        """
        utxos, by_addr = self._utxos, self._by_addr
        spent: dict[tuple, UTXO] = {}
        created = destroyed = 0
        for tx in block.transactions:
            coinbase = tx.is_coinbase
            if not coinbase:
                for txin in tx.inputs:
                    op = txin.outpoint
                    u = utxos.pop(op)
                    spent[op] = u
                    destroyed += u.amount
                    if by_addr is not None and type(u.address) is str:
                        bucket = by_addr.get(u.address)
                        if bucket is not None:
                            bucket.pop(op, None)
                            if not bucket:
                                del by_addr[u.address]
            txid = tx.txid
            for vout, out in enumerate(tx.outputs):
                u = UTXO(out.amount, out.address, block.height, coinbase)
                utxos[(txid, vout)] = u
                created += out.amount
                # Consensus has always accepted an output whose "address"
                # is a JSON list of base58 characters. Nothing can ever
                # spend it, and it can't key a dict, so it stays out of
                # the per-address view rather than crashing it.
                if by_addr is not None and type(out.address) is str:
                    by_addr.setdefault(out.address, {})[(txid, vout)] = u
        if self._supply is not None:
            self._supply += created - destroyed
        self._undo[block.height] = spent
        self._trim_undo(block.height)
        self.version += 1

    def _trim_undo(self, height: int):
        floor = height - UNDO_DEPTH + 1
        if floor <= self._undo_floor:
            return
        # prune in batches, not on every block
        if len(self._undo) > UNDO_DEPTH + 256:
            for h in [h for h in self._undo if h < floor]:
                del self._undo[h]
            self._undo_floor = floor

    def add_block(self, block: Block, *, save: bool = True) -> None:
        self.validate_block(block, self.tip)
        self._apply(block)
        self.blocks.append(block)
        # Tell the mempool pass which transactions this block actually
        # carried, so a payment that LOST a double spend is reported as
        # replaced rather than quietly filed as a success.
        dropped = self._prune_mempool({t.txid for t in block.transactions})
        for txid, why in dropped:
            if why != self.CONFIRMED:
                from . import logfile
                logfile.write(f"pending transaction {txid[:12]}… dropped "
                              f"({why})", level="warn")
        if save:
            # A block that validated is part of this chain whether or not
            # the disk cooperates. A full disk, a folder syncing in the
            # background, an antivirus holding the file open — none of
            # those are a reason to throw out of here, because the caller
            # is a mining round or an HTTP handler that would treat it as
            # "block rejected" and, worse, could die on the way out and
            # take the loop with it. The write is retried on the next
            # block, from scratch, so nothing half-written survives.
            try:
                self.save()
                # this block was just validated in full; say so, so the
                # next start does not do it again
                self._write_mark()
            except OSError as e:
                from . import logfile
                self._written = None
                logfile.write(f"could not write block {block.height:,} to "
                              f"disk ({e}) — the chain is still correct in "
                              f"memory and will be written again shortly",
                              level="warn")

    # -------------------------------------------------------------- queries

    def balance(self, address: str) -> dict:
        """What this address holds, and what it can actually pay with.

        `spendable` has to agree with utxos_for(), because that is the list
        a payment is built from. It used to count coins that an unconfirmed
        payment had already committed, so the wallet showed a balance it
        would then refuse to spend: the Send screen accepted the amount,
        the confirm dialog agreed, and only the node said no — by which
        point the person had been told twice that the money was there.
        """
        confirmed = spendable = 0
        for op, utxo in self._addr_map().get(address, {}).items():
            confirmed += utxo.amount           # on-chain, pending spend or not
            if op in self.mempool_spends:
                continue                       # already promised to a payment
            if (not utxo.coinbase
                    or self.height + 1 - utxo.height >= params.COINBASE_MATURITY):
                spendable += utxo.amount
        return {"confirmed": confirmed, "spendable": spendable}

    def utxos_for(self, address: str, spendable_only: bool = True) -> list[dict]:
        out = []
        for (txid, vout), u in self._addr_map().get(address, {}).items():
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
                         data_dir: str = None,
                         partial: bool = False) -> "Blockchain":
        """Rebuild and fully re-validate a chain from serialized blocks.

        With partial=True, stop at the first block that doesn't hold up and
        return everything before it instead of raising. That is only ever
        used for our own store on disk: a chain from the network is taken
        whole or not at all, but a local file with a bad block at height
        300,000 should cost you the tail, not the 300,000 blocks in front
        of it. Nothing invalid is accepted either way — the prefix is
        validated by exactly the same rules.
        """
        if not block_dicts:
            raise ValidationError("empty chain")
        chain = cls(data_dir=data_dir, autoload=False)
        try:
            genesis = Block.from_dict(block_dicts[0])
        except MALFORMED as e:
            raise ValidationError(f"malformed genesis block ({e})") from None
        if genesis.block_id != chain.blocks[0].block_id:
            raise ValidationError("foreign chain has a different genesis block")
        for d in block_dicts[1:]:
            if not partial:
                try:
                    block = Block.from_dict(d)
                except MALFORMED as e:
                    raise ValidationError(f"malformed block ({e})") from None
                chain.add_block(block, save=False)
                continue
            # check first, apply second, so a rejected block can never
            # leave a half-applied UTXO set behind
            try:
                block = Block.from_dict(d)
                chain.validate_block(block, chain.tip)
            except Exception as e:
                chain.stopped_at = (len(chain.blocks), e)
                break
            chain.add_block(block, save=False)
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
        except MALFORMED:
            return 0
        return total

    def extend_with(self, block_dicts: list[dict], *, save: bool = True) -> int:
        """Fast path for sync: append blocks that build directly on our tip.

        Fully validates each block (same rules as add_block). Stops at the
        first block that doesn't fit. Returns how many blocks were added.
        Used when a peer is simply ahead of us on the same branch — no need
        to re-download and re-validate the whole chain from genesis.

        `save=False` is for the scratch chains built during loading, which
        share the real data directory and must not write to it — see the
        note in _init_genesis. Saving from one of those wrote the whole
        chain out a second time and emptied the stored mempool on top of
        it, because a scratch chain has never written a byte and its
        append cursor says so.
        """
        added = 0
        for d in block_dicts:
            try:
                block = Block.from_dict(d)
            except MALFORMED:
                break
            if block.prev_hash != self.tip.block_id:
                continue  # skip blocks below/askew of our tip
            try:
                self.add_block(block, save=False)
                added += 1
            except (ValidationError,) + MALFORMED:
                break           # hostile input is a refusal, not a crash
        if added and save:
            try:
                self.save()
                # these were validated in full just now, so say so — without
                # this the mark stays behind the chain after every bulk sync
                # and the next start re-verifies everything above it
                self._write_mark()
            except OSError as e:
                from . import logfile
                self._written = None
                logfile.write(f"could not write {added} new block(s) to disk "
                              f"({e}) — the chain is still correct in memory",
                              level="warn")
        return added

    # -- switching forks ----------------------------------------------------
    #
    # Two chains that share history up to some height and then disagree:
    # the heavier one wins. Working out which is heavier is cheap. Proving
    # the heavier one is VALID is the expensive part, and it only needs
    # doing for the blocks after the fork — everything before it is a block
    # we already hold and already checked, byte for byte (a block id
    # commits to the header, which commits to every transaction). So the
    # plan is: find the fork, wind a private copy of our UTXO set back to
    # it with the undo data, validate their blocks on top of that copy,
    # and swap it in only if it still wins.

    def _block_id_of(self, d) -> str | None:
        try:
            return Block.from_dict(d).block_id
        except MALFORMED:
            return None

    def find_fork(self, block_dicts: list, start: int = 0):
        """Where a foreign chain stops matching ours.

        `block_dicts[i]` claims height `start + i`. Returns (fork, status):

          'ok'       their block at `fork` builds on our block `fork - 1`
          'same'     nothing they sent is new to us
          'deeper'   they diverge from us somewhere below `start`
          'foreign'  a different genesis block — another network
          'bad'      their blocks don't hang together
          'gap'      they start above our tip; there is a hole between
        """
        n = len(block_dicts)
        end = start + n - 1                       # their highest height
        if n == 0:
            return None, "same"
        if start > self.height + 1:
            return None, "gap"
        top = min(self.height, end)

        def agree(h):
            return self.blocks[h].block_id == \
                self._block_id_of(block_dicts[h - start])

        if top < start:                          # pure extension of our tip
            fork = start
        elif not agree(start):
            fork = start
        else:
            # agreement is monotone — equal ids at h mean equal history
            # below h — so binary-search the last height we share
            lo, hi = start, top
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if agree(mid):
                    lo = mid
                else:
                    hi = mid - 1
            fork = lo + 1
        if fork > end:
            return fork, "same"
        if fork == 0:
            return 0, "foreign"
        first = block_dicts[fork - start]
        prev = first.get("prev_hash") if isinstance(first, dict) else None
        if prev != self.blocks[fork - 1].block_id:
            return fork, ("deeper" if fork == start else "bad")
        return fork, "ok"

    def _base_at(self, fork: int):
        """A private copy of this chain wound back to just before `fork`.

        Holds our blocks 0 .. fork-1 and the UTXO set exactly as it stood
        after block fork-1, rebuilt from the undo data rather than from
        genesis. None when the undo data does not reach that far back.
        """
        top = self.height
        if not 1 <= fork <= top + 1:
            return None
        if fork <= top and fork < self._undo_floor:
            return None
        utxos = dict(self._utxos)
        for h in range(top, fork - 1, -1):
            spent = self._undo.get(h)
            if spent is None:
                return None
            # last transaction first: a later one in the same block may
            # spend an output an earlier one created
            for tx in reversed(self.blocks[h].transactions):
                txid = tx.txid
                for vout in range(len(tx.outputs)):
                    utxos.pop((txid, vout), None)
                if not tx.is_coinbase:
                    for txin in tx.inputs:
                        u = spent.get(txin.outpoint)
                        if u is None:
                            return None          # undo data doesn't fit
                        utxos[txin.outpoint] = u
        work = self._sync_work()
        base = Blockchain(data_dir=self.data_dir, autoload=False)
        base.blocks = self.blocks[:fork]
        base.utxos = utxos
        base._undo = {h: s for h, s in self._undo.items() if h < fork}
        base._undo_floor = self._undo_floor
        base._work, base._work_last = work[:fork], base.blocks[-1]
        base._written = None       # a scratch chain: never to be saved
        return base

    def plan_switch(self, block_dicts, start: int = 0):
        """Decide whether a foreign chain is worth validating, cheaply.

        Returns (plan, status). `plan` is None unless there is something
        to try; status is one of find_fork's, or 'lighter' when even the
        work their blocks claim cannot beat ours.
        """
        if not isinstance(block_dicts, list) or not all(
                isinstance(d, dict) for d in block_dicts):
            return None, "bad"
        try:
            start = int(start)
        except (TypeError, ValueError):
            return None, "bad"
        if start < 0:
            return None, "bad"
        fork, status = self.find_fork(block_dicts, start)
        if status == "ok":
            suffix = block_dicts[fork - start:]
            claimed = self.work_at(fork - 1) + self.claimed_work(suffix)
            if claimed <= self.total_work():
                return None, "lighter"
            base = self._base_at(fork)
            if base is not None:
                return SwitchPlan(fork, self.blocks[fork - 1].block_id,
                                  suffix=suffix, base=base,
                                  data_dir=self.data_dir), "ok"
            if start > 1:
                return None, "deeper"      # need their whole chain for this
            if start == 1:
                # They sent everything but the genesis, which is ours by
                # definition of "ok" — no need to ask for it again.
                block_dicts = [self.blocks[0].to_dict()] + block_dicts
                start = 0
            status = "full"
        if status in ("foreign", "full") and start == 0:
            # The fork is older than our undo data (or the genesis differs,
            # which full validation reports properly). Validate it all —
            # the slow way, but outside any lock and only if it can win.
            if self.claimed_work(block_dicts) <= self.total_work():
                return None, "lighter"
            return SwitchPlan(0, None, full=block_dicts,
                              data_dir=self.data_dir), "full"
        return None, status

    def adopt(self, candidate: "Blockchain", plan: SwitchPlan = None) -> bool:
        """Swap in a validated candidate chain if it has more work than ours.

        Checks, under whatever lock the caller holds, that our chain still
        contains the prefix the candidate was built on — something else
        may have moved it while the candidate was being validated.
        """
        if candidate.blocks[0].block_id != self.blocks[0].block_id:
            return False
        if plan is not None and plan.prefix_id is not None:
            f = plan.fork
            if f - 1 > self.height or \
                    self.blocks[f - 1].block_id != plan.prefix_id:
                return False
        if candidate.total_work() <= self.total_work():
            return False
        fork = self._shared_prefix(candidate)

        # Transactions that were confirmed in the blocks we are about to
        # discard must not simply disappear. Without this, a reorg silently
        # destroys real payments: the money reverts to the sender and the
        # recipient is never told. Re-queue them so they get mined into the
        # new chain instead. Only the diverged suffix is scanned — the two
        # chains share a prefix — and coinbases are skipped because a block
        # reward only exists inside the block that created it.
        orphaned = [t for b in self.blocks[fork:] for t in b.transactions
                    if not t.is_coinbase]
        pending = list(self.mempool.values())
        seen_at = dict(self.mempool_seen)

        self.blocks = candidate.blocks
        self.utxos = candidate.utxos
        self._undo, self._undo_floor = candidate._undo, candidate._undo_floor
        self._work, self._work_last = candidate._work, candidate._work_last
        self.mempool, self.mempool_spends, self.mempool_seen = {}, set(), {}
        # Orphaned first: they were confirmed before anything still pending.
        # Anything the new chain already contains, or that it invalidates,
        # is refused here by the normal rules — exactly as it should be.
        # A transaction undone by a reorg gets its waiting clock reset: it
        # *was* confirmed, so it has not been sitting unwanted, and expiring
        # it for time it spent in a block would be wrong.
        now = time.time()
        for tx in orphaned:
            try:
                self.add_transaction(tx, seen=now, allow_readmit=True)
            except ValidationError:
                pass
        for tx in pending:
            try:
                self.add_transaction(tx, seen=seen_at.get(tx.txid, now),
                                     allow_readmit=True)
            except ValidationError:
                pass
        # History itself changed, so the stored chain has to follow. The
        # swap already happened in memory and this chain is the one with
        # the most work behind it whether or not the disk cooperates;
        # throwing here would report a successful reorg as a rejected one
        # to a caller that only expects ValidationError.
        try:
            self._store_reorg(fork)
            self._save_pool()
            self._write_mark()
        except OSError as e:
            from . import logfile
            self._written = None
            logfile.write(f"could not write the adopted chain to disk ({e}) "
                          f"— it is still correct in memory and will be "
                          f"written again on the next block", level="warn")
        return True

    def _shared_prefix(self, other: "Blockchain") -> int:
        """How many leading blocks two chains have in common."""
        lo, hi = 0, min(len(self.blocks), len(other.blocks)) - 1
        if hi < 0 or self.blocks[0].block_id != other.blocks[0].block_id:
            return 0
        while lo < hi:                      # last height where both agree
            mid = (lo + hi + 1) // 2
            if self.blocks[mid] is other.blocks[mid] or \
                    self.blocks[mid].block_id == other.blocks[mid].block_id:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def maybe_replace(self, block_dicts: list[dict]) -> bool:
        """Adopt a fully-validated foreign chain iff it has more total work."""
        plan, _status = self.plan_switch(block_dicts, 0)
        if plan is None:
            return False
        try:
            candidate = plan.build()
        except ValidationError:
            return False          # malformed or invalid chain — never adopt
        return self.adopt(candidate, plan)

    def validate_full(self) -> bool:
        """Re-validate the entire chain from genesis. Used by tests/tools."""
        Blockchain.from_block_dicts([b.to_dict() for b in self.blocks],
                                    data_dir=self.data_dir)
        return True

    # ---------------------------------------------------------- persistence
    #
    # The chain used to live in one chain.json that was rewritten in full
    # every time a block arrived. That is fine for a toy and impossible for
    # a real chain: serialising every block you have ever seen, every two
    # minutes, forever. Measured on this code — 20,000 blocks took 440ms
    # and 14MB per block; 50,000 took 1.1 seconds and 35MB. Kestrel mines
    # 262,800 blocks a year, so within months the app would spend more time
    # writing the chain than doing anything else, and the disk would take a
    # beating it does not deserve.
    #
    # Blocks are therefore append-only: one JSON object per line in
    # blocks.jsonl, and a new block costs one line. A reorg cuts the file
    # back to the fork and appends the new branch — it used to rewrite the
    # entire file, which for a routine one-block reorg meant writing every
    # block ever mined again. The mempool is small and changes for other
    # reasons, so it keeps its own little file.
    #
    # A half-written final line (power cut mid-append) is detected and
    # dropped on load, which is strictly safer than the old scheme: there,
    # a power cut during the rewrite of a 35MB file put the whole chain at
    # risk, and only the .tmp-and-rename dance saved it.

    BLOCKS_FILE = "blocks.jsonl"
    POOL_FILE = "mempool.json"
    LEGACY_FILE = "chain.json"
    MARK_FILE = "validated.json"

    def _paths(self):
        d = self.data_dir
        return (os.path.join(d, self.BLOCKS_FILE),
                os.path.join(d, self.POOL_FILE),
                os.path.join(d, self.LEGACY_FILE))

    @staticmethod
    def _line(block: Block) -> bytes:
        return (json.dumps(block.to_dict(), separators=(",", ":"))
                + "\n").encode("utf-8")

    # -------------------------------------------------- validation marker
    #
    # Opening the app re-ran full consensus validation over the entire
    # chain — every scrypt proof-of-work and every ECDSA signature, from
    # genesis, every single time. Measured here that is ~0.4ms a block, so
    # ~18 seconds at today's height, about 1.7 minutes a year from now and
    # over five after three years. It is also work that was already done:
    # these blocks were validated when this node accepted them.
    #
    # So we write down how far we have validated. On the next start,
    # anything up to that mark is re-applied rather than re-verified —
    # still checked for corruption, because that is cheap (a sha256 of the
    # header, the merkle root, and the prev-hash chain), but without the
    # scrypt and the signatures, which is where the time goes. Anything
    # past the mark is validated in full, as is anything if the mark does
    # not describe the chain on disk.
    #
    # The trust boundary is unchanged: this only ever trusts a file on
    # your own disk, in the same folder as the wallet that holds your
    # money. Anyone who can edit one can edit the other.

    def _read_mark(self):
        try:
            with open(os.path.join(self.data_dir, self.MARK_FILE),
                      encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(m, dict) or m.get("magic") != params.NETWORK_MAGIC:
            return None
        h, tip = m.get("height"), m.get("tip")
        if isinstance(h, int) and not isinstance(h, bool) and h >= 0 \
                and isinstance(tip, str):
            return h, tip
        return None

    def _write_mark(self, height: int = None):
        """Record that everything up to `height` (default: the tip) was
        validated in full by this node."""
        h = self.height if height is None else height
        try:
            path = os.path.join(self.data_dir, self.MARK_FILE)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"magic": params.NETWORK_MAGIC,
                           "height": h,
                           "tip": self.blocks[h].block_id}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except (OSError, IndexError):
            pass          # a missing mark only costs time, never safety

    def save(self):
        """Persist anything not yet on disk. Cheap in the common case."""
        os.makedirs(self.data_dir, exist_ok=True)
        self._save_blocks()
        self._save_pool()

    def _save_blocks(self, *, rewrite: bool = False):
        blocks_path, _, _ = self._paths()
        # None means "we don't know what's in that file" — a failed load, or
        # a write that died halfway. Appending to it would splice two
        # different chains together, so it gets written out from scratch.
        if (rewrite or self._written is None or self._offsets is None
                or self._written > len(self.blocks)
                or len(self._offsets) != self._written):
            self._rewrite_blocks()
            return
        if self._written == len(self.blocks):
            return                                  # nothing new
        try:
            with open(blocks_path, "ab") as f:
                pos = f.seek(0, os.SEEK_END)
                if pos != self._file_end:
                    # the file is not the one we last wrote — something
                    # else touched it, or a write was lost. Never append
                    # to something we cannot vouch for.
                    raise _Mismatch()
                for b in self.blocks[self._written:]:
                    line = self._line(b)
                    self._offsets.append(pos)
                    f.write(line)
                    pos += len(line)
                f.flush()
                os.fsync(f.fileno())
            self._file_end = pos
            self._written = len(self.blocks)
        except _Mismatch:
            self._rewrite_blocks()
        except OSError:
            # out of disk, or the folder went away — try a clean rewrite
            # next time rather than leaving the file half-extended
            self._written = None
            self._offsets = None
            raise

    def _rewrite_blocks(self):
        """Write every block out again. For a migration, or a store we
        cannot vouch for."""
        blocks_path, _, _ = self._paths()
        tmp = blocks_path + ".tmp"
        offsets, pos = [], 0
        try:
            with open(tmp, "wb") as f:
                for b in self.blocks:
                    line = self._line(b)
                    offsets.append(pos)
                    f.write(line)
                    pos += len(line)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, blocks_path)
        except OSError:
            # The file still holds the OLD chain while memory holds the new
            # one. Leaving the cursor where it was would append the new
            # chain onto the end of the old one — two different histories
            # in one file, which is the worst state this store can reach.
            # Unknown means "rewrite from scratch next time" instead.
            self._written = None
            self._offsets = None
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self._written = len(self.blocks)
        self._offsets, self._file_end = offsets, pos

    def _store_reorg(self, fork: int):
        """Make the stored chain match memory after a switch at `fork`.

        The file already holds every block below the fork, so it is cut
        back to there and the new branch is appended. The validation mark
        is moved down to the shared prefix first: a power cut in between
        leaves a shorter chain that loads quickly and is re-checked above
        the mark, never a file that disagrees with its own mark.
        """
        keep = min(fork, self._written or 0)
        blocks_path, _, _ = self._paths()
        if (self._written is None or self._offsets is None or keep < 1
                or len(self._offsets) != self._written
                or not os.path.exists(blocks_path)
                or os.path.getsize(blocks_path) != self._file_end):
            self._rewrite_blocks()
            return
        self._write_mark(keep - 1)
        cut = self._offsets[keep] if keep < len(self._offsets) \
            else self._file_end
        try:
            with open(blocks_path, "r+b") as f:
                f.truncate(cut)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            self._written = None
            self._offsets = None
            raise
        self._offsets = self._offsets[:keep]
        self._written, self._file_end = keep, cut
        self._save_blocks()

    def _save_pool(self):
        """The mempool, which is small and changes on its own schedule."""
        _, pool_path, _ = self._paths()
        tmp = pool_path + ".tmp"
        os.makedirs(self.data_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"magic": params.NETWORK_MAGIC,
                       "mempool": [t.to_dict()
                                   for t in self.mempool.values()],
                       # first-seen times travel with the mempool: a
                       # restart must not hand every pending payment a
                       # fresh 24 hours
                       "mempool_seen": self.mempool_seen,
                       "mempool_dropped": self.mempool_dropped}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, pool_path)

    # ------------------------------------------------------------- loading

    def _load(self) -> bool:
        blocks_path, pool_path, legacy = self._paths()
        block_dicts = None
        migrating = False
        truncated = False
        offsets, end = None, 0

        if os.path.exists(blocks_path):
            block_dicts, whole, offsets, end = \
                self._read_block_file(blocks_path)
            truncated = not whole
        elif os.path.exists(legacy):
            block_dicts, pool = self._read_legacy(legacy)
            migrating = block_dicts is not None
            if migrating:
                self._legacy_pool = pool
        if not block_dicts:
            if os.path.exists(blocks_path):
                # there is a file, we just couldn't use it: the fresh
                # genesis chain must replace it, never be appended to it
                self._written = None
                self._offsets = None
            return False

        from . import logfile
        restored = None
        mark = None if migrating else self._read_mark()
        if mark:
            upto, tip = mark
            if 0 <= upto < len(block_dicts) and \
                    isinstance(block_dicts[upto], dict) and \
                    block_dicts[upto].get("prev_hash") is not None:
                try:
                    cand = Blockchain._replay(block_dicts, upto,
                                              self.data_dir)
                except Exception:
                    cand = None
                if cand is not None and cand.tip.block_id == tip:
                    restored = cand
                    # everything past the mark gets the full treatment
                    rest = block_dicts[upto + 1:]
                    if rest:
                        try:
                            # save=False: this is a scratch chain pointed at
                            # the real data directory, and it must not write
                            restored.extend_with(rest, save=False)
                        except Exception:
                            restored = None
                    if restored is not None and \
                            len(restored.blocks) != len(block_dicts):
                        restored = None       # a gap: do it properly
                if restored is None:
                    logfile.write("stored validation mark did not match the "
                                  "chain on disk — verifying in full")

        if restored is None:
            # Validate as far as the file holds up, rather than all-or-
            # nothing. On a chain hundreds of thousands of blocks long,
            # throwing all of it away over one bad block means a full
            # re-download — and the bad block is nearly always near the
            # end anyway, since the tail is what a power cut or a full
            # disk lands on. Everything kept passed exactly the same
            # checks as always; the rest comes back from the network like
            # any other missing blocks.
            try:
                restored = Blockchain.from_block_dicts(
                    block_dicts, data_dir=self.data_dir, partial=True)
            except (ValidationError, KeyError, IndexError, TypeError,
                    json.JSONDecodeError, ValueError) as e:
                logfile.write(f"stored chain failed validation ({e}) — "
                              f"starting from genesis", level="warn")
                self._written = None
                self._offsets = None
                return False
            stopped = getattr(restored, "stopped_at", None)
            if stopped:
                at, why = stopped
                lost = len(block_dicts) - at
                logfile.write(
                    f"block {at} in the store did not hold up ({why}) — "
                    f"keeping the {at:,} good block(s) and dropping the last "
                    f"{lost:,}; they will be downloaded again", level="warn")
                self._keep_rejected(block_dicts[at:])
                truncated = True

        self.blocks, self.utxos = restored.blocks, restored.utxos
        self._undo, self._undo_floor = restored._undo, restored._undo_floor
        self._written = len(self.blocks)
        if migrating or truncated or offsets is None \
                or len(offsets) != len(self.blocks):
            self._offsets = None
        else:
            self._offsets, self._file_end = offsets, end
        if truncated and not migrating:
            try:
                self._rewrite_blocks()
            except OSError:
                self._written = None
        self._write_mark()

        pool = getattr(self, "_legacy_pool", None)
        if pool is None:
            pool = self._read_pool(pool_path)
        self._restore_pool(pool)

        if migrating:
            logfile.write(f"migrating {len(self.blocks):,} blocks to the "
                          f"append-only store")
            try:
                self._rewrite_blocks()
                self._save_pool()
                # keep the old file rather than deleting it: it is the
                # only copy of the chain until the new one is proven
                os.replace(legacy, legacy + ".pre-1.4.8")
            except OSError as e:
                logfile.write(f"migration could not finish ({e})",
                              level="warn")
        return True

    @classmethod
    def _replay(cls, block_dicts, upto, data_dir):
        """Rebuild the chain to `upto` without re-verifying the maths.

        Still refuses anything that does not hang together — the header
        hashes to the id it claims, the merkle root matches the
        transactions it carries, and each block names the one before it.
        That catches a damaged file, which is what can realistically go
        wrong here. Returns None if anything looks off, and the caller
        falls back to validating the lot.
        """
        chain = cls(data_dir=data_dir, autoload=False)
        genesis = Block.from_dict(block_dicts[0])
        if genesis.block_id != chain.blocks[0].block_id:
            return None
        blocks = [chain.blocks[0]]
        utxos = {}
        undo = {}
        keep_from = max(1, upto - UNDO_DEPTH + 1)
        for d in block_dicts[1:upto + 1]:
            try:
                b = Block.from_dict(d)
            except MALFORMED:
                return None
            if b.prev_hash != blocks[-1].block_id or b.height != len(blocks):
                return None
            if b.merkle_root != merkle_root([t.txid for t in b.transactions]):
                return None
            spent = {} if b.height >= keep_from else None
            for tx in b.transactions:
                if not tx.is_coinbase:
                    for txin in tx.inputs:
                        u = utxos.pop(txin.outpoint, None)
                        if u is None:
                            return None
                        if spent is not None:
                            spent[txin.outpoint] = u
                for vout, out in enumerate(tx.outputs):
                    utxos[(tx.txid, vout)] = UTXO(out.amount, out.address,
                                                  b.height, tx.is_coinbase)
            if spent is not None:
                undo[b.height] = spent
            blocks.append(b)
        chain.blocks, chain.utxos = blocks, utxos
        chain._undo, chain._undo_floor = undo, keep_from
        # Every one of these came off disk, so the append cursor has to say
        # so. Left at zero — the default for a chain that has never written
        # anything — a save from here appends the whole chain a second time
        # rather than nothing at all.
        chain._written = len(blocks)
        chain._offsets = None
        return chain

    @staticmethod
    def _read_block_file(path):
        """One block per line. A torn final line is dropped, not fatal.

        Returns (blocks, whole, offsets, end): `whole` is False when the
        file had more in it than we could read, so the caller knows to
        write it back out cleanly rather than appending after the damage;
        `offsets` is the byte position of each block's line and `end` the
        position just past the last good one — what lets a reorg cut the
        file back instead of rewriting all of it.
        """
        out, offsets, whole = [], [], True
        pos = end = 0
        try:
            with open(path, "rb") as f:
                for n, raw in enumerate(f, 1):
                    start, pos = pos, pos + len(raw)
                    line = raw.strip()
                    if not line:
                        end = pos
                        continue
                    try:
                        d = json.loads(line)
                        if not isinstance(d, dict):
                            raise ValueError("not a block")
                    except (ValueError, RecursionError):
                        from . import logfile
                        logfile.write(f"block store: ignoring damaged line "
                                      f"{n} and everything after it",
                                      level="warn")
                        whole = False
                        break
                    if not raw.endswith(b"\n"):
                        whole = False          # complete, but never ended
                    out.append(d)
                    offsets.append(start)
                    end = pos
        except OSError:
            return None, True, None, 0
        return out, whole, offsets, end

    @staticmethod
    def _read_block_lines(path):
        """(blocks, whole) — kept for tools that read the store directly."""
        blocks, whole, _offsets, _end = Blockchain._read_block_file(path)
        return blocks, whole

    def _keep_rejected(self, block_dicts):
        """Set aside blocks we refused, if there aren't many.

        Only useful for working out afterwards what went wrong, so it is
        not worth duplicating a multi-gigabyte file for: past a few
        thousand blocks the tail is dropped without a copy.
        """
        if not block_dicts or len(block_dicts) > 2000:
            return
        try:
            with open(os.path.join(self.data_dir, "blocks.rejected.jsonl"),
                      "w", encoding="utf-8") as f:
                for d in block_dicts:
                    f.write(json.dumps(d, separators=(",", ":")) + "\n")
        except (OSError, TypeError, ValueError):
            pass

    def _read_legacy(self, path):
        """The pre-1.4.8 single-file format."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, RecursionError):
            return None, None
        if not isinstance(data, dict) or \
                data.get("magic") != params.NETWORK_MAGIC:
            return None, None
        blocks = data.get("blocks")
        if not isinstance(blocks, list):
            return None, None
        return blocks, data

    @staticmethod
    def _read_pool(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(data, dict) or \
                data.get("magic") != params.NETWORK_MAGIC:
            return None
        return data

    def _restore_pool(self, data):
        """Put the saved mempool back. A damaged file costs the pending
        list, never the start-up: whatever doesn't parse is skipped."""
        if not isinstance(data, dict):
            return
        seen = data.get("mempool_seen")
        seen = seen if isinstance(seen, dict) else {}
        dropped = data.get("mempool_dropped")
        dropped = dropped if isinstance(dropped, dict) else {}
        self.mempool_dropped = {str(k): float(v) for k, v in dropped.items()
                                if _is_number(v)}
        entries = data.get("mempool")
        for tx_dict in entries if isinstance(entries, list) else []:
            try:
                tx = Transaction.from_dict(tx_dict)
                at = seen.get(tx.txid)
                self.add_transaction(
                    tx, seen=float(at) if _is_number(at) else None,
                    allow_readmit=True)
            except (ValidationError,) + MALFORMED:
                pass  # stale entries are dropped on reload
        # entries that were already past their TTL when we shut down
        self.revalidate_mempool()


class _Mismatch(Exception):
    """The block file is not in the state this chain last left it."""
