"""Mempool hygiene: a pending payment must not lie about its future.

The bug these cover: a transaction that no miner ever picked up sat in the
mempool for good, and every wallet watching it kept saying the money was on
its way. The only thing that ever re-examined it was the reload path — so
closing the app and opening it again was, literally, the only way to find
out the truth. Running and restarted now agree.
"""

import os
import tempfile
import time
import unittest

from kestrel import params
from kestrel import blockchain as bc
from kestrel.blockchain import Blockchain, ValidationError
from kestrel.wallet import Wallet
from kestrel.miner import mine, mine_block


# Proof-of-work is the expensive part, so the fixture is mined exactly
# once for the whole module and each test rebuilds from the serialized
# blocks — full validation, no grinding.
ALICE = BOB = None
_BLOCKS = None


def setUpModule():
    global ALICE, BOB, _BLOCKS
    ALICE, BOB = Wallet.create(), Wallet.create()
    chain = Blockchain(data_dir=tempfile.mkdtemp(), autoload=False)
    mine(chain, ALICE.address, count=2, quiet=True)
    mine(chain, Wallet.create().address,
         count=params.COINBASE_MATURITY, quiet=True)
    _BLOCKS = [b.to_dict() for b in chain.blocks]


def funded_chain(tmp, wallets=2):
    """A fresh chain where ALICE holds mature, spendable coins."""
    chain = Blockchain.from_block_dicts(_BLOCKS, data_dir=tmp)
    return chain, ALICE, BOB


def pay(chain, frm, to, amount_ksl=3, fee=None):
    utxos = chain.utxos_for(frm.address, spendable_only=True)
    return frm.build_transaction(utxos, to.address,
                                 int(amount_ksl * params.COIN),
                                 fee or params.MIN_RELAY_FEE)


class TestSpendableIsHonest(unittest.TestCase):
    """What the wallet is told it can spend must be what it can spend."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_pending_payment_stops_counting_as_available(self):
        chain, alice, bob = funded_chain(self.tmp)
        before = chain.balance(alice.address)["spendable"]
        chain.add_transaction(pay(chain, alice, bob, 3))
        after = chain.balance(alice.address)["spendable"]
        self.assertLess(after, before)

    def test_balance_and_the_utxo_list_agree(self):
        """They are two answers to the same question and were disagreeing.

        balance() fed the headline figure and the Send screen's check;
        utxos_for() is what a payment is actually built from. With a
        payment already in flight the first was higher, so the app
        accepted an amount, confirmed it, and only the node refused.
        """
        chain, alice, bob = funded_chain(self.tmp)
        chain.add_transaction(pay(chain, alice, bob, 3))
        offered = sum(u["amount"] for u in
                      chain.utxos_for(alice.address, spendable_only=True))
        self.assertEqual(chain.balance(alice.address)["spendable"], offered)

    def test_spending_exactly_what_is_advertised_works(self):
        chain, alice, bob = funded_chain(self.tmp)
        chain.add_transaction(pay(chain, alice, bob, 3))
        avail = chain.balance(alice.address)["spendable"]
        utxos = chain.utxos_for(alice.address, spendable_only=True)
        tx = alice.build_transaction(utxos, bob.address,
                                     avail - params.MIN_RELAY_FEE,
                                     params.MIN_RELAY_FEE)
        chain.add_transaction(tx)            # must not raise

    def test_confirmed_still_counts_coins_committed_to_a_payment(self):
        """They are on the chain until a block says otherwise."""
        chain, alice, bob = funded_chain(self.tmp)
        before = chain.balance(alice.address)["confirmed"]
        chain.add_transaction(pay(chain, alice, bob, 3))
        self.assertEqual(chain.balance(alice.address)["confirmed"], before)


class TestLosingADoubleSpend(unittest.TestCase):
    """A payment beaten to its inputs must not be filed as a success."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _conflict(self):
        chain, alice, bob = funded_chain(self.tmp)
        carol = Wallet.create()
        utxos = chain.utxos_for(alice.address, spendable_only=True)
        mine_tx = alice.build_transaction(utxos, bob.address,
                                          params.COIN, params.MIN_RELAY_FEE)
        rival = alice.build_transaction(utxos, carol.address,
                                        params.COIN, params.MIN_RELAY_FEE * 3)
        chain.add_transaction(mine_tx)
        # somebody else's block carries the rival instead
        other = Blockchain.from_block_dicts([b.to_dict() for b in chain.blocks],
                                            data_dir=tempfile.mkdtemp())
        other.add_transaction(rival)
        mine_block(other, alice.address, quiet=True)
        from kestrel.block import Block
        chain.add_block(Block.from_dict(other.blocks[-1].to_dict()))
        return chain, mine_tx, bob, carol

    def test_it_is_not_reported_as_confirmed(self):
        chain, lost, bob, carol = self._conflict()
        self.assertNotIn(lost.txid, chain.mempool)
        # recorded as given up on, so a peer's mempool cannot hand it back
        # as though nothing happened
        self.assertIn(lost.txid, chain.mempool_dropped)

    def test_the_money_really_did_not_arrive(self):
        chain, lost, bob, carol = self._conflict()
        self.assertEqual(chain.balance(bob.address)["confirmed"], 0)
        self.assertGreater(chain.balance(carol.address)["confirmed"], 0)

    def test_a_payment_that_really_confirmed_is_still_a_success(self):
        chain, alice, bob = funded_chain(self.tmp)
        tx = pay(chain, alice, bob, 3)
        chain.add_transaction(tx)
        mine_block(chain, alice.address, quiet=True)
        self.assertNotIn(tx.txid, chain.mempool)
        self.assertNotIn(tx.txid, chain.mempool_dropped)   # not a rejection
        self.assertEqual(chain.balance(bob.address)["confirmed"],
                         3 * params.COIN)


