"""v1.4.9: the things that should never have been able to go wrong.

Hostile or broken input from the network, damaged files on disk, an
update that could delete the wrong file, a peer that lies about its
height, a log that grows forever. Each test is one way the previous
version could be knocked over, or could quietly do the wrong thing.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile

os.environ["KESTREL_DHT"] = "0"
os.environ["KESTREL_SHARE_LOCAL"] = "1"

import kestrel
from kestrel import params, logfile, updates
from kestrel.blockchain import Blockchain
from kestrel.discovery import LanDiscovery, PACKET_PREFIX
from kestrel.miner import mine
from kestrel.node import Node, strict_loads
from kestrel.wallet import Wallet, parse_ksl

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _quiet(node):
    for svc in (node.discovery, node.rendezvous):
        try:
            svc.stop()
        except Exception:
            pass
    node.peers.clear()
    node.peer_info.clear()
    node.seeds.clear()


def _serve(chain):
    port = _free_port()
    node = Node(chain, host="127.0.0.1", port=port)
    _quiet(node)
    threading.Thread(target=node.serve_forever, daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            _get(f"http://127.0.0.1:{port}/info")
            break
        except Exception:
            time.sleep(0.1)
    return node, f"http://127.0.0.1:{port}"


def _get(url, t=10):
    with urllib.request.urlopen(url, timeout=t) as r:
        return json.loads(r.read())


def _post_raw(url, body: bytes, t=10, headers=None):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=headers or
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=t) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


# ================================================================ network

class StrictJson(unittest.TestCase):
    def test_nan_and_infinity_are_not_json(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '[-Infinity]'):
            with self.assertRaises(ValueError):
                strict_loads(text)

    def test_absurd_nesting_is_a_value_error(self):
        with self.assertRaises(ValueError):
            strict_loads("[" * 200_000 + "]" * 200_000)

    def test_ordinary_json_still_parses(self):
        self.assertEqual(strict_loads(b'{"a": [1, 2.5, "x"]}'),
                         {"a": [1, 2.5, "x"]})


class HostileRequests(unittest.TestCase):
    """Every one of these used to be either a dropped connection, a log
    line with a traceback, or — for Infinity — an exception class the
    handler was not expecting. Each must now be an ordinary 4xx."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.chain = Blockchain(data_dir=cls.tmp)
        cls.node, cls.url = _serve(cls.chain)

    @classmethod
    def tearDownClass(cls):
        cls.node.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_infinity_in_a_transaction_is_a_400(self):
        body = (b'{"tx": {"version": 1, "timestamp": Infinity, '
                b'"inputs": [], "outputs": []}}')
        code, reply = _post_raw(self.url + "/tx", body)
        self.assertEqual(code, 400)

    def test_infinity_in_a_block_is_a_400(self):
        body = (b'{"block": {"height": 1, "prev_hash": "00", '
                b'"timestamp": 1e999, "target": "ff", "nonce": 0, '
                b'"transactions": []}}')
        code, _ = _post_raw(self.url + "/block", body)
        self.assertEqual(code, 400)

    def test_a_body_that_is_not_an_object_is_a_400(self):
        for body in (b"[]", b"7", b'"hello"', b"null"):
            code, _ = _post_raw(self.url + "/tx", body)
            self.assertEqual(code, 400, body)

    def test_deep_nesting_is_a_400(self):
        body = b'{"tx": ' + b"[" * 50_000 + b"]" * 50_000 + b"}"
        code, _ = _post_raw(self.url + "/tx", body)
        self.assertEqual(code, 400)

    def test_an_oversized_body_is_refused_not_truncated(self):
        s = socket.create_connection(("127.0.0.1", self.node.port), 5)
        s.sendall(b"POST /tx HTTP/1.1\r\nHost: x\r\n"
                  b"Content-Type: application/json\r\n"
                  b"Content-Length: 900000000\r\n\r\n{")
        reply = s.recv(4096).decode("latin-1")
        s.close()
        self.assertIn(" 413 ", reply.splitlines()[0])

    def test_a_bad_announce_port_is_a_400(self):
        for body in (b'{"port": "abc"}', b'{"port": true}', b'{"port": -1}',
                     b'{"port": 1e999}'):
            code, _ = _post_raw(self.url + "/announce", body)
            self.assertEqual(code, 400, body)

    def test_the_node_is_still_fine_afterwards(self):
        self.assertEqual(_get(self.url + "/info")["magic"],
                         params.NETWORK_MAGIC)

    def test_info_says_which_software_it_is(self):
        info = _get(self.url + "/info")
        self.assertEqual(info["software"], f"kestrel/{kestrel.__version__}")

    def test_health_answers_with_a_status(self):
        try:
            h = _get(self.url + "/health")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)       # alone: not healthy
            h = json.loads(e.read())
        for k in ("ok", "status", "height", "peers_alive", "software"):
            self.assertIn(k, h)

    def test_chain_can_be_fetched_in_pages(self):
        w = Wallet.create()
        with self.node.lock:
            mine(self.chain, w.address, count=4, quiet=True)
        page = _get(self.url + "/chain?from=1&limit=2")["blocks"]
        self.assertEqual([b["height"] for b in page], [1, 2])
        rest = _get(self.url + "/chain?from=3")["blocks"]
        self.assertEqual(rest[0]["height"], 3)
        self.assertEqual(rest[-1]["height"], self.chain.height)


