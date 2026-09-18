"""How the chain is written down, and what happens when that goes wrong.

The old store rewrote every block on disk every time one arrived — 1.1
seconds and 35MB per block at 50,000 blocks, on a chain that mines 262,800
blocks a year. Blocks are now appended. These cover the things that can go
wrong with that: a half-written line after a power cut, an old install
migrating, a reorg rewriting history, and — most importantly — the
validation marker, which must never let a chain be accepted without being
checked.
"""

import json
import os
import shutil
import tempfile
import unittest

from kestrel import params
from kestrel.blockchain import Blockchain, ValidationError
from kestrel.block import Block
from kestrel.miner import mine
from kestrel.wallet import Wallet


_BLOCKS = None
MINER = None


def setUpModule():
    """One small mined chain, reused — proof-of-work is the slow part."""
    global _BLOCKS, MINER
    MINER = Wallet.create()
    c = Blockchain(data_dir=tempfile.mkdtemp(), autoload=False)
    mine(c, MINER.address, count=6, quiet=True)
    _BLOCKS = [b.to_dict() for b in c.blocks]


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def chain(self, **kw):
        c = Blockchain.from_block_dicts(_BLOCKS, data_dir=self.tmp)
        c.save()
        c._write_mark()
        return c

    def path(self, name):
        return os.path.join(self.tmp, name)


class TestAppendOnly(StorageCase):
    def test_a_new_block_appends_one_line(self):
        c = self.chain()
        before = sum(1 for _ in open(self.path("blocks.jsonl")))
        mine(c, MINER.address, count=1, quiet=True)
        after = sum(1 for _ in open(self.path("blocks.jsonl")))
        self.assertEqual(after, before + 1)
        self.assertEqual(after, c.height + 1)

    def test_reopening_gives_back_the_same_chain(self):
        c = self.chain()
        again = Blockchain(data_dir=self.tmp)
        self.assertEqual(again.height, c.height)
        self.assertEqual(again.tip.block_id, c.tip.block_id)
        self.assertEqual(again.circulating_supply(), c.circulating_supply())

    def test_the_mempool_travels_separately(self):
        c = self.chain()
        self.assertTrue(os.path.exists(self.path("mempool.json")))
        self.assertTrue(os.path.exists(self.path("blocks.jsonl")))