class TestExpiry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._ttl, self._forget = bc.MEMPOOL_TTL, bc.MEMPOOL_FORGET
        bc.MEMPOOL_TTL = 2
        bc.MEMPOOL_FORGET = 60

    def tearDown(self):
        bc.MEMPOOL_TTL, bc.MEMPOOL_FORGET = self._ttl, self._forget

    def test_unconfirmed_transaction_eventually_expires(self):
        chain, alice, bob = funded_chain(self.tmp, 2)
        tx = pay(chain, alice, bob)
        chain.add_transaction(tx)
        self.assertIn(tx.txid, chain.mempool)

        # not yet — it has had no real chance to be mined
        self.assertEqual(chain.revalidate_mempool(), [])
        self.assertIn(tx.txid, chain.mempool)

        # past the deadline with no miner interested
        dropped = chain.revalidate_mempool(now=time.time() + 5)
        self.assertEqual(dropped, [(tx.txid, "expired")])
        self.assertNotIn(tx.txid, chain.mempool)
        self.assertEqual(chain.mempool_spends, set())

    def test_expiry_frees_the_senders_coins_again(self):
        chain, alice, bob = funded_chain(self.tmp, 2)
        before = len(chain.utxos_for(alice.address))
        chain.add_transaction(pay(chain, alice, bob))
        self.assertLess(len(chain.utxos_for(alice.address)), before)
        chain.revalidate_mempool(now=time.time() + 5)
        self.assertEqual(len(chain.utxos_for(alice.address)), before)

    def test_a_peer_cannot_resurrect_what_we_expired(self):
        """Otherwise expiry cannot work on a network of more than one node."""
        chain, alice, bob = funded_chain(self.tmp, 2)
        tx = pay(chain, alice, bob)
        chain.add_transaction(tx)
        chain.revalidate_mempool(now=time.time() + 5)

        with self.assertRaises(ValidationError):
            chain.add_transaction(tx)          # a peer handing it back
        self.assertNotIn(tx.txid, chain.mempool)

        # but the person at this machine retrying it outranks that
        chain.add_transaction(tx, allow_readmit=True)
        self.assertIn(tx.txid, chain.mempool)