class LanPackets(unittest.TestCase):
    """Anyone on the Wi-Fi can send anything to the discovery port."""

    def test_json_that_is_not_an_object_is_ignored(self):
        for body in (b"[]", b"7", b'"x"', b"null", b"[[[[]]]]"):
            self.assertEqual(
                LanDiscovery.parse_packet(PACKET_PREFIX + body, "10.0.0.2",
                                          "me"), (None, None), body)

    def test_a_bad_port_is_ignored(self):
        for port in ('"4444"', "true", "0", "70000", "4444.5"):
            body = (f'{{"magic": "{params.NETWORK_MAGIC}", "id": "x", '
                    f'"port": {port}}}').encode()
            self.assertEqual(LanDiscovery.parse_packet(
                PACKET_PREFIX + body, "10.0.0.2", "me"), (None, None), port)

    def test_a_good_packet_still_works(self):
        body = json.dumps({"magic": params.NETWORK_MAGIC, "id": "them",
                           "port": 4444}).encode()
        self.assertEqual(LanDiscovery.parse_packet(PACKET_PREFIX + body,
                                                   "10.0.0.2", "me"),
                         ("http://10.0.0.2:4444", "them"))

    def test_the_listener_survives_garbage(self):
        port = _free_port()
        import kestrel.discovery as d
        old = d.DISCOVERY_PORT
        d.DISCOVERY_PORT = port
        found = []
        lan = LanDiscovery(4444, "me", on_peer=lambda u, n: found.append(u))
        try:
            lan.start()
            if not lan.active:
                self.skipTest("UDP not available here")
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.sendto(PACKET_PREFIX + b"[]", ("127.0.0.1", port))
            tx.sendto(PACKET_PREFIX + json.dumps(
                {"magic": params.NETWORK_MAGIC, "id": "them",
                 "port": 5555}).encode(), ("127.0.0.1", port))
            tx.close()
            deadline = time.time() + 5
            while time.time() < deadline and not found:
                time.sleep(0.05)
            self.assertEqual(found, ["http://127.0.0.1:5555"])
        finally:
            lan.stop()
            d.DISCOVERY_PORT = old


