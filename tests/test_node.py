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
