"""Switching forks without replaying history.

A switch used to re-verify the heavier chain from genesis — every scrypt
proof-of-work and every signature — with the node locked the whole time,
and then rewrite the entire block file. v1.4.9 winds the UTXO set back to
the fork with per-block undo data, validates only the blocks after it,
and cuts the file back instead of rewriting it.

The rule every test here comes back to: the fast path must end up in
exactly the state full validation from genesis would have produced. A
node that disagreed with its peers about a single coin would be on a
different chain.
"""

import json
import os
import shutil
import tempfile
import unittest

from kestrel import params
from kestrel import blockchain as bc
from kestrel.block import Block
from kestrel.blockchain import Blockchain
from kestrel.miner import assemble_candidate, find_pow, mine
from kestrel.transaction import Transaction, TxInput, TxOutput
from kestrel.wallet import Wallet


def utxo_state(chain):
    return sorted((op, u.amount, u.address, u.height, u.coinbase)
                  for op, u in chain.utxos.items())


def mine_with(chain, txs, address):
    """Mine one block carrying exactly `txs` (no mempool involved)."""
    block = assemble_candidate(chain, address)
    fees = 0
    spent, overlay = set(), {}
    for tx in txs:
        fees += chain.validate_transaction(tx, spent=spent,
                                           utxo_overlay=overlay,
                                           height=block.height)
        spent.update(i.outpoint for i in tx.inputs)
        for v, o in enumerate(tx.outputs):
            overlay[(tx.txid, v)] = bc.UTXO(o.amount, o.address,
                                            block.height, False)
    block.transactions = [Transaction.coinbase(
        block.height, chain.block_subsidy(block.height) + fees, address,
        timestamp=block.timestamp)] + list(txs)
    assert find_pow(block, threads=2)
    chain.add_block(block)
    return block


def spend(wallet, utxo_list, to, amount, fee=params.MIN_RELAY_FEE):
    return wallet.build_transaction(utxo_list, to, amount, fee)


class Fixture:
    """Chain A: coinbases, a payment, and a block where one transaction
    spends an output another transaction in the SAME block created —
    the case undo has to unwind in the right order. Chain B forks off A
    and ends up heavier."""

    @classmethod
    def build(cls):
        cls.alice, cls.bob, cls.carol = (Wallet.create(), Wallet.create(),
                                         Wallet.create())
        a = Blockchain(data_dir=tempfile.mkdtemp(), autoload=False)
        mine(a, cls.alice.address, count=2, quiet=True)
        mine(a, cls.carol.address, count=params.COINBASE_MATURITY,
             quiet=True)
        # a plain payment, alice -> bob
        pay = spend(cls.alice, a.utxos_for(cls.alice.address)[:1],
                    cls.bob.address, 3 * params.COIN)
        mine_with(a, [pay], cls.carol.address)
        # alice pays bob, and bob immediately forwards part of it to carol,
        # both in the same block
        t1 = spend(cls.alice, a.utxos_for(cls.alice.address)[:1],
                   cls.bob.address, 5 * params.COIN)
        t2 = Transaction([TxInput(t1.txid, 0)],
                         [TxOutput(2 * params.COIN, cls.carol.address),
                          TxOutput(3 * params.COIN - params.MIN_RELAY_FEE,
                                   cls.bob.address)])
        t2.sign_input(0, cls.bob.private_key, cls.bob.public_key)
        mine_with(a, [t1, t2], cls.carol.address)
        mine(a, cls.carol.address, count=2, quiet=True)
        cls.fork_at = params.COINBASE_MATURITY + 3      # inside the history
        cls.a_dicts = [b.to_dict() for b in a.blocks]
        cls.a_top = a.height

        b = Blockchain.from_block_dicts(cls.a_dicts[:cls.fork_at],
                                        data_dir=tempfile.mkdtemp())
        mine(b, cls.bob.address, count=(a.height - cls.fork_at + 1) + 2,
             quiet=True)
        cls.b_dicts = [x.to_dict() for x in b.blocks]


def setUpModule():
    Fixture.build()


class FastSwitchMatchesFullValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = Blockchain.from_block_dicts(Fixture.a_dicts,
                                             data_dir=self.tmp)
        self.a.save()
        self.a._write_mark()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_undo_wind_back_is_exact_at_every_height(self):
        """Rolling back to height f must give the UTXO set that replaying
        blocks 0..f-1 from genesis gives — for every f."""
        for f in range(1, self.a.height + 2):
            base = self.a._base_at(f)
            self.assertIsNotNone(base, f)
            ref = Blockchain.from_block_dicts(Fixture.a_dicts[:f],
                                              data_dir=tempfile.mkdtemp())
            self.assertEqual(utxo_state(base), utxo_state(ref), f"height {f}")
            self.assertEqual(base.total_work(), ref.total_work())
            self.assertEqual(base.tip.block_id, ref.tip.block_id)

    def test_a_switch_lands_exactly_where_full_validation_does(self):
        self.assertTrue(self.a.maybe_replace(Fixture.b_dicts))
        ref = Blockchain.from_block_dicts(Fixture.b_dicts,
                                          data_dir=tempfile.mkdtemp())
        self.assertEqual(self.a.tip.block_id, ref.tip.block_id)
        self.assertEqual(utxo_state(self.a), utxo_state(ref))
        self.assertEqual(self.a.total_work(), ref.total_work())
        self.assertEqual(self.a.circulating_supply(),
                         ref.circulating_supply())
        for w in (Fixture.alice, Fixture.bob, Fixture.carol):
            # "confirmed" only: the switch re-queued A's payments, and a
            # pending payment rightly makes its coins not spendable
            self.assertEqual(self.a.balance(w.address)["confirmed"],
                             ref.balance(w.address)["confirmed"])

    def test_only_the_suffix_is_validated(self):
        """The whole point: blocks below the fork are not checked again."""
        seen = []
        real = Blockchain.validate_block

        def counting(chain, block, prev):
            seen.append(block.height)
            return real(chain, block, prev)
        Blockchain.validate_block = counting
        try:
            self.assertTrue(self.a.maybe_replace(Fixture.b_dicts))
        finally:
            Blockchain.validate_block = real
        self.assertTrue(seen)
        self.assertEqual(min(seen), Fixture.fork_at)

    def test_payments_undone_by_the_switch_are_requeued(self):
        self.assertTrue(self.a.maybe_replace(Fixture.b_dicts))
        # A's blocks above the fork carried the alice -> bob payments;
        # on chain B they never happened, so they are pending again
        # (whichever of them is still valid there)
        self.assertTrue(self.a.mempool)

    def test_a_lighter_chain_is_refused_before_any_validation(self):
        plan, status = self.a.plan_switch(Fixture.a_dicts[:5], 0)
        self.assertIsNone(plan)
        self.assertIn(status, ("same", "lighter"))
        heavy = Blockchain.from_block_dicts(Fixture.b_dicts,
                                            data_dir=tempfile.mkdtemp())
        plan, status = heavy.plan_switch(Fixture.a_dicts, 0)
        self.assertIsNone(plan)
        self.assertEqual(status, "lighter")

    def test_a_suffix_that_starts_mid_chain_is_enough(self):
        start = Fixture.fork_at - 2
        plan, status = self.a.plan_switch(Fixture.b_dicts[start:], start)
        self.assertEqual(status, "ok")
        self.assertEqual(plan.fork, Fixture.fork_at)
        self.assertTrue(self.a.adopt(plan.build(), plan))
        self.assertEqual(self.a.tip.block_id,
                         Block.from_dict(Fixture.b_dicts[-1]).block_id)

    def test_a_window_that_starts_above_the_fork_says_so(self):
        start = Fixture.fork_at + 2
        plan, status = self.a.plan_switch(Fixture.b_dicts[start:], start)
        self.assertIsNone(plan)
        self.assertEqual(status, "deeper")

    def test_a_tampered_suffix_is_rejected_whole(self):
        bad = json.loads(json.dumps(Fixture.b_dicts))
        bad[-1]["transactions"][0]["outputs"][0]["amount"] += 1   # overpays
        before = self.a.tip.block_id
        self.assertFalse(self.a.maybe_replace(bad))
        self.assertEqual(self.a.tip.block_id, before)
        self.assertEqual(utxo_state(self.a), utxo_state(
            Blockchain.from_block_dicts(Fixture.a_dicts,
                                        data_dir=tempfile.mkdtemp())))

    def test_garbage_is_refused_not_raised(self):
        for junk in (None, "x", [1, 2], [{}], [{"height": "nope"}],
                     [{"prev_hash": 5, "target": "zz"}]):
            self.assertFalse(self.a.maybe_replace(junk), junk)

    def test_a_foreign_genesis_is_refused(self):
        other = json.loads(json.dumps(Fixture.b_dicts))
        other[0]["nonce"] += 1
        self.assertFalse(self.a.maybe_replace(other))

    def test_the_prefix_is_rechecked_at_adoption(self):
        """Something else may move our chain while a candidate is being
        validated without the lock. The swap must notice."""
        start = Fixture.fork_at - 2
        plan, _ = self.a.plan_switch(Fixture.b_dicts[start:], start)
        cand = plan.build()
        # meanwhile, our own chain reorganises below the fork
        self.a.blocks = self.a.blocks[:Fixture.fork_at - 2]
        self.assertFalse(self.a.adopt(cand, plan))


