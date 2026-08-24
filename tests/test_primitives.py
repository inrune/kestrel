"""Transactions, blocks, wallet coin-selection and amount parsing."""

import unittest

from kestrel import params
from kestrel.crypto_utils import generate_private_key, private_to_public, pubkey_to_address
from kestrel.transaction import Transaction, TxInput, TxOutput, COINBASE_TXID, COINBASE_VOUT
from kestrel.block import Block, build_genesis, merkle_root
from kestrel.wallet import Wallet, format_ksl, parse_ksl
from kestrel.blockchain import ValidationError


def _addr():
    return pubkey_to_address(private_to_public(generate_private_key()))


class TestTransaction(unittest.TestCase):
    def test_txid_is_deterministic(self):
        tx = Transaction([TxInput("aa" * 32, 0)], [TxOutput(100, _addr())], timestamp=1)
        self.assertEqual(tx.txid, Transaction.from_dict(tx.to_dict()).txid)

    def test_sighash_ignores_signatures_and_pubkeys(self):
        a = _addr()
        tx = Transaction([TxInput("bb" * 32, 1)], [TxOutput(50, a)], timestamp=7)
        before = tx.sighash()
        tx.inputs[0].pubkey = "deadbeef"
        tx.inputs[0].signature = "cafe"
        self.assertEqual(tx.sighash(), before)   # sighash unchanged
        # but the txid DOES change once a signature is attached
        self.assertNotEqual(
            Transaction([TxInput("bb" * 32, 1)], [TxOutput(50, a)], timestamp=7).txid,
            tx.txid,
        )

    def test_coinbase_detection(self):
        cb = Transaction.coinbase(5, params.INITIAL_REWARD, _addr())
        self.assertTrue(cb.is_coinbase)
        normal = Transaction([TxInput("cc" * 32, 0)], [TxOutput(1, _addr())])
        self.assertFalse(normal.is_coinbase)

    def test_basic_check_rejections(self):
        a = _addr()
        self.assertFalse(Transaction([], [TxOutput(1, a)]).basic_check()[0])
        self.assertFalse(Transaction([TxInput("dd" * 32, 0)], []).basic_check()[0])
        # duplicate input outpoint
        dup = Transaction([TxInput("ee" * 32, 0), TxInput("ee" * 32, 0)],
                          [TxOutput(1, a)])
        self.assertFalse(dup.basic_check()[0])
        # non-positive amount
        self.assertFalse(
            Transaction([TxInput("ff" * 32, 0)], [TxOutput(0, a)]).basic_check()[0])
        # invalid address
        self.assertFalse(
            Transaction([TxInput("ab" * 32, 0)], [TxOutput(1, "notanaddress")]).basic_check()[0])
        # over the supply cap
        self.assertFalse(
            Transaction([TxInput("ac" * 32, 0)],
                        [TxOutput(params.MAX_SUPPLY + 1, a)]).basic_check()[0])

    def test_signing_is_order_independent(self):
        # sign two inputs in both orders — same signatures, since the
        # sighash commits to outpoints/outputs but not to pubkeys/sigs
        priv = generate_private_key()
        pub = private_to_public(priv)
        outs = [TxOutput(10, _addr())]
        t1 = Transaction([TxInput("a1" * 32, 0), TxInput("a2" * 32, 1)], outs, timestamp=3)
        t2 = Transaction([TxInput("a1" * 32, 0), TxInput("a2" * 32, 1)], outs, timestamp=3)
        t1.sign_input(0, priv, pub); t1.sign_input(1, priv, pub)
        t2.sign_input(1, priv, pub); t2.sign_input(0, priv, pub)
        self.assertEqual(t1.inputs[0].signature, t2.inputs[0].signature)
        self.assertEqual(t1.inputs[1].signature, t2.inputs[1].signature)


class TestBlock(unittest.TestCase):
    def test_merkle_root_stability(self):
        ids = [c * 64 for c in "12345"]
        self.assertEqual(merkle_root(ids), merkle_root(ids))
        self.assertEqual(merkle_root([]), "0" * 64)

    def test_genesis_is_deterministic_and_valid(self):
        g1, g2 = build_genesis(), build_genesis()
        self.assertEqual(g1.block_id, g2.block_id)
        self.assertTrue(g1.has_valid_pow())
        # matches the id committed in params
        self.assertEqual(
            g1.block_id,
            "c8f460e1f38bd483ced56c037400108032a28e33746da71efeddf698735036f1",
        )

    def test_block_roundtrip(self):
        g = build_genesis()
        self.assertEqual(Block.from_dict(g.to_dict()).block_id, g.block_id)

    def test_work_is_positive(self):
        self.assertGreater(build_genesis().work, 0)


class TestAmounts(unittest.TestCase):
    def test_parse_roundtrip(self):
        for s, feathers in [("12.5", 1_250_000_000), ("0.00000001", 1),
                            ("1,000.5", 100_050_000_000), ("44000000", params.MAX_SUPPLY),
                            ("0", 0), ("7 KSL", 700_000_000)]:
            self.assertEqual(parse_ksl(s), feathers)

    def test_parse_rejects_bad_input(self):
        for bad in ["-5", "1.234567890", "abc", "", ".", "1.5.5", "0x10"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_ksl(bad)

    def test_format(self):
        self.assertEqual(format_ksl(0), "0.00000000 KSL")
        self.assertEqual(format_ksl(1), "0.00000001 KSL")
        self.assertEqual(format_ksl(25 * params.COIN), "25.00000000 KSL")
        self.assertEqual(format_ksl(-1), "-0.00000001 KSL")


class TestWalletBuilding(unittest.TestCase):
    def setUp(self):
        self.w = Wallet.create()
        self.utxos = [
            {"txid": "aa" * 32, "vout": 0, "amount": 5 * params.COIN},
            {"txid": "bb" * 32, "vout": 1, "amount": 3 * params.COIN},
        ]

    def test_change_output_created(self):
        tx = self.w.build_transaction(self.utxos, _addr(), 2 * params.COIN,
                                      params.MIN_RELAY_FEE)
        # one payment + one change back to self
        self.assertEqual(len(tx.outputs), 2)
        self.assertTrue(any(o.address == self.w.address for o in tx.outputs))
        # value conserved: inputs = outputs + fee (greedy picked the 5-KSL utxo)
        self.assertEqual(5 * params.COIN,
                         tx.total_output + params.MIN_RELAY_FEE)

    def test_no_change_when_exact(self):
        tx = self.w.build_transaction(
            [{"txid": "cc" * 32, "vout": 0, "amount": 2 * params.COIN + params.MIN_RELAY_FEE}],
            _addr(), 2 * params.COIN, params.MIN_RELAY_FEE)
        self.assertEqual(len(tx.outputs), 1)

    def test_insufficient_funds_raises(self):
        with self.assertRaises(ValidationError):
            self.w.build_transaction(self.utxos, _addr(), 100 * params.COIN,
                                     params.MIN_RELAY_FEE)

    def test_fee_below_minimum_raises(self):
        with self.assertRaises(ValidationError):
            self.w.build_transaction(self.utxos, _addr(), params.COIN, 0)

    def test_signatures_verify_against_utxo_addresses(self):
        # a fully-signed tx should verify each input against the paying address
        tx = self.w.build_transaction(self.utxos, _addr(), 2 * params.COIN,
                                      params.MIN_RELAY_FEE)
        for i in range(len(tx.inputs)):
            self.assertTrue(tx.verify_input_signature(i, self.w.address))


if __name__ == "__main__":
    unittest.main()