class PeerClaims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.node = Node(Blockchain(data_dir=self.tmp), host="127.0.0.1",
                         port=_free_port())
        _quiet(self.node)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_height_that_is_not_a_number_is_not_recorded(self):
        for bad in ("99999999", True, -5, 1.5, None, [1], {"a": 1}):
            self.node._mark("http://1.2.3.4:4444", True, bad)
            h = self.node.peer_info["http://1.2.3.4:4444"].get("height")
            self.assertTrue(h is None or h == 99999999, bad)

    def test_a_claim_the_peer_could_not_back_up_stops_counting(self):
        u = "http://5.6.7.8:4444"
        self.node.peers.add(u)
        self.node._mark(u, True, 10_000_000, work=10**60)
        self.assertEqual(self.node.sync_status()[1], 10_000_000)
        self.node._set_proven(u, False)
        self.assertEqual(self.node.sync_status()[1], self.node.chain.height)

    def test_software_strings_are_cleaned(self):
        u = "http://5.6.7.8:4444"
        self.node._mark(u, True, 1, software="kestrel/1.4.9\x1b[31m" + "x" * 99)
        s = self.node.peer_info[u]["software"]
        self.assertNotIn("\x1b", s)
        self.assertLessEqual(len(s), 40)

    def test_the_peer_book_is_written_atomically(self):
        self.node.add_peers([f"http://9.9.9.{i}:4444" for i in range(30)])
        threads = [threading.Thread(target=self.node._save_peers)
                   for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(os.path.join(self.tmp, "peers.json")) as f:
            self.assertEqual(len(json.load(f)), len(self.node.peers))


class PullSwitchBetweenLiveNodes(unittest.TestCase):
    """The pull path: a reachable peer on a heavier fork. The switch is
    planned, validated without the lock, and adopted — and the explorer
    index winds back to the fork instead of rebuilding from genesis."""

    def test_pull_switch_and_index_rewind(self):
        w1, w2 = Wallet.create(), Wallet.create()
        a = Blockchain(data_dir=tempfile.mkdtemp())
        mine(a, w1.address, count=3, quiet=True)
        b = Blockchain.from_block_dicts([x.to_dict() for x in a.blocks],
                                        data_dir=tempfile.mkdtemp())
        mine(a, w1.address, count=2, quiet=True)          # a: height 5
        mine(b, w2.address, count=4, quiet=True)          # b: height 7

        server, url = _serve(b)
        client = Node(a, host="127.0.0.1", port=_free_port())
        _quiet(client)
        try:
            with client.lock:
                client._reindex()
            index = client._tx_index
            self.assertIn(w1.address, client._addr_index)

            msg = client.sync_peer(url)
            self.assertIn("switched", msg)
            self.assertEqual(a.tip.block_id, b.tip.block_id)

            with client.lock:
                client._reindex()
            self.assertIs(client._tx_index, index)        # rewound, kept
            self.assertEqual(len(client._addr_index[w1.address]), 3)
            self.assertEqual(len(client._addr_index[w2.address]), 4)
            self.assertEqual(client._index_tip, a.tip.block_id)
            with client.lock:
                v = client._address_view(w2.address)
            self.assertEqual(v["confirmed"], 4 * params.INITIAL_REWARD)
        finally:
            server.stop()

    def test_catching_up_from_scratch_uses_pages(self):
        w = Wallet.create()
        src = Blockchain(data_dir=tempfile.mkdtemp())
        mine(src, w.address, count=7, quiet=True)
        server, url = _serve(src)
        import kestrel.node as kn
        old = kn.FETCH_BATCH
        kn.FETCH_BATCH = 2
        try:
            fresh = Blockchain(data_dir=tempfile.mkdtemp())
            client = Node(fresh, host="127.0.0.1", port=_free_port())
            _quiet(client)
            client.sync_peer(url)
            self.assertEqual(fresh.tip.block_id, src.tip.block_id)
        finally:
            kn.FETCH_BATCH = old
            server.stop()


class RichlistCache(unittest.TestCase):
    def test_it_follows_the_chain(self):
        w1, w2 = Wallet.create(), Wallet.create()
        c = Blockchain(data_dir=tempfile.mkdtemp())
        node = Node(c, host="127.0.0.1", port=_free_port())
        _quiet(node)
        mine(c, w1.address, count=2, quiet=True)
        first = node._richlist(5)
        self.assertEqual(first[0]["address"], w1.address)
        mine(c, w2.address, count=3, quiet=True)
        again = node._richlist(5)
        self.assertEqual(again[0]["address"], w2.address)
        self.assertEqual(again[0]["amount"], 3 * params.INITIAL_REWARD)


# ============================================================== on disk

class DamagedFiles(unittest.TestCase):
    """A file that parses as JSON but is the wrong shape used to raise
    AttributeError on the way up — the app would not open at all."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        c = Blockchain(data_dir=self.tmp)
        mine(c, Wallet.create().address, count=2, quiet=True)
        self.height = c.height

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(text)

    def test_odd_mempool_files(self):
        for text in ("[]", "null", "7", '"x"',
                     json.dumps({"magic": params.NETWORK_MAGIC,
                                 "mempool": "nope",
                                 "mempool_seen": [], "mempool_dropped": 5}),
                     json.dumps({"magic": params.NETWORK_MAGIC,
                                 "mempool": [7, None, {"inputs": 5}],
                                 "mempool_dropped": {"a": "NaN"}})):
            self.write("mempool.json", text)
            self.assertEqual(Blockchain(data_dir=self.tmp).height,
                             self.height, text)

    def test_odd_validation_marks(self):
        for text in ("[]", "null", '{"magic": "' + params.NETWORK_MAGIC +
                     '", "height": true, "tip": "x"}'):
            self.write("validated.json", text)
            self.assertEqual(Blockchain(data_dir=self.tmp).height,
                             self.height, text)

    def test_a_block_line_that_is_not_an_object(self):
        with open(os.path.join(self.tmp, "blocks.jsonl"), "a") as f:
            f.write("[1, 2, 3]\n")
        self.assertEqual(Blockchain(data_dir=self.tmp).height, self.height)

    def test_an_odd_peer_book(self):
        self.write("peers.json", '{"not": "a list"}')
        n = Node(Blockchain(data_dir=self.tmp), host="127.0.0.1",
                 port=_free_port())
        self.assertIsInstance(n.peers, set)


class WalletFilesAreNeverOverwritten(unittest.TestCase):
    """An app that can't read the wallet file makes a new wallet. It used
    to write that straight over the unreadable file — which, damaged or
    not, may be the only copy of somebody's key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "kestrel-wallet.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def baks(self):
        return sorted(f for f in os.listdir(self.tmp) if f.endswith(".bak"))

    def test_an_unreadable_file_is_kept_aside(self):
        for junk in ('{"private_key": "5f3a…truncat', "", "[]", "not json"):
            with open(self.path, "w") as f:
                f.write(junk)
            before = len(self.baks())
            kept = Wallet.create().save(self.path)
            self.assertIsNotNone(kept, junk)
            self.assertEqual(len(self.baks()), before + 1)
            with open(kept) as f:
                self.assertEqual(f.read(), junk)

    def test_a_different_wallet_is_kept_aside(self):
        a, b = Wallet.create(), Wallet.create()
        a.save(self.path)
        kept = b.save(self.path)
        self.assertEqual(Wallet.load(kept).address, a.address)
        self.assertEqual(Wallet.load(self.path).address, b.address)

    def test_saving_the_same_wallet_again_makes_no_copy(self):
        a = Wallet.create()
        a.save(self.path)
        self.assertIsNone(a.save(self.path))
        self.assertEqual(self.baks(), [])

    def test_two_in_the_same_second_both_survive(self):
        for _ in range(3):
            with open(self.path, "w") as f:
                f.write("damaged")
            Wallet.create().save(self.path)
        self.assertEqual(len(self.baks()), 3)


class Amounts(unittest.TestCase):
    def test_only_plain_digits(self):
        for bad in ("²", "١٢", "1.²", "½", "1e3", "-1", "0x10"):
            with self.assertRaises(ValueError, msg=bad):
                parse_ksl(bad)
        self.assertEqual(parse_ksl("12.5"), 1_250_000_000)
        self.assertEqual(parse_ksl("1,000 KSL"), 1_000 * params.COIN)


class LogRotation(unittest.TestCase):
    def test_a_long_running_log_rotates(self):
        tmp = tempfile.mkdtemp()
        old = (logfile.MAX_BYTES, logfile.CHECK_EVERY, logfile._PATH,
               logfile._QUIET)
        try:
            logfile.setup(tmp, "test", quiet=True)
            logfile.MAX_BYTES, logfile.CHECK_EVERY = 5_000, 10
            for i in range(600):
                logfile.write(f"line {i} " + "x" * 40, to_console=False)
            path = os.path.join(tmp, "kestrel-log.txt")
            self.assertTrue(os.path.exists(path + ".1"))
            self.assertLess(os.path.getsize(path), 5_000 + 60 * 12)
        finally:
            (logfile.MAX_BYTES, logfile.CHECK_EVERY, logfile._PATH,
             logfile._QUIET) = old
            shutil.rmtree(tmp, ignore_errors=True)


# ============================================================== updates

def _write(root, rel, body, mode=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    if mode is not None:
        os.chmod(path, mode)


class UpdaterLeavesYourFilesAlone(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = os.path.join(self.tmp, "app")
        for rel, body in {
            "app.py": "MARKER = 'old'\n",
            os.path.join("kestrel", "__init__.py"): '__version__ = "1.0.0"\n',
            os.path.join("kestrel", "dropped.py"): "# old\n",
            "README.md": "old readme\n",
            "run.sh": "#!/bin/sh\necho old\n",
            # the person's own things
            "my-backup-key.txt": "KEY: L1aW4aubDFB7yfras2S1mN3bqg9w3...\n",
            "notes.md": "remember to back up\n",
            "export.csv": "a,b\n",
            "kestrel-wallet.json": '{"private_key": "THE-MONEY"}',
            "kestrel-wallet.json.replaced-1700000000.bak":
                '{"private_key": "OLD-MONEY"}',
            "kestrel-log.txt": "diagnostics\n",
            os.path.join(".venv", "bin", "python"): "#!/bin/sh\n",
        }.items():
            _write(self.app, rel, body)
        self.staging = os.path.join(self.app, ".kestrel-update")
        os.makedirs(self.staging)
        self.new = os.path.join(self.tmp, "new")
        _write(self.new, "app.py", "MARKER = 'new'\n")
        _write(self.new, os.path.join("kestrel", "__init__.py"),
               '__version__ = "2.0.0"\n')
        _write(self.new, "README.md", "new readme\n")
        _write(self.new, "run.sh", "#!/bin/sh\necho new\n", mode=0o644)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def apply(self):
        script = os.path.join(self.staging, "apply_update.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(updates.APPLY_SCRIPT)
        stand_in = subprocess.Popen([sys.executable, "-c",
                                     "import time; time.sleep(1)"])
        cfg = os.path.join(self.staging, "apply.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"app_dir": self.app, "staged": self.new,
                       "backup": os.path.join(self.staging, "previous"),
                       "keep": sorted(updates.KEEP),
                       "keep_prefixes": list(updates.KEEP_PREFIXES),
                       "required": list(updates.REQUIRED),
                       "pid": stand_in.pid,
                       "relaunch": [sys.executable, "-c", "pass"]}, f)
        out = subprocess.run([sys.executable, script, cfg],
                             capture_output=True, text=True,
                             cwd=self.staging, timeout=180)
        stand_in.wait(timeout=30)
        return out

    def read(self, rel):
        try:
            with open(os.path.join(self.app, rel), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def test_personal_files_survive(self):
        self.apply()
        self.assertIn("new", self.read("app.py"))
        self.assertIsNone(self.read(os.path.join("kestrel", "dropped.py")))
        # none of these were shipped by any release; all must survive
        self.assertIn("KEY:", self.read("my-backup-key.txt"))
        self.assertEqual(self.read("notes.md"), "remember to back up\n")
        self.assertEqual(self.read("export.csv"), "a,b\n")
        self.assertEqual(self.read("kestrel-log.txt"), "diagnostics\n")
        self.assertIsNotNone(self.read(os.path.join(".venv", "bin",
                                                    "python")))

    def test_key_material_never_lands_in_the_backup(self):
        self.apply()
        backup = os.path.join(self.staging, "previous")
        for base, _dirs, files in os.walk(backup):
            for f in files:
                self.assertFalse(f.startswith("kestrel-wallet"), f)
                self.assertNotEqual(f, "my-backup-key.txt")
        self.assertIn("OLD-MONEY", self.read(
            "kestrel-wallet.json.replaced-1700000000.bak"))

    @unittest.skipIf(os.name == "nt", "no execute bit on Windows")
    def test_the_launcher_stays_executable(self):
        self.apply()
        mode = os.stat(os.path.join(self.app, "run.sh")).st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    @unittest.skipIf(os.name == "nt", "no execute bit on Windows")
    def test_extraction_keeps_the_execute_bit(self):
        src = os.path.join(self.tmp, "zsrc")
        _write(src, os.path.join("kestrel-miner", "app.py"), "x\n")
        _write(src, os.path.join("kestrel-miner", "kestrel", "__init__.py"),
               '__version__ = "9.9.9"\n')
        _write(src, os.path.join("kestrel-miner", "run.sh"), "#!/bin/sh\n",
               mode=0o755)
        _write(src, os.path.join("kestrel-miner", "tool.py"), "#\n",
               mode=0o755)
        z = os.path.join(self.tmp, "u.zip")
        with zipfile.ZipFile(z, "w") as zf:
            for base, _d, files in os.walk(src):
                for f in files:
                    p = os.path.join(base, f)
                    zf.write(p, os.path.relpath(p, src))
        root = updates.stage(z, os.path.join(self.tmp, "stage"))
        self.assertTrue(os.stat(os.path.join(root, "run.sh")).st_mode
                        & stat.S_IXUSR)
        self.assertTrue(os.stat(os.path.join(root, "tool.py")).st_mode
                        & stat.S_IXUSR)


class GithubChecksums(unittest.TestCase):
    """The updater trusts the SHA-256 GitHub publishes per release file."""

    def test_the_digest_field_is_read(self):
        h = "ab" * 32
        self.assertEqual(updates.asset_sha256({"digest": "sha256:" + h}), h)
        self.assertEqual(updates.asset_sha256({"digest": "SHA256:" + h.upper()}), h)
        for bad in ({}, {"digest": None}, {"digest": "md5:" + h},
                    {"digest": "sha256:xyz"}, "nope"):
            self.assertIsNone(updates.asset_sha256(bad))

    def test_a_download_that_does_not_match_is_refused(self):
        tmp = tempfile.mkdtemp()
        app = os.path.join(tmp, "app")
        _write(app, "app.py", "x\n")
        _write(app, os.path.join("kestrel", "__init__.py"), '__version__ = "1.0"\n')
        z = os.path.join(tmp, "kestrel-miner.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("kestrel-miner/app.py", "new")
            zf.writestr("kestrel-miner/kestrel/__init__.py", '__version__ = "9.9.9"')
        rel = {"version": "v9.9.9", "assets": [
            {"name": "kestrel-miner.zip", "url": "file://" + z, "size": 0,
             "sha256": "0" * 64}]}
        with self.assertRaises(ValueError):
            updates.install(rel, "kestrel-miner", app,
                            os.path.join(app, "app.py"))
        with open(os.path.join(app, "app.py")) as f:
            self.assertEqual(f.read(), "x\n")        # nothing replaced
        shutil.rmtree(tmp, ignore_errors=True)

    def test_a_download_with_no_checksum_is_refused(self):
        tmp = tempfile.mkdtemp()
        app = os.path.join(tmp, "app")
        _write(app, "app.py", "x\n")
        _write(app, os.path.join("kestrel", "__init__.py"), '__version__ = "1.0"\n')
        rel = {"version": "v9.9.9", "assets": [
            {"name": "kestrel-miner.zip", "url": "file:///nonexistent",
             "size": 0}]}
        with self.assertRaises(ValueError) as cm:
            updates.install(rel, "kestrel-miner", app,
                            os.path.join(app, "app.py"))
        self.assertIn("checksum", str(cm.exception))
        with open(os.path.join(app, "app.py")) as f:
            self.assertEqual(f.read(), "x\n")
        shutil.rmtree(tmp, ignore_errors=True)


class HostileInputIsRefusedNotRaised(unittest.TestCase):
    """Found in the 1.4.9 pre-release review."""

    def test_odd_digit_strings_are_not_numbers(self):
        from kestrel.node import _as_int
        self.assertEqual(_as_int("²", 7), 7)
        self.assertEqual(_as_int("٣", 7), 7)
        self.assertEqual(_as_int("42"), 42)

    def test_a_negative_target_claims_no_work(self):
        self.assertEqual(Blockchain.claimed_work([{"target": "-1"}]), 0)

    def test_a_huge_number_in_the_mempool_file_is_not_fatal(self):
        from kestrel.blockchain import _is_number
        self.assertTrue(_is_number(10 ** 400))
        self.assertFalse(_is_number(float("inf")))
        self.assertFalse(_is_number(True))

    def test_a_slow_peer_hits_the_deadline(self):
        class Drip:
            def read1(self, n):
                time.sleep(0.05)
                return b"x"
        t0 = time.time()
        with self.assertRaises(TimeoutError):
            Node._read_capped(Drip(), 10 ** 6, time.time() + 0.3)
        self.assertLess(time.time() - t0, 2)


class RecoveringFromTheOldUpdater(unittest.TestCase):
    """Coming from 1.4.8, the OLD install script is what ran: it deleted
    the person's .txt/.md files (keeping copies in previous-version) and
    left run.sh without its execute bit. 1.4.9 repairs both on start."""

    def setUp(self):
        self.app = tempfile.mkdtemp()
        prev = os.path.join(self.app, ".kestrel-update", "previous-version")
        # what the 1.4.8 script backed up before replacing things
        for rel, body in {
            "app.py": "old\n",
            "README.md": "old readme\n",
            "my-backup-key.txt": "KEY\n",
            os.path.join("exports", "march.md"): "notes\n",
            os.path.join("kestrel", "gone.py"): "# dropped on purpose\n",
            "kestrel-log.txt": "old log\n",
            # 1.4.8 had no prefix rule, so it deleted this one too
            "kestrel-wallet-backup.txt": "KEY2\n",
        }.items():
            _write(prev, rel, body)
        # what the folder looks like after it ran
        _write(self.app, "app.py", "new\n")
        _write(self.app, "README.md", "new readme\n")
        _write(self.app, os.path.join("kestrel", "__init__.py"), "#\n")
        _write(self.app, "run.sh", "#!/bin/sh\n", mode=0o644)

    def tearDown(self):
        shutil.rmtree(self.app, ignore_errors=True)

    def test_personal_files_come_back_and_code_does_not(self):
        got = sorted(updates.after_update(self.app))
        self.assertEqual(got, sorted(["my-backup-key.txt",
                                      "kestrel-wallet-backup.txt",
                                      "kestrel-log.txt",
                                      os.path.join("exports", "march.md")]))
        with open(os.path.join(self.app, "kestrel-wallet-backup.txt")) as f:
            self.assertEqual(f.read(), "KEY2\n")
        with open(os.path.join(self.app, "my-backup-key.txt")) as f:
            self.assertEqual(f.read(), "KEY\n")
        self.assertFalse(os.path.exists(os.path.join(self.app, "kestrel",
                                                     "gone.py")))
        with open(os.path.join(self.app, "README.md")) as f:
            self.assertEqual(f.read(), "new readme\n")     # not overwritten

    def test_it_happens_once(self):
        updates.after_update(self.app)
        os.remove(os.path.join(self.app, "my-backup-key.txt"))  # deliberate
        self.assertEqual(updates.after_update(self.app), [])
        self.assertFalse(os.path.exists(os.path.join(self.app,
                                                     "my-backup-key.txt")))

    @unittest.skipIf(os.name == "nt", "no execute bit on Windows")
    def test_the_launcher_is_made_executable_again(self):
        updates.after_update(self.app)
        self.assertTrue(os.stat(os.path.join(self.app, "run.sh")).st_mode
                        & stat.S_IXUSR)

    def test_nothing_to_do_on_a_fresh_install(self):
        fresh = tempfile.mkdtemp()
        _write(fresh, "app.py", "x\n")
        self.assertEqual(updates.after_update(fresh), [])
        shutil.rmtree(fresh, ignore_errors=True)


# ============================================================ packaging

class Packaging(unittest.TestCase):
    def test_every_app_ships_the_same_package(self):
        """Each app carries its own copy of kestrel/. They drifted once
        (v1.4.1 shipped truncated copies of two modules)."""
        core = os.path.join(ROOT, "kestrel-core", "kestrel")
        names = sorted(f for f in os.listdir(core) if f.endswith(".py"))
        for app in ("kestrel-miner", "kestrel-wallet"):
            pkg = os.path.join(ROOT, app, "kestrel")
            self.assertEqual(
                sorted(f for f in os.listdir(pkg) if f.endswith(".py")),
                names, app)
            for n in names:
                with open(os.path.join(core, n), "rb") as a, \
                        open(os.path.join(pkg, n), "rb") as b:
                    self.assertEqual(a.read(), b.read(), f"{app}/{n}")

    def test_versions_agree(self):
        self.assertEqual(updates.CURRENT, kestrel.__version__)
        from kestrel import node
        self.assertEqual(node.SOFTWARE, f"kestrel/{kestrel.__version__}")

    @unittest.skipIf(os.name == "nt", "no execute bit on Windows")
    def test_launchers_are_executable_in_the_tree(self):
        for rel in ("run-tests.sh", "build-apps.sh", "sync-packages.sh",
                    "kestrel-core/start.sh", "kestrel-miner/run.sh",
                    "kestrel-wallet/run.sh", "deploy/setup-vps.sh",
                    "deploy/set-seeds.sh"):
            path = os.path.join(ROOT, rel)
            if os.path.exists(path):
                self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR, rel)


if __name__ == "__main__":
    unittest.main()
