"""
Fork healing when the heavier chain sits behind NAT.

This reproduces the real-world desync: a miner at home mines fast on its
own chain while an anchor node on a VPS sits on a different one. Syncing
is pull-based, so the anchor tries to fetch the miner's chain — and
cannot, because the miner is behind NAT and accepts no inbound
connections. Before the fix both sides mined in parallel forever and
never converged.

NAT is simulated exactly: the miner's node is never served, so nothing can
dial it. Only its outbound calls work, which is the whole point.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

os.environ["KESTREL_DHT"] = "0"
os.environ["KESTREL_SHARE_LOCAL"] = "1"

from kestrel.blockchain import Blockchain
from kestrel.wallet import Wallet
from kestrel.node import Node
from kestrel.miner import mine


def _get(url, t=10):
    with urllib.request.urlopen(url, timeout=t) as r:
        return json.loads(r.read())


def _quiet(node):
    """Silence auto-discovery so the test controls who knows whom."""
    for svc in (node.discovery, node.rendezvous):
        try:
            svc.stop()
        except Exception:
            pass
    node.peers.clear()
    node.peer_info.clear()
    node.seeds.clear()


class ForkHealsWithoutInboundAccess(unittest.TestCase):
    ANCHOR_PORT = 4491

    @classmethod
    def setUpClass(cls):
        cls.dirs = [tempfile.mkdtemp(), tempfile.mkdtemp()]
        w = Wallet.create()

        # Two chains diverging from genesis. The miner is strictly heavier.
        cls.anchor_chain = Blockchain(data_dir=cls.dirs[0])
        mine(cls.anchor_chain, w.address, count=3, quiet=True)
        cls.miner_chain = Blockchain(data_dir=cls.dirs[1])
        mine(cls.miner_chain, w.address, count=9, quiet=True)

        # The anchor is a real reachable node (the VPS).
        cls.anchor = Node(cls.anchor_chain, host="127.0.0.1",
                          port=cls.ANCHOR_PORT)
        _quiet(cls.anchor)
        threading.Thread(target=cls.anchor.serve_forever, daemon=True).start()

        # The miner is NEVER served — nothing in the world can dial it.
        # That is precisely what being behind NAT means.
        cls.miner = Node(cls.miner_chain, host="127.0.0.1", port=4492)
        _quiet(cls.miner)
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        for n in (cls.anchor, cls.miner):
            try:
                n.stop()
            except Exception:
                pass
        for d in cls.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _anchor(self):
        return _get(f"http://127.0.0.1:{self.ANCHOR_PORT}/info")

    def test_01_chains_start_forked(self):
        a = self._anchor()
        self.assertEqual(a["height"], 3)
        self.assertEqual(self.miner_chain.height, 9)
        self.assertNotEqual(a["tip"], self.miner_chain.tip.block_id)
        self.assertGreater(self.miner_chain.total_work(), a["total_work"])

    def test_02_anchor_cannot_pull_from_a_natted_miner(self):
        # The anchor has no way to reach the miner, so pull-based sync —
        # the only mechanism that existed before — cannot converge.
        self.assertNotIn("http://127.0.0.1:4492", self.anchor.peers,
                         "anchor must not be able to dial the miner")
        self.anchor.sync_once()
        time.sleep(1)
        self.assertEqual(self._anchor()["height"], 3,
                         "anchor should still be stuck on its own fork")

    def test_03_miner_heals_the_fork_by_pushing(self):
        # The miner dials out to its seed, exactly as a home miner does.
        self.miner.add_peers([f"http://127.0.0.1:{self.ANCHOR_PORT}"])
        self.miner.gossip_block(self.miner_chain.tip)

        deadline = time.time() + 25
        while time.time() < deadline:
            if self._anchor()["height"] == 9:
                break
            time.sleep(0.5)

        a = self._anchor()
        self.assertEqual(a["height"], 9,
                         "anchor never adopted the heavier pushed chain")
        self.assertEqual(a["tip"], self.miner_chain.tip.block_id,
                         "chains did not converge")

    def test_04_a_lighter_chain_cannot_be_pushed_over_a_heavier_one(self):
        # Now the anchor is heavier. A push from the lighter miner must be
        # refused — otherwise "push" would be a way to rewrite history.
        w = Wallet.create()
        mine(self.anchor_chain, w.address, count=4, quiet=True)
        heavy_tip = self.anchor_chain.tip.block_id
        heavy_height = self.anchor_chain.height

        self.miner.gossip_block(self.miner_chain.tip)
        time.sleep(3)

        a = self._anchor()
        self.assertEqual(a["height"], heavy_height,
                         "a lighter pushed chain must be refused")
        self.assertEqual(a["tip"], heavy_tip)

    def test_05_malformed_push_is_rejected(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.ANCHOR_PORT}/chain",
            data=json.dumps({"blocks": "not-a-list"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected a 400 for a non-list payload")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == "__main__":
    unittest.main()
