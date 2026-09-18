"""
Networking integration test — real HTTP nodes on loopback.

Spins up two full nodes, connects them, mines through the JSON API, and
checks that blocks propagate, peers are exchanged, and (the regression
this guards) the node's API stays responsive while it mines instead of
freezing on the chain lock for the whole proof-of-work grind.

DHT is disabled and loopback sharing enabled so the test never touches
the public internet.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request

os.environ["KESTREL_DHT"] = "0"          # no real internet during tests
os.environ["KESTREL_SHARE_LOCAL"] = "1"  # allow loopback peers on one host

from kestrel.blockchain import Blockchain
from kestrel.wallet import Wallet
from kestrel.node import Node
from kestrel import params
from kestrel.miner import mine


def _get(url, t=5):
    with urllib.request.urlopen(url, timeout=t) as r:
        return json.loads(r.read())


def _post(url, payload, t=600):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read())


class TwoNodeNetwork(unittest.TestCase):
    PORT1 = 4481
    PORT2 = 4482

    @classmethod
    def setUpClass(cls):
        cls.dirs = [tempfile.mkdtemp(), tempfile.mkdtemp()]
        cls.n1 = Node(Blockchain(data_dir=cls.dirs[0]), host="127.0.0.1", port=cls.PORT1)
        cls.n2 = Node(Blockchain(data_dir=cls.dirs[1]), host="127.0.0.1", port=cls.PORT2)
        for n in (cls.n1, cls.n2):
            threading.Thread(target=n.serve_forever, daemon=True).start()
        time.sleep(1.2)

    @classmethod
    def tearDownClass(cls):
        for n in (cls.n1, cls.n2):
            try:
                n.stop()
            except Exception:
                pass
        for d in cls.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _base(self, port):
        return f"http://127.0.0.1:{port}"

    def test_01_nodes_are_up(self):
        for p in (self.PORT1, self.PORT2):
            info = _get(self._base(p) + "/info")
            self.assertEqual(info["magic"], params.NETWORK_MAGIC)
            self.assertEqual(info["height"], 0)

    def test_02_dashboard_served_to_browsers(self):
        req = urllib.request.Request(self._base(self.PORT1) + "/",
                                     headers={"Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=5) as r:
            html = r.read().decode()
        self.assertIn("<html", html.lower())
        self.assertIn("Kestrel", html)

    def test_03_mine_is_loopback_only(self):
        # loopback is allowed here, so this should succeed and set the tip
        w = Wallet.create()
        out = _post(self._base(self.PORT1) + "/mine",
                    {"address": w.address, "count": 3, "threads": 2})
        self.assertEqual(len(out["mined"]), 3)
        self.assertEqual(_get(self._base(self.PORT1) + "/info")["height"], 3)

    def test_04_api_responsive_while_mining(self):
        # Probe /info latency while mining more blocks. The old code held
        # the chain lock across the whole grind, so /info would block for
        # seconds; now it must stay well under a second.
        w = Wallet.create()
        latencies = []
        stop = threading.Event()

        def probe():
            while not stop.is_set():
                t0 = time.time()
                try:
                    _get(self._base(self.PORT1) + "/info", t=5)
                    latencies.append(time.time() - t0)
                except Exception:
                    latencies.append(99.0)
                time.sleep(0.2)

        pt = threading.Thread(target=probe, daemon=True)
        pt.start()
        _post(self._base(self.PORT1) + "/mine",
              {"address": w.address, "count": 3, "threads": 2})
        stop.set()
        time.sleep(0.3)
        self.assertTrue(latencies, "no latency samples collected")
        self.assertLess(max(latencies), 2.0,
                        f"API froze while mining (worst {max(latencies):.1f}s)")

    def test_05_peer_connect_and_sync(self):
        # point node2 at node1; it should learn the peer and catch up
        _post(self._base(self.PORT2) + "/peers/add",
              {"url": self._base(self.PORT1)})
        h1 = _get(self._base(self.PORT1) + "/info")["height"]
        deadline = time.time() + 30
        while time.time() < deadline:
            if _get(self._base(self.PORT2) + "/info")["height"] >= h1:
                break
            time.sleep(0.5)
        self.assertEqual(_get(self._base(self.PORT2) + "/info")["height"], h1)

    def test_06_peer_exchange_mutual(self):
        # after syncing, each node should list the other as an alive peer
        deadline = time.time() + 10
        while time.time() < deadline:
            a1 = _get(self._base(self.PORT1) + "/peers")["alive"]
            a2 = _get(self._base(self.PORT2) + "/peers")["alive"]
            if a1 and a2:
                break
            time.sleep(0.5)
        self.assertTrue(any(str(self.PORT2) in u for u in a1),
                        f"node1 doesn't see node2 alive: {a1}")
        self.assertTrue(any(str(self.PORT1) in u for u in a2),
                        f"node2 doesn't see node1 alive: {a2}")

    def test_07_block_gossip_after_connect(self):
        # mine one more on node1; node2 should receive it via gossip
        before = _get(self._base(self.PORT2) + "/info")["height"]
        w = Wallet.create()
        _post(self._base(self.PORT1) + "/mine",
              {"address": w.address, "count": 1, "threads": 2})
        target = before + 1
        deadline = time.time() + 20
        while time.time() < deadline:
            if _get(self._base(self.PORT2) + "/info")["height"] >= target:
                break
            time.sleep(0.5)
        self.assertGreaterEqual(_get(self._base(self.PORT2) + "/info")["height"],
                                target)


if __name__ == "__main__":
    unittest.main()


class TestAddressIndex(unittest.TestCase):
    """The address view: one request, one consistent moment, and cheap.

    It used to walk every block in the chain on every call, holding the
    node lock while it did — so a wallet asking for its balance every few
    seconds got slower with every block mined. It also answered only about
    confirmed history, leaving the wallet to ask separately what was still
    in flight; two questions asked at two different moments can disagree,
    and a payment could appear in both answers or in neither.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.chain = Blockchain(data_dir=self.tmp, autoload=False)
        self.alice, self.bob = Wallet.create(), Wallet.create()
        mine(self.chain, self.alice.address, count=2, quiet=True)
        mine(self.chain, Wallet.create().address,
             count=params.COINBASE_MATURITY, quiet=True)
        self.node = Node(self.chain, host="127.0.0.1",
                         port=params.DEFAULT_PORT + 70)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def view(self, addr):
        with self.node.lock:
            self.node._reindex()
            return self.node._address_view(addr)

    def test_mined_coins_appear_in_history(self):
        v = self.view(self.alice.address)
        self.assertTrue(v["valid"])
        self.assertEqual(v["tx_count"], 2)
        self.assertTrue(all(h["coinbase"] for h in v["history"]))
        self.assertEqual(v["confirmed"], 2 * params.INITIAL_REWARD)

    def test_pending_comes_back_in_the_same_answer(self):
        utxos = self.chain.utxos_for(self.alice.address, spendable_only=True)
        tx = self.alice.build_transaction(utxos, self.bob.address,
                                          params.COIN, params.MIN_RELAY_FEE)
        self.chain.add_transaction(tx)

        v = self.view(self.bob.address)
        self.assertEqual(len(v["pending"]), 1)
        self.assertEqual(v["pending"][0]["delta"], params.COIN)
        self.assertFalse(v["pending"][0]["outgoing"])
        self.assertEqual(v["pending_in"], params.COIN)
        self.assertEqual(v["confirmed"], 0)          # not a block yet
        self.assertIn("age_seconds", v["pending"][0])
        self.assertIn("expires_in", v["pending"][0])

        # and the sender sees it as going out, in the same one call
        s = self.view(self.alice.address)
        self.assertTrue(s["pending"][0]["outgoing"])
        self.assertGreater(s["pending_out"], 0)

    def test_pending_becomes_history_once_mined(self):
        utxos = self.chain.utxos_for(self.alice.address, spendable_only=True)
        tx = self.alice.build_transaction(utxos, self.bob.address,
                                          params.COIN, params.MIN_RELAY_FEE)
        self.chain.add_transaction(tx)
        mine(self.chain, self.alice.address, count=1, quiet=True)

        v = self.view(self.bob.address)
        self.assertEqual(v["pending"], [])
        self.assertEqual(v["confirmed"], params.COIN)
        self.assertEqual(v["history"][0]["txid"], tx.txid)
        self.assertEqual(v["history"][0]["confirmations"], 1)

    def test_index_extends_instead_of_rebuilding(self):
        """The whole point: a new block must not re-walk the whole chain."""
        with self.node.lock:
            self.node._reindex()
        first = self.node._tx_index
        mine(self.chain, self.alice.address, count=1, quiet=True)
        with self.node.lock:
            self.node._reindex()
        self.assertIs(self.node._tx_index, first)     # same dict, extended
        self.assertEqual(self.node._index_at, self.chain.height)
        self.assertEqual(self.node._index_tip, self.chain.tip.block_id)

    def test_index_is_rebuilt_after_a_reorg(self):
        blocks = [b.to_dict() for b in self.chain.blocks]
        mine(self.chain, self.alice.address, count=1, quiet=True)
        with self.node.lock:
            self.node._reindex()
        stale_tip = self.node._index_tip

        rival = Blockchain.from_block_dicts(blocks, data_dir=self.tmp)
        mine(rival, self.bob.address, count=2, quiet=True)
        self.assertTrue(self.chain.maybe_replace(
            [b.to_dict() for b in rival.blocks]))
        with self.node.lock:
            self.node._reindex()

        self.assertNotEqual(self.node._index_tip, stale_tip)
        self.assertEqual(self.node._index_tip, self.chain.tip.block_id)
        # history must reflect the chain that actually won
        self.assertEqual(self.view(self.bob.address)["tx_count"], 2)

    def test_an_unknown_address_is_answered_not_crashed(self):
        v = self.view("not-an-address")
        self.assertFalse(v["valid"])
        self.assertIn("error", v)