class DeepForks(unittest.TestCase):
    def test_a_fork_older_than_the_undo_data_still_switches(self):
        a = Blockchain.from_block_dicts(Fixture.a_dicts,
                                        data_dir=tempfile.mkdtemp())
        # pretend the undo data only reaches back to near the tip
        a._undo_floor = a.height - 1
        for h in [h for h in a._undo if h < a._undo_floor]:
            del a._undo[h]
        self.assertIsNone(a._base_at(Fixture.fork_at))
        start = Fixture.fork_at - 1
        plan, status = a.plan_switch(Fixture.b_dicts[start:], start)
        self.assertEqual(status, "deeper")          # needs the whole chain
        plan, status = a.plan_switch(Fixture.b_dicts, 0)
        self.assertEqual(status, "full")
        self.assertTrue(a.adopt(plan.build(), plan))
        ref = Blockchain.from_block_dicts(Fixture.b_dicts,
                                          data_dir=tempfile.mkdtemp())
        self.assertEqual(utxo_state(a), utxo_state(ref))

    def test_a_deep_fork_offered_from_block_one_still_switches(self):
        """A pushed suffix starts at 1 at most. With the fork older than
        our undo data, that is the whole chain bar the genesis we share,
        so it must be validated in full rather than refused."""
        a = Blockchain.from_block_dicts(Fixture.a_dicts,
                                        data_dir=tempfile.mkdtemp())
        a._undo_floor = a.height - 1
        for h in [h for h in a._undo if h < a._undo_floor]:
            del a._undo[h]
        plan, status = a.plan_switch(Fixture.b_dicts[1:], 1)
        self.assertEqual(status, "full")
        self.assertTrue(a.adopt(plan.build(), plan))
        ref = Blockchain.from_block_dicts(Fixture.b_dicts,
                                          data_dir=tempfile.mkdtemp())
        self.assertEqual(utxo_state(a), utxo_state(ref))

    def test_undo_data_is_bounded(self):
        old = bc.UNDO_DEPTH
        bc.UNDO_DEPTH = 3
        try:
            c = Blockchain.from_block_dicts(Fixture.a_dicts,
                                            data_dir=tempfile.mkdtemp())
            # pruning happens in batches; force one
            c._undo.update({-i: {} for i in range(1, 400)})
            c._trim_undo(c.height)
            self.assertTrue(all(h >= c.height - 2 for h in c._undo))
            self.assertEqual(c._undo_floor, c.height - 2)
            self.assertIsNone(c._base_at(1))
            self.assertIsNotNone(c._base_at(c.height - 1))
        finally:
            bc.UNDO_DEPTH = old

    def test_undo_survives_a_restart(self):
        """A chain loaded from disk can switch without replaying either."""
        tmp = tempfile.mkdtemp()
        c = Blockchain.from_block_dicts(Fixture.a_dicts, data_dir=tmp)
        c.save()
        c._write_mark()
        back = Blockchain(data_dir=tmp)
        self.assertIsNotNone(back._base_at(Fixture.fork_at))
        self.assertTrue(back.maybe_replace(Fixture.b_dicts))
        ref = Blockchain.from_block_dicts(Fixture.b_dicts,
                                          data_dir=tempfile.mkdtemp())
        self.assertEqual(utxo_state(back), utxo_state(ref))


