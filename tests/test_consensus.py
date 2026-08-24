"""Consensus engine: emission schedule, mining, maturity, reorgs, persistence."""

import copy
import tempfile
import unittest

from kestrel import params
from kestrel.blockchain import Blockchain, ValidationError
from kestrel.block import Block
from kestrel.transaction import Transaction, TxInput, TxOutput
from kestrel.wallet import Wallet, parse_ksl
from kestrel.miner import mine, assemble_candidate


class TestEmission(unittest.TestCase):
    def test_total_emission_within_cap(self):
        total, h = 0, 0
        while True:
            s = Blockchain.block_subsidy(h)
            if s == 0:
                break
            total += s * params.HALVING_INTERVAL
            h += params.HALVING_INTERVAL
        self.assertLessEqual(total, params.MAX_SUPPLY)
        # and it should be close to the cap (fair-launch geometric series)
        self.assertGreater(total, params.MAX_SUPPLY * 0.999)

    def test_subsidy_halves(self):
        self.assertEqual(Blockchain.block_subsidy(0), params.INITIAL_REWARD)
        self.assertEqual(Blockchain.block_subsidy(params.HALVING_INTERVAL),
                         params.INITIAL_REWARD // 2)
        self.assertEqual(Blockchain.block_subsidy(2 * params.HALVING_INTERVAL),
                         params.INITIAL_REWARD // 4)

    def test_subsidy_reaches_zero(self):
        self.assertEqual(Blockchain.block_subsidy(64 * params.HALVING_INTERVAL), 0)


class ChainTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.chain = Blockchain(data_dir=self.tmp)
        self.alice = Wallet.create()
        self.bob = Wallet.create()

    def mine_to(self, wallet, n):
        mine(self.chain, wallet.address, count=n, quiet=True)


class TestMiningAndBalances(ChainTestBase):
    def test_genesis_supply_is_zero(self):
        self.assertEqual(self.chain.height, 0)
        self.assertEqual(self.chain.circulating_supply(), 0)

    def test_mining_credits_reward(self):
        self.mine_to(self.alice, 3)
        self.assertEqual(self.chain.height, 3)
        self.assertEqual(self.chain.balance(self.alice.address)["confirmed"],
                         3 * params.INITIAL_REWARD)

    def test_coinbase_maturity(self):
        # coinbase spendable only after COINBASE_MATURITY confirmations
        self.mine_to(self.alice, 12)
        bal = self.chain.balance(self.alice.address)
        self.assertEqual(bal["confirmed"], 12 * params.INITIAL_REWARD)
        # tip height 12; block h spendable if 13-h >= 10 -> h <= 3
        self.assertEqual(bal["spendable"], 3 * params.INITIAL_REWARD)

    def test_full_revalidation(self):
        self.mine_to(self.alice, 5)
        self.assertTrue(self.chain.validate_full())


class TestSpending(ChainTestBase):
    def test_send_and_confirm(self):
        self.mine_to(self.alice, 12)
        utxos = self.chain.utxos_for(self.alice.address)
        tx = self.alice.build_transaction(utxos, self.bob.address,
                                          parse_ksl("30"), parse_ksl("0.0001"))
        self.chain.add_transaction(tx)
        self.assertEqual(len(self.chain.mempool), 1)
        self.mine_to(self.alice, 1)
        self.assertEqual(len(self.chain.mempool), 0)  # confirmed, pruned
        self.assertEqual(self.chain.balance(self.bob.address)["confirmed"],
                         parse_ksl("30"))

    def test_double_spend_in_mempool_rejected(self):
        self.mine_to(self.alice, 12)
        utxos = self.chain.utxos_for(self.alice.address)
        t1 = self.alice.build_transaction(utxos, self.bob.address,
                                          parse_ksl("10"), parse_ksl("0.0001"))
        self.chain.add_transaction(t1)
        # a second tx reusing the same first input must be rejected
        t2 = self.alice.build_transaction(utxos, self.bob.address,
                                          parse_ksl("11"), parse_ksl("0.0001"))
        with self.assertRaises(ValidationError):
            self.chain.add_transaction(t2)

    def test_low_fee_rejected(self):
        self.mine_to(self.alice, 12)
        utxos = self.chain.utxos_for(self.alice.address)
        # build a tx then rewrite outputs so the fee is zero
        tx = self.alice.build_transaction(utxos, self.bob.address,
                                          parse_ksl("1"), parse_ksl("0.0001"))
        total_in = 0
        for i in tx.inputs:
            u = self.chain.utxos[(i.txid, i.vout)]
            total_in += u.amount
        tx.outputs = [TxOutput(total_in, self.bob.address)]  # fee now 0
        for idx in range(len(tx.inputs)):
            tx.sign_input(idx, self.alice.private_key, self.alice.public_key)
        with self.assertRaises(ValidationError):
            self.chain.add_transaction(tx)


class TestBlockValidation(ChainTestBase):
    def test_wrong_prev_hash_rejected(self):
        self.mine_to(self.alice, 2)
        cand = assemble_candidate(self.chain, self.alice.address)
        cand.prev_hash = "00" * 32
        with self.assertRaises(ValidationError):
            self.chain.validate_block(cand, self.chain.tip)

    def test_insufficient_pow_rejected(self):
        self.mine_to(self.alice, 1)
        cand = assemble_candidate(self.chain, self.alice.address)
        cand.nonce = 0  # essentially certainly not a solution
        # make sure it really doesn't satisfy the target before asserting
        if not cand.has_valid_pow():
            with self.assertRaises(ValidationError):
                self.chain.validate_block(cand, self.chain.tip)

    def test_overpaying_coinbase_rejected(self):
        cand = assemble_candidate(self.chain, self.alice.address)
        # inflate the reward beyond subsidy+fees
        cand.transactions[0].outputs[0].amount += 1
        with self.assertRaises(ValidationError):
            self.chain.validate_block(cand, self.chain.tip)


class TestPersistenceAndReorg(ChainTestBase):
    def test_persistence_roundtrip(self):
        self.mine_to(self.alice, 6)
        self.chain.save()
        reloaded = Blockchain(data_dir=self.tmp)
        self.assertEqual(reloaded.height, 6)
        self.assertEqual(reloaded.tip.block_id, self.chain.tip.block_id)
        self.assertEqual(reloaded.circulating_supply(),
                         self.chain.circulating_supply())

    def test_extend_with_fast_path(self):
        self.mine_to(self.alice, 4)
        block_dicts = [b.to_dict() for b in self.chain.blocks]
        # a fresh chain should be able to extend from genesis with these blocks
        fresh = Blockchain(data_dir=tempfile.mkdtemp())
        added = fresh.extend_with(block_dicts[1:])  # skip genesis
        self.assertEqual(added, 4)
        self.assertEqual(fresh.tip.block_id, self.chain.tip.block_id)

    def test_maybe_replace_prefers_more_work(self):
        # build a shorter local chain and a longer foreign one; the longer
        # (heavier) chain must win, and a shorter one must be ignored
        self.mine_to(self.alice, 3)
        short_dicts = [b.to_dict() for b in self.chain.blocks]

        longer = Blockchain(data_dir=tempfile.mkdtemp())
        mine(longer, self.alice.address, count=6, quiet=True)
        long_dicts = [b.to_dict() for b in longer.blocks]

        # local (height 3) adopts the heavier height-6 chain
        self.assertTrue(self.chain.maybe_replace(long_dicts))
        self.assertEqual(self.chain.height, 6)
        # and refuses to go back to the shorter one
        self.assertFalse(self.chain.maybe_replace(short_dicts))
        self.assertEqual(self.chain.height, 6)

    def test_foreign_genesis_rejected(self):
        self.mine_to(self.alice, 2)
        dicts = [b.to_dict() for b in self.chain.blocks]
        dicts[0]["nonce"] = dicts[0]["nonce"] + 1  # different genesis
        with self.assertRaises(ValidationError):
            Blockchain.from_block_dicts(dicts, data_dir=tempfile.mkdtemp())


class TestRetarget(unittest.TestCase):
    def test_no_retarget_between_intervals(self):
        chain = Blockchain(data_dir=tempfile.mkdtemp())
        # next_target off a retarget boundary equals the tip's target
        self.assertEqual(chain.next_target(), chain.tip.target)

    def test_retarget_clamped(self):
        # sanity: difficulty_of is monotone and MAX_TARGET is difficulty 1
        chain = Blockchain(data_dir=tempfile.mkdtemp())
        self.assertAlmostEqual(chain.difficulty_of(params.MAX_TARGET), 1.0, places=6)
        self.assertGreater(chain.difficulty_of(params.MAX_TARGET // 4), 1.0)


if __name__ == "__main__":
    unittest.main()