class TestRevalidation(unittest.TestCase):
    """Running and restarted must reach the same answer."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_confirmation_removes_it_without_a_restart(self):
        chain, alice, bob = funded_chain(self.tmp, 2)
        tx = pay(chain, alice, bob)
        chain.add_transaction(tx)
        mine_block(chain, alice.address, quiet=True)
        self.assertNotIn(tx.txid, chain.mempool)
        self.assertEqual(chain.balance(bob.address)["confirmed"],
                         3 * params.COIN)

    def test_running_state_matches_restarted_state(self):
        chain, alice, bob = funded_chain(self.tmp, 2)
        chain.add_transaction(pay(chain, alice, bob))
        chain.save()
        live = set(chain.mempool)
        self.assertEqual(live, set(Blockchain(data_dir=self.tmp).mempool))

        mine_block(chain, alice.address, quiet=True)
        chain.save()
        self.assertEqual(set(chain.mempool),
                         set(Blockchain(data_dir=self.tmp).mempool))

    def test_spend_set_is_rebuilt_not_edited(self):
        """A spend set that drifts rejects perfectly good payments."""
        chain, alice, bob = funded_chain(self.tmp, 2)
        chain.add_transaction(pay(chain, alice, bob, 1))
        for _ in range(3):
            chain.revalidate_mempool()
        claimed = {i.outpoint for tx in chain.mempool.values()
                   for i in tx.inputs}
        self.assertEqual(chain.mempool_spends, claimed)

    def test_double_spend_conflict_is_dropped_on_the_next_block(self):
        chain, alice, bob = funded_chain(self.tmp, 2)
        utxos = chain.utxos_for(alice.address, spendable_only=True)
        a = alice.build_transaction(utxos, bob.address, params.COIN,
                                    params.MIN_RELAY_FEE)
        b = alice.build_transaction(utxos, bob.address, 2 * params.COIN,
                                    params.MIN_RELAY_FEE * 2)
        chain.add_transaction(a)
        with self.assertRaises(ValidationError):
            chain.add_transaction(b)          # same coins, refused up front
        mine_block(chain, alice.address, quiet=True)
        self.assertEqual(chain.mempool, {})

    def test_the_waiting_clock_survives_a_restart(self):
        """A restart must not hand every pending payment a fresh deadline.

        Ages are kept alongside the mempool on disk, so an hour of waiting
        is still an hour of waiting after the app is reopened.
        """
        chain, alice, bob = funded_chain(self.tmp)
        tx = pay(chain, alice, bob)
        chain.add_transaction(tx, seen=time.time() - 3600)
        chain.save()

        reopened = Blockchain(data_dir=self.tmp)
        self.assertIn(tx.txid, reopened.mempool)
        self.assertGreater(reopened.mempool_age(tx.txid), 3000)

    def test_age_is_reported(self):
        chain, alice, bob = funded_chain(self.tmp, 2)
        tx = pay(chain, alice, bob)
        chain.add_transaction(tx, seen=time.time() - 300)
        self.assertGreater(chain.mempool_age(tx.txid), 290)
        self.assertEqual(chain.mempool_age("not-a-txid"), 0.0)


class TestReorgKeepsPayments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_reorg_resets_the_clock_rather_than_expiring_a_payment(self):
        """A transaction undone by a reorg has not been sitting unwanted."""
        chain, alice, bob = funded_chain(self.tmp, 2)
        tx = pay(chain, alice, bob)
        chain.add_transaction(tx)
        blocks = [b.to_dict() for b in chain.blocks]
        mine_block(chain, alice.address, quiet=True)   # confirms tx
        self.assertNotIn(tx.txid, chain.mempool)

        # a heavier chain arrives that does not contain it
        rival = Blockchain.from_block_dicts(blocks, data_dir=self.tmp)
        mine(rival, alice.address, count=2, quiet=True)
        self.assertTrue(chain.maybe_replace([b.to_dict()
                                             for b in rival.blocks]))
        self.assertIn(tx.txid, chain.mempool)
        self.assertLess(chain.mempool_age(tx.txid), 5)


if __name__ == "__main__":
    unittest.main()