class ForkPoints(unittest.TestCase):
    def setUp(self):
        self.a = Blockchain.from_block_dicts(Fixture.a_dicts,
                                             data_dir=tempfile.mkdtemp())

    def test_statuses(self):
        a, A, B = self.a, Fixture.a_dicts, Fixture.b_dicts
        self.assertEqual(a.find_fork(A, 0)[1], "same")
        self.assertEqual(a.find_fork(A[3:], 3)[1], "same")
        self.assertEqual(a.find_fork(B, 0), (Fixture.fork_at, "ok"))
        self.assertEqual(a.find_fork(B[1:], 1), (Fixture.fork_at, "ok"))
        self.assertEqual(a.find_fork(B[-2:], len(B) - 2)[1], "deeper")
        self.assertEqual(a.find_fork(B[-1:], a.height + 5)[1], "gap")
        foreign = json.loads(json.dumps(A))
        foreign[0]["nonce"] += 1
        self.assertEqual(a.find_fork(foreign, 0)[1], "foreign")

    def test_an_extension_of_our_tip(self):
        ext = Blockchain.from_block_dicts(Fixture.a_dicts,
                                          data_dir=tempfile.mkdtemp())
        mine(ext, Fixture.bob.address, count=2, quiet=True)
        dicts = [b.to_dict() for b in ext.blocks]
        self.assertEqual(self.a.find_fork(dicts[self.a.height + 1:],
                                          self.a.height + 1),
                         (self.a.height + 1, "ok"))
        self.assertTrue(self.a.maybe_replace(dicts))
        self.assertEqual(self.a.tip.block_id, ext.tip.block_id)


