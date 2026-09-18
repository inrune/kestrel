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
from .block import Block, build_genesis, merkle_root
from .transaction import Transaction


class ValidationError(Exception):
    pass


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


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
        self.mempool_seen: dict[str, float] = {}   # txid -> when we first saw it
        self.mempool_dropped: dict[str, float] = {}  # txid -> when we gave up
        self._written = 0          # blocks already on disk (append cursor)
        self.stopped_at = None     # (height, why) if a load stopped early

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
            utxo = overlay.get(op) or self.utxos.get(op)
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
        if any((txid, v) in self.utxos for v in range(len(tx.outputs))):
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
            gone = any(i.outpoint not in self.utxos for i in tx.inputs)
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
        for (txid, vout), utxo in self.utxos.items():
            if utxo.address != address:
                continue
            confirmed += utxo.amount           # on-chain, pending spend or not
            if (txid, vout) in self.mempool_spends:
                continue                       # already promised to a payment
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
        genesis = Block.from_dict(block_dicts[0])
        if genesis.block_id != chain.blocks[0].block_id:
            raise ValidationError("foreign chain has a different genesis block")
        for d in block_dicts[1:]:
            if not partial:
                chain.add_block(Block.from_dict(d), save=False)
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
        except (KeyError, ValueError, TypeError):
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
            except (KeyError, ValueError, TypeError):
                break
            if block.prev_hash != self.tip.block_id:
                continue  # skip blocks below/askew of our tip
            try:
                self.add_block(block, save=False)
                added += 1
            except ValidationError:
                break
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
        seen_at = dict(self.mempool_seen)

        # Transactions that were confirmed in the blocks we are about to
        # discard must not simply disappear. Without this, a reorg silently
        # destroys real payments: the money reverts to the sender and the
        # recipient is never told. Re-queue them so they get mined into the
        # new chain instead. Only the diverged suffix is scanned — the two
        # chains share a prefix — and coinbases are skipped because a block
        # reward only exists inside the block that created it.
        new_ids = {b.block_id for b in candidate.blocks}
        orphaned = []
        for b in reversed(self.blocks):
            if b.block_id in new_ids:
                break
            orphaned.extend(t for t in b.transactions if not t.is_coinbase)
        orphaned.reverse()

        self.blocks = candidate.blocks
        self.utxos = candidate.utxos
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
        # history itself changed, so the append-only file must be redone.
        # The swap already happened in memory and this chain is the one
        # with the most work behind it whether or not the disk cooperates;
        # throwing here would report a successful reorg as a rejected one
        # to a caller that only expects ValidationError.
        try:
            self._save_blocks(rewrite=True)
            self._save_pool()
            self._write_mark()
        except OSError as e:
            from . import logfile
            self._written = None
            logfile.write(f"could not write the adopted chain to disk ({e}) "
                          f"— it is still correct in memory and will be "
                          f"written again on the next block", level="warn")
        return True

    def validate_full(self) -> bool:
        """Re-validate the entire chain from genesis. Used by tests/tools."""
        Blockchain.from_block_dicts([b.to_dict() for b in self.blocks],
                                    data_dir=self.data_dir)
        return True

    # ---------------------------------------------------------- persistence

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
    # blocks.jsonl, and a new block costs one line. A reorg — rare, and
    # bounded — rewrites the file. The mempool is small and changes for
    # other reasons, so it keeps its own little file.
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
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if m.get("magic") != params.NETWORK_MAGIC:
            return None
        h, tip = m.get("height"), m.get("tip")
        if isinstance(h, int) and h >= 0 and isinstance(tip, str):
            return h, tip
        return None

    def _write_mark(self):
        try:
            path = os.path.join(self.data_dir, self.MARK_FILE)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"magic": params.NETWORK_MAGIC,
                           "height": self.height,
                           "tip": self.tip.block_id}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
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
        if rewrite or self._written is None or self._written > len(self.blocks):
            self._rewrite_blocks()
            return
        if self._written == len(self.blocks):
            return                                  # nothing new
        try:
            with open(blocks_path, "a", encoding="utf-8") as f:
                for b in self.blocks[self._written:]:
                    f.write(json.dumps(b.to_dict(), separators=(",", ":"))
                            + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._written = len(self.blocks)
        except OSError:
            # out of disk, or the folder went away — try a clean rewrite
            # next time rather than leaving the file half-extended
            self._written = None
            raise

    def _rewrite_blocks(self):
        """Write every block out again. Only for a reorg or a migration."""
        blocks_path, _, _ = self._paths()
        tmp = blocks_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for b in self.blocks:
                    f.write(json.dumps(b.to_dict(),
                                       separators=(",", ":")) + "\n")
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
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self._written = len(self.blocks)

    def _save_pool(self):
        """The mempool, which is small and changes on its own schedule."""
        _, pool_path, _ = self._paths()
        tmp = pool_path + ".tmp"
        os.makedirs(self.data_dir, exist_ok=True)
        try:
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
        except OSError:
            raise

    # ------------------------------------------------------------- loading

    def _load(self) -> bool:
        blocks_path, pool_path, legacy = self._paths()
        block_dicts = None
        migrating = False
        truncated = False

        if os.path.exists(blocks_path):
            block_dicts, whole = self._read_block_lines(blocks_path)
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
            return False

        from . import logfile
        restored = None
        mark = None if migrating else self._read_mark()
        if mark:
            upto, tip = mark
            if 0 <= upto < len(block_dicts) and \
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
        self._written = len(self.blocks)
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
            from . import logfile
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
        for d in block_dicts[1:upto + 1]:
            try:
                b = Block.from_dict(d)
            except (KeyError, ValueError, TypeError):
                return None
            if b.prev_hash != blocks[-1].block_id or b.height != len(blocks):
                return None
            if b.merkle_root != merkle_root([t.txid for t in b.transactions]):
                return None
            for tx in b.transactions:
                if not tx.is_coinbase:
                    for txin in tx.inputs:
                        if utxos.pop(txin.outpoint, None) is None:
                            return None
                for vout, out in enumerate(tx.outputs):
                    utxos[(tx.txid, vout)] = UTXO(out.amount, out.address,
                                                  b.height, tx.is_coinbase)
            blocks.append(b)
        chain.blocks, chain.utxos = blocks, utxos
        # Every one of these came off disk, so the append cursor has to say
        # so. Left at zero — the default for a chain that has never written
        # anything — a save from here appends the whole chain a second time
        # rather than nothing at all.
        chain._written = len(blocks)
        return chain

    @staticmethod
    def _read_block_lines(path):
        """One block per line. A torn final line is dropped, not fatal.

        Returns (blocks, whole) — `whole` is False when the file had more
        in it than we could read, so the caller knows to write it back out
        cleanly rather than appending after the damage.
        """
        out, whole = [], True
        try:
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        from . import logfile
                        logfile.write(f"block store: ignoring damaged line "
                                      f"{n} and everything after it",
                                      level="warn")
                        whole = False
                        break
        except OSError:
            return None, True
        return out, whole

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
        except (OSError, json.JSONDecodeError, ValueError):
            return None, None
        if data.get("magic") != params.NETWORK_MAGIC:
            return None, None
        return data.get("blocks"), data

    @staticmethod
    def _read_pool(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if data.get("magic") != params.NETWORK_MAGIC:
            return None
        return data

    def _restore_pool(self, data):
        if not data:
            return
        seen = data.get("mempool_seen") or {}
        dropped = data.get("mempool_dropped") or {}
        self.mempool_dropped = {str(k): float(v) for k, v in dropped.items()
                                if _is_number(v)}
        for tx_dict in data.get("mempool", []):
            try:
                tx = Transaction.from_dict(tx_dict)
                at = seen.get(tx.txid)
                self.add_transaction(
                    tx, seen=float(at) if _is_number(at) else None,
                    allow_readmit=True)
            except (ValidationError, KeyError, ValueError, TypeError):
                pass  # stale entries are dropped on reload
        # entries that were already past their TTL when we shut down
        self.revalidate_mempool()