class TestDamage(StorageCase):
    def test_a_torn_final_line_costs_only_that_block(self):
        """A power cut mid-append must not take the chain with it."""
        c = self.chain()
        with open(self.path("blocks.jsonl"), "a") as f:
            f.write('{"height": 99, "prev_hash": "aa')     # cut off
        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.height, c.height)
        self.assertEqual(back.tip.block_id, c.tip.block_id)

    def test_a_damaged_middle_line_is_not_silently_accepted(self):
        c = self.chain()
        lines = open(self.path("blocks.jsonl")).read().splitlines()
        lines[2] = '{"height": 2, "nonsense": true}'
        with open(self.path("blocks.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        back = Blockchain(data_dir=self.tmp)
        # it must not carry on as though nothing happened
        self.assertLess(back.height, c.height)

    def test_one_bad_block_costs_the_tail_not_the_whole_chain(self):
        """The expensive mistake would be re-downloading 300,000 blocks."""
        c = self.chain()
        lines = open(self.path("blocks.jsonl")).read().splitlines()
        bad = json.loads(lines[4])
        bad["nonce"] = bad["nonce"] + 1              # proof of work no longer holds
        lines[4] = json.dumps(bad)
        with open(self.path("blocks.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")

        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.height, 3)             # everything before it kept
        self.assertEqual(back.tip.block_id, c.blocks[3].block_id)
        # and the store was tidied, so it doesn't happen again every start
        self.assertEqual(sum(1 for _ in open(self.path("blocks.jsonl"))), 4)
        self.assertEqual(Blockchain(data_dir=self.tmp).height, 3)

    def test_the_dropped_blocks_are_set_aside(self):
        c = self.chain()
        lines = open(self.path("blocks.jsonl")).read().splitlines()
        bad = json.loads(lines[4]); bad["nonce"] += 1
        lines[4] = json.dumps(bad)
        with open(self.path("blocks.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        Blockchain(data_dir=self.tmp)
        kept = self.path("blocks.rejected.jsonl")
        self.assertTrue(os.path.exists(kept))
        self.assertEqual(sum(1 for _ in open(kept)), len(_BLOCKS) - 4)

    def test_a_truncated_chain_can_still_grow(self):
        """After a recovery the append cursor must match what's on disk."""
        c = self.chain()
        lines = open(self.path("blocks.jsonl")).read().splitlines()
        bad = json.loads(lines[4]); bad["nonce"] += 1
        lines[4] = json.dumps(bad)
        with open(self.path("blocks.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")

        back = Blockchain(data_dir=self.tmp)
        mine(back, MINER.address, count=2, quiet=True)
        self.assertEqual(sum(1 for _ in open(self.path("blocks.jsonl"))), 6)
        self.assertEqual(Blockchain(data_dir=self.tmp).tip.block_id,
                         back.tip.block_id)

    def test_an_empty_store_starts_from_genesis(self):
        open(self.path("blocks.jsonl"), "w").close()
        c = Blockchain(data_dir=self.tmp)
        self.assertEqual(c.height, 0)

    def test_an_unusable_store_is_replaced_not_appended_to(self):
        """The worst outcome is two chains spliced into one file."""
        with open(self.path("blocks.jsonl"), "w") as f:
            f.write("this is not a block at all\n")
        c = Blockchain(data_dir=self.tmp)
        self.assertEqual(c.height, 0)
        mine(c, MINER.address, count=2, quiet=True)
        self.assertEqual(sum(1 for _ in open(self.path("blocks.jsonl"))), 3)
        self.assertEqual(Blockchain(data_dir=self.tmp).height, 2)


class TestMigration(StorageCase):
    def legacy(self, blocks=None):
        json.dump({"magic": params.NETWORK_MAGIC,
                   "blocks": blocks if blocks is not None else _BLOCKS,
                   "mempool": [], "mempool_seen": {}, "mempool_dropped": {}},
                  open(self.path("chain.json"), "w"))

    def test_an_old_install_is_carried_across(self):
        self.legacy()
        c = Blockchain(data_dir=self.tmp)
        self.assertEqual(c.height, len(_BLOCKS) - 1)
        self.assertTrue(os.path.exists(self.path("blocks.jsonl")))

    def test_the_old_file_is_kept_not_deleted(self):
        """It is the only copy of the chain until the new one is proven."""
        self.legacy()
        Blockchain(data_dir=self.tmp)
        self.assertFalse(os.path.exists(self.path("chain.json")))
        self.assertTrue(os.path.exists(self.path("chain.json.pre-1.4.8")))

    def test_a_foreign_network_file_is_refused(self):
        json.dump({"magic": "some-other-coin", "blocks": _BLOCKS},
                  open(self.path("chain.json"), "w"))
        self.assertEqual(Blockchain(data_dir=self.tmp).height, 0)


class TestValidationMark(StorageCase):
    """The marker skips re-verification, so it must be impossible to fool."""

    def test_a_marked_chain_loads_to_the_same_place(self):
        c = self.chain()
        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.height, c.height)
        self.assertEqual(back.tip.block_id, c.tip.block_id)
        self.assertEqual(sorted(map(str, back.utxos)),
                         sorted(map(str, c.utxos)))

    def test_a_mark_naming_a_different_tip_is_ignored(self):
        c = self.chain()
        json.dump({"magic": params.NETWORK_MAGIC, "height": c.height,
                   "tip": "0" * 64}, open(self.path("validated.json"), "w"))
        back = Blockchain(data_dir=self.tmp)      # falls back to full check
        self.assertEqual(back.height, c.height)
        self.assertEqual(back.tip.block_id, c.tip.block_id)

    def test_a_mark_past_the_end_is_ignored(self):
        c = self.chain()
        json.dump({"magic": params.NETWORK_MAGIC, "height": c.height + 500,
                   "tip": c.tip.block_id},
                  open(self.path("validated.json"), "w"))
        self.assertEqual(Blockchain(data_dir=self.tmp).height, c.height)

    def test_a_mark_cannot_smuggle_in_a_tampered_block(self):
        """The fast path still checks the chain hangs together."""
        c = self.chain()
        lines = open(self.path("blocks.jsonl")).read().splitlines()
        bad = json.loads(lines[3])
        bad["transactions"][0]["outputs"][0]["amount"] = 999_000_000_000
        lines[3] = json.dumps(bad)
        with open(self.path("blocks.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        back = Blockchain(data_dir=self.tmp)
        # the merkle root no longer matches, so it is refused either way
        self.assertLess(back.height, c.height)
        self.assertNotEqual(back.circulating_supply(), 999_000_000_000)

    def test_a_garbage_mark_is_survivable(self):
        c = self.chain()
        open(self.path("validated.json"), "w").write("{not json")
        self.assertEqual(Blockchain(data_dir=self.tmp).height, c.height)

    def test_a_foreign_mark_is_ignored(self):
        c = self.chain()
        json.dump({"magic": "another-chain", "height": 2,
                   "tip": c.blocks[2].block_id},
                  open(self.path("validated.json"), "w"))
        self.assertEqual(Blockchain(data_dir=self.tmp).height, c.height)


class TestDiskTrouble(StorageCase):
    """A disk that won't take a write must not stop the chain."""

    def test_a_failing_write_does_not_reject_a_good_block(self):
        c = self.chain()
        def boom(*a, **kw):
            raise OSError(28, "No space left on device")
        c._save_blocks = boom
        mine(c, MINER.address, count=1, quiet=True)   # must not raise
        self.assertEqual(c.height, len(_BLOCKS))      # and it counted

    def test_the_next_write_repairs_the_file(self):
        c = self.chain()
        real = c._save_blocks
        c._save_blocks = lambda *a, **kw: (_ for _ in ()).throw(
            OSError(28, "No space left on device"))
        mine(c, MINER.address, count=1, quiet=True)
        c._save_blocks = real
        mine(c, MINER.address, count=1, quiet=True)   # disk is back

        lines = sum(1 for _ in open(self.path("blocks.jsonl")))
        self.assertEqual(lines, c.height + 1)         # the skipped one too
        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.tip.block_id, c.tip.block_id)


class TestReorgOnDisk(StorageCase):
    def test_a_reorg_rewrites_the_file_rather_than_appending(self):
        c = self.chain()
        fork = Blockchain.from_block_dicts(_BLOCKS[:4], data_dir=self.tmp)
        mine(fork, Wallet.create().address, count=5, quiet=True)
        self.assertTrue(c.maybe_replace([b.to_dict() for b in fork.blocks]))

        lines = sum(1 for _ in open(self.path("blocks.jsonl")))
        self.assertEqual(lines, c.height + 1)          # no stale tail
        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.tip.block_id, c.tip.block_id)
        self.assertEqual(back.height, c.height)


class TestSyncThenRestart(StorageCase):
    """Catching up from a peer, then reopening the app.

    The path that broke: a chain loaded via the validation mark is a
    scratch object whose append cursor says it has never written
    anything. Letting it save wrote the whole chain out a SECOND time and
    emptied the stored mempool on the way past, so a pending payment
    vanished and the file doubled. Any node that ever syncs in bulk and
    then restarts goes through here, which is all of them.
    """

    def synced(self):
        c = self.chain()
        donor = Blockchain.from_block_dicts(_BLOCKS, data_dir=tempfile.mkdtemp())
        mine(donor, MINER.address, count=4, quiet=True)
        added = c.extend_with([b.to_dict() for b in donor.blocks[len(_BLOCKS):]])
        self.assertEqual(added, 4)
        return c

    def test_a_bulk_sync_writes_each_block_once(self):
        c = self.synced()
        lines = sum(1 for _ in open(self.path("blocks.jsonl")))
        self.assertEqual(lines, c.height + 1)

    def test_a_restart_after_syncing_keeps_the_chain_and_the_mempool(self):
        c = self.chain()
        # a payment waiting on disk before the sync happens
        MINER2 = Wallet.create()
        utxo = c.utxos_for(MINER.address, spendable_only=True)
        if utxo:
            tx = MINER.build_transaction(utxo[:1], MINER2.address,
                                         params.COIN, params.MIN_RELAY_FEE)
            c.add_transaction(tx)
            c.save()
        pending = set(c.mempool)

        donor = Blockchain.from_block_dicts(_BLOCKS, data_dir=tempfile.mkdtemp())
        mine(donor, MINER.address, count=4, quiet=True)
        c.extend_with([b.to_dict() for b in donor.blocks[len(_BLOCKS):]])

        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.height, c.height)
        self.assertEqual(back.tip.block_id, c.tip.block_id)
        self.assertEqual(set(back.mempool), pending)
        self.assertEqual(sum(1 for _ in open(self.path("blocks.jsonl"))),
                         c.height + 1)

    def test_a_block_mined_after_that_restart_still_lands(self):
        """The doubled file also silently swallowed the next block."""
        c = self.synced()
        back = Blockchain(data_dir=self.tmp)
        mine(back, MINER.address, count=1, quiet=True)
        again = Blockchain(data_dir=self.tmp)
        self.assertEqual(again.height, back.height)
        self.assertEqual(again.tip.block_id, back.tip.block_id)

    def test_syncing_moves_the_validation_mark_forward(self):
        """Or every start re-verifies everything gained since the last block."""
        c = self.synced()
        mark = json.load(open(self.path("validated.json")))
        self.assertEqual(mark["height"], c.height)
        self.assertEqual(mark["tip"], c.tip.block_id)


class TestReorgDiskFailure(StorageCase):
    def test_a_failed_rewrite_does_not_splice_two_chains(self):
        c = self.chain()
        fork = Blockchain.from_block_dicts(_BLOCKS[:4], data_dir=tempfile.mkdtemp())
        mine(fork, Wallet.create().address, count=5, quiet=True)

        real = c._rewrite_blocks
        c._rewrite_blocks = lambda *a, **k: (_ for _ in ()).throw(
            OSError(28, "No space left on device"))
        # the reorg is real in memory whatever the disk says, so this must
        # report success rather than throwing at a caller that treats any
        # exception as "chain rejected"
        self.assertTrue(c.maybe_replace([b.to_dict() for b in fork.blocks]))
        c._rewrite_blocks = real

        mine(c, MINER.address, count=1, quiet=True)      # disk comes back
        back = Blockchain(data_dir=self.tmp)
        self.assertEqual(back.height, c.height)
        self.assertEqual(back.tip.block_id, c.tip.block_id)
        self.assertEqual(sum(1 for _ in open(self.path("blocks.jsonl"))),
                         c.height + 1)


if __name__ == "__main__":
    unittest.main()