class ReorgOnDisk(unittest.TestCase):
    """The file is cut back to the fork, not rewritten."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = Blockchain.from_block_dicts(Fixture.a_dicts,
                                             data_dir=self.tmp)
        self.a.save()
        self.a._write_mark()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name="blocks.jsonl"):
        return os.path.join(self.tmp, name)

    def lines(self):
        with open(self.path(), "rb") as f:
            return f.read().splitlines()

    def test_the_file_is_cut_back_not_rewritten(self):
        prefix = self.lines()[:Fixture.fork_at]
        real = self.a._rewrite_blocks
        self.a._rewrite_blocks = lambda: self.fail("rewrote the whole file")
        try:
            self.assertTrue(self.a.maybe_replace(Fixture.b_dicts))
        finally:
            self.a._rewrite_blocks = real
        lines = self.lines()
        self.assertEqual(len(lines), self.a.height + 1)
        self.assertEqual(lines[:Fixture.fork_at], prefix)   # untouched
        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.tip.block_id, self.a.tip.block_id)
        self.assertEqual(utxo_state(back), utxo_state(self.a))
        mark = json.load(open(self.path("validated.json")))
        self.assertEqual(mark["height"], self.a.height)

    def test_the_append_cursor_is_right_afterwards(self):
        self.assertTrue(self.a.maybe_replace(Fixture.b_dicts))
        mine(self.a, Fixture.bob.address, count=1, quiet=True)
        self.assertEqual(len(self.lines()), self.a.height + 1)
        self.assertEqual(Blockchain(data_dir=self.tmp).tip.block_id,
                         self.a.tip.block_id)

    def test_a_crash_between_cut_and_append_loads_the_prefix(self):
        """Power cut after the truncation, before the new branch landed:
        the next start finds the shared prefix and a mark that matches
        it — never a file that disagrees with its own mark."""
        real = self.a._save_blocks

        def dies(*a, **k):
            raise OSError(5, "I/O error")
        self.a._save_blocks = dies
        try:
            self.assertTrue(self.a.maybe_replace(Fixture.b_dicts))
        finally:
            self.a._save_blocks = real
        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.height, Fixture.fork_at - 1)
        self.assertEqual(back.tip.block_id,
                         Block.from_dict(Fixture.a_dicts[Fixture.fork_at - 1]
                                         ).block_id)
        # and the running node repairs the file on its next write
        mine(self.a, Fixture.bob.address, count=1, quiet=True)
        again = Blockchain(data_dir=self.tmp)
        self.assertEqual(again.tip.block_id, self.a.tip.block_id)

    def test_windows_line_endings_are_handled(self):
        """Files written by 1.4.8 on Windows end every line in CRLF."""
        with open(self.path(), "rb") as f:
            data = f.read().replace(b"\n", b"\r\n")
        with open(self.path(), "wb") as f:
            f.write(data)
        c = Blockchain(data_dir=self.tmp)
        self.assertEqual(c.height, self.a.height)
        self.assertTrue(c.maybe_replace(Fixture.b_dicts))
        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.tip.block_id, c.tip.block_id)
        self.assertEqual(back.height, c.height)

    def test_a_file_changed_behind_our_back_is_rewritten_not_spliced(self):
        with open(self.path(), "ab") as f:
            f.write(b'{"stray": "line"}\n')
        self.assertTrue(self.a.maybe_replace(Fixture.b_dicts))
        self.assertEqual(len(self.lines()), self.a.height + 1)
        self.assertEqual(Blockchain(data_dir=self.tmp).tip.block_id,
                         self.a.tip.block_id)


class DerivedState(unittest.TestCase):
    """Supply, work and per-address balances are kept, not recomputed —
    so they must never drift from what a full recount says."""

    def test_caches_agree_with_a_recount_through_everything(self):
        tmp = tempfile.mkdtemp()
        a = Blockchain.from_block_dicts(Fixture.a_dicts, data_dir=tmp)

        def check(c):
            self.assertEqual(c.circulating_supply(),
                             sum(u.amount for u in c.utxos.values()))
            self.assertEqual(c.total_work(), sum(b.work for b in c.blocks))
            for w in (Fixture.alice, Fixture.bob, Fixture.carol):
                brute = sum(u.amount for u in c.utxos.values()
                            if u.address == w.address)
                self.assertEqual(c.balance(w.address)["confirmed"], brute)
                self.assertEqual(c.address_totals().get(w.address, 0),
                                 brute)
        a.balance(Fixture.bob.address)          # build the address map
        check(a)
        mine(a, Fixture.bob.address, count=1, quiet=True)
        check(a)
        self.assertTrue(a.maybe_replace(Fixture.b_dicts))
        check(a)
        mine(a, Fixture.alice.address, count=1, quiet=True)
        check(a)
        check(Blockchain(data_dir=tmp) if a._written else a)

    def test_version_moves_with_the_chain(self):
        a = Blockchain.from_block_dicts(Fixture.a_dicts,
                                        data_dir=tempfile.mkdtemp())
        v = a.version
        mine(a, Fixture.bob.address, count=1, quiet=True)
        self.assertGreater(a.version, v)
        v = a.version
        a.maybe_replace(Fixture.b_dicts)
        self.assertGreater(a.version, v)


if __name__ == "__main__":
    unittest.main()


class OddButValidOutputs(unittest.TestCase):
    """Consensus has always accepted an output "address" that is a JSON
    list of base58 characters. Nothing can spend it; 1.4.9's per-address
    cache must not choke on it, or 1.4.9 would split from 1.4.8."""

    def test_a_list_address_neither_splits_nor_breaks_balances(self):
        w = Wallet.create()
        chain = Blockchain(data_dir=tempfile.mkdtemp(), autoload=False)
        mine(chain, w.address, count=1, quiet=True)
        chain.balance(w.address)                    # warm the cache
        burn = list(w.address)
        block = mine_with(chain, [], burn)          # coinbase to the list
        self.assertEqual(chain.tip.block_id, block.block_id)
        self.assertGreater(chain.balance(w.address)["confirmed"], 0)
        chain._by_addr = None                       # and from cold
        self.assertGreater(chain.address_totals()[w.address], 0)
        self.assertEqual(chain.balance(w.address)["confirmed"],
                         chain.address_totals()[w.address])
        reloaded = Blockchain(data_dir=chain.data_dir)
        self.assertEqual(reloaded.height, chain.height)


class ClaimsMustCostWork(unittest.TestCase):
    """A peer's header targets decide how much work it claims. Blocks
    whose hash doesn't meet their own target are cut off before any
    planning under the lock."""

    def test_a_block_with_an_impossible_target_is_cut(self):
        from kestrel.node import Node
        a = Blockchain.from_block_dicts(Fixture.a_dicts,
                                        data_dir=tempfile.mkdtemp())
        node = Node(a, port=0)
        start = Fixture.fork_at - 1
        good = Fixture.b_dicts[start:]
        self.assertEqual(node._worked_prefix(good, start), good)
        forged = json.loads(json.dumps(good))
        forged[3]["target"] = "0" * 63 + "1"
        self.assertEqual(node._worked_prefix(forged, start), forged[:3])
        plan, status = a.plan_switch(forged[:3], start)
        self.assertIn(status, ("lighter", "ok"))
