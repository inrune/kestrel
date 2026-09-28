"""
Kestrel full node.

A threaded HTTP server that does two jobs:

  1. Peer-to-peer consensus — new blocks and transactions are gossiped to
     known peers, and a background loop adopts any peer chain with more
     accumulated proof-of-work (fully re-validated locally; nothing from the
     network is trusted).
  2. A plain-JSON API, open CORS. This is the platform: explorers, wallets,
     dashboards and bots are all built by whoever wants to build them —
     Kestrel ships the protocol, the world ships the apps.

Networking is zero-config, the way Bitcoin launched:
  - on start the node contacts the seed nodes (params.SEED_NODES, the
    KESTREL_SEEDS env var, or a seeds.txt file) and announces itself
  - nodes on the same Wi-Fi/LAN find each other automatically via UDP
    broadcast — no addresses to type at all
  - nodes ANYWHERE ON EARTH find each other automatically through the
    public BitTorrent DHT (rendezvous.py) — each node announces itself
    under the network's key and looks up everyone else, no server needed
  - peers exchange peer lists ("peer exchange"), so knowing one node is
    enough to learn the whole mesh
  - every node re-announces and re-syncs continuously; dead peers are
    dropped after repeated failures, good ones are remembered on disk
  - mempools sync too, so a transaction sent anywhere reaches every miner

JSON API
  GET  /info                 node + chain summary (p2p handshake)
  GET  /health               one-line health check for monitors (200 / 503)
  GET  /supply               rich chain statistics
  GET  /latest[?n=15]        newest blocks (light view)
  GET  /chain[?from=H&limit=N]  full blocks from height H (default 0)
  GET  /block/<height>       one block, enriched, with transactions
  GET  /blockhash/<id>       one block by block id
  GET  /tx/<txid>            a transaction (chain or mempool) with context
  GET  /address/<addr>       balance, UTXOs and history for an address
  GET  /balance/<addr>       confirmed + spendable balance
  GET  /utxos/<addr>         spendable outputs for an address
  GET  /richlist[?n=20]      largest balances
  GET  /search/<query>       classify a height / block id / txid / address
  GET  /mempool              pending transactions (ids, views and raw)
  GET  /peers                known peer URLs + liveness
  POST /tx                   submit a signed transaction  {tx: {...}}
  POST /block                submit a mined block         {block: {...}}
  POST /chain                push a heavier chain         {blocks, from}
  POST /announce             p2p hello: {port, id} — registers caller as peer
  POST /peers/add            register a peer              {url: "http://..."}
  POST /mine                 mine n blocks (loopback only){address, count}
  GET  /                     live HTML dashboard (browsers) / JSON welcome (API)
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import params
from .block import Block
from .blockchain import Blockchain, ValidationError, MEMPOOL_TTL, UNDO_DEPTH
from .transaction import Transaction
from .wallet import format_ksl
from .miner import assemble_candidate, find_pow
from . import upnp
from . import logfile
from . import dashboard
from .discovery import (LanDiscovery, load_seed_nodes, fetch_remote_seeds,
                        new_node_id, get_lan_ip, is_routable_url,
                        is_routable_host, normalize_peer_url)
from .rendezvous import DhtRendezvous

SYNC_INTERVAL = int(os.environ.get("KESTREL_SYNC_INTERVAL", "15"))
ANNOUNCE_INTERVAL = 120
MAX_PEERS = 40
MAX_FAILS = 6          # consecutive failures before a non-seed peer is dropped
NEW_PEER_FAILS = 3     # ...but a peer that NEVER answered is dropped sooner
SYNC_WORKERS = 8       # parallel peer connections (dead peers can't stall us)
MAX_PUSH_BLOCKS = 50_000    # cap on blocks accepted in one POST /chain
PUSH_WINDOW = 2_000         # how far back a fork-heal push reaches
ALONE_AFTER_ATTEMPTS = 4    # sync rounds with no answer before we accept
                            # that this really is a network of one
REMAP_INTERVAL = 15 * 60    # re-ask the router for the port (renew/reboot)
RECHECK_INTERVAL = 10 * 60  # re-test reachability (things change)
SEED_REFRESH = 10 * 60      # re-fetch published seed lists while peerless
REBROADCAST_INTERVAL = 10 * 60  # re-offer our pending transactions to peers
REBROADCAST_MAX = 50            # ...at most this many per round
FETCH_BATCH = 500           # blocks per request while catching up
LOCK_SLICE = 250            # blocks validated per hold of the node lock
MAX_CATCHUP_BATCHES = 1_000  # 500,000 blocks per sync pass, then yield

# Hard limits on what a peer can make us read. The old client read whole
# responses into memory with no limit and no deadline, so one hostile or
# broken peer could exhaust memory, or drip a byte every few seconds and
# hold a sync worker hostage indefinitely.
SMALL_RESPONSE = 8 * 1024 * 1024          # /info, /mempool, handshakes
CHAIN_RESPONSE = 512 * 1024 * 1024        # /chain
RESPONSE_DEADLINE = 180                   # seconds for any one response
MAX_BODY = 4 * params.MAX_BLOCK_SIZE      # request bodies in general
MAX_CHAIN_BODY = 64 * 1024 * 1024         # POST /chain carries many blocks

try:
    from . import __version__ as SOFTWARE_VERSION
except ImportError:                            # pragma: no cover
    SOFTWARE_VERSION = "?"
SOFTWARE = f"kestrel/{SOFTWARE_VERSION}"
# (v1.4: connections that just work — loose addresses accepted everywhere,
#  instant mutual handshake on manual add, parallel sync/announce loops,
#  NAT-PMP + UPnP renewal, DHT node caching + fast retry while peerless,
#  and peer-book hygiene so dead addresses can't crowd out live ones)


# ------------------------------------------------------------ strict JSON

def _no_constants(name):
    raise ValueError(f"{name} is not valid JSON")


def strict_loads(raw):
    """json.loads for input from strangers.

    Python's parser accepts NaN and Infinity, which are not JSON and which
    blow up later as OverflowError deep inside a block parser — an
    exception nothing was expecting. Nesting deep enough to exhaust the
    stack is a RecursionError. Both come back here as a plain ValueError.
    """
    try:
        return json.loads(raw, parse_constant=_no_constants)
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None


def _as_int(v, default=0) -> int:
    """An integer from untrusted JSON, or `default`. Never a bool."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) < 200:
        return int(v)       # isascii: "²".isdigit() is True, int("²") fails
    return default


class Node:
    def __init__(self, chain: Blockchain, host: str = "0.0.0.0",
                 port: int = params.DEFAULT_PORT, peers: list[str] = None):
        self.chain = chain
        self.host, self.port = host, port
        self.node_id = new_node_id()
        self.started = time.time()
        self.on_log = None                # apps can hook this: fn(msg, level)
        self.public_ip = None             # learned from peers / the router
        self.upnp_mapped = False
        self.reachable = None             # can others connect IN? (None=unknown)
        self.best_height = chain.height   # tallest chain seen on the network

        self.seeds = set(load_seed_nodes(chain.data_dir))
        # accept peers however people type them: bare IP, ip:port, full URL
        self.peers: set[str] = set(
            u for u in (normalize_peer_url(p) for p in (peers or [])) if u)
        self.peers |= self.seeds
        self._peers_path = os.path.join(chain.data_dir, "peers.json")
        self._peers_lock = threading.Lock()
        try:
            with open(self._peers_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            for p in saved if isinstance(saved, list) else []:
                u = normalize_peer_url(str(p))
                if u and len(self.peers) < MAX_PEERS:
                    self.peers.add(u)
        except Exception:
            pass
        self.peers.discard(f"http://127.0.0.1:{port}")
        self.peers.discard(f"http://localhost:{port}")

        # liveness bookkeeping for UIs and pruning
        self.peer_info: dict[str, dict] = {}   # url -> {alive,height,last,fails}

        self.lock = threading.RLock()
        # one fork validation at a time: several peers offering the same
        # heavier chain at once would otherwise validate it several times
        # over, in parallel, for nothing
        self._switching = threading.Lock()
        os.makedirs(chain.data_dir, exist_ok=True)
        self._save_peers()
        self._stop = threading.Event()
        self._sync_attempts = 0
        self.joined_network = False
        self._last_remap = 0.0        # when we last asked the router
        self._last_recheck = 0.0      # when we last tested reachability
        self._last_seedfetch = 0.0    # when we last pulled the seed lists
        self._last_rebroadcast = time.time()
        self.discovery = LanDiscovery(self.port, self.node_id,
                                      on_peer=self._on_lan_peer)
        self.rendezvous = DhtRendezvous(
            self.port, on_peer=self._on_world_peer, on_log=self._log,
            data_dir=chain.data_dir,
            # advertise while reachable or still unknown; stop once we
            # KNOW inbound is blocked (a dead address helps no one)
            should_announce=lambda: self.reachable is not False)
        # lazy indexes, extended block by block (see _reindex)
        self._index_at = -1
        self._index_tip = None                    # tip the indexes describe
        self._tx_index: dict[str, tuple] = {}     # txid -> (height, tx)
        self._block_index: dict[str, int] = {}    # block_id -> height
        self._addr_index: dict[str, list] = {}    # address -> [entries]
        self._index_log: dict[int, tuple] = {}    # height -> what it added
        self._rich = None                         # (chain version, ranking)

    # ------------------------------------------------------------------ log

    def _log(self, msg: str, level: str = "info"):
        # Goes to the log file always, and to the console only when the
        # console is the point (the CLI). In a windowed app it used to
        # print regardless, over whatever the person had in that terminal.
        logfile.write(f"[node] {msg}", level=level)
        if self.on_log:
            try:
                self.on_log(msg, level)
            except Exception:
                pass

    # ------------------------------------------------------------ peer book

    def _is_self(self, url: str) -> bool:
        return url in (f"http://127.0.0.1:{self.port}",
                       f"http://localhost:{self.port}")

    def _save_peers(self):
        """Write the peer book. Many threads call this; one writes at a
        time, and always to a temp file first — two writers on one open
        file used to be able to leave it half one list and half another,
        which then failed to load and cost the whole book."""
        with self._peers_lock:
            tmp = self._peers_path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(sorted(self.peers), fh)
                os.replace(tmp, self._peers_path)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _evict_dead_peer(self) -> bool:
        """Drop one known-dead, non-seed peer to make room for a fresh one.
        Dead addresses must never crowd live newcomers out of a full book."""
        worst, worst_fails = None, 0
        for u in list(self.peers):          # snapshot; see shareable_peers
            if u in self.seeds:
                continue
            info = self.peer_info.get(u, {})
            if info.get("alive"):
                continue
            fails = info.get("fails", 0)
            if fails > worst_fails:
                worst, worst_fails = u, fails
        if worst:
            self.peers.discard(worst)
            self.peer_info.pop(worst, None)
            return True
        return False

    def add_peers(self, urls) -> list[str]:
        """Merge peer URLs (capped). Returns the URLs that were new.
        Input is forgiving: '1.2.3.4', '1.2.3.4:4444' and full URLs all
        work — whatever shape people paste, it connects."""
        fresh = []
        for u in list(urls or [])[:200]:
            u = normalize_peer_url(str(u))
            if not u or self._is_self(u) or u in self.peers:
                continue
            if len(self.peers) >= MAX_PEERS and not self._evict_dead_peer():
                continue
            self.peers.add(u)
            fresh.append(u)
        if fresh:
            self._save_peers()
        return fresh

    def add_network_peers(self, urls) -> list[str]:
        """Merge peers learned FROM the network (peer exchange / DHT).

        Only globally-routable addresses are kept: a 192.168.x.x or
        127.0.0.1 URL from another network is unreachable here and would
        just create dead peers and 'unreachable' errors. LAN peers are
        found separately by UDP discovery, so nothing is lost."""
        if not isinstance(urls, list):
            return []
        return self.add_peers([u for u in urls[:200]
                               if isinstance(u, str) and is_routable_url(u)])

    def shareable_peers(self) -> list[str]:
        """The peers we advertise to others — only ones they could reach.

        We hand out addresses the wider internet can actually connect to,
        preferring peers we've confirmed are alive. Our own public URL is
        included when known so newcomers can find us."""
        # list(set) copies at C level with the GIL held, so it cannot
        # observe a half-mutated set. A comprehension runs a Python-level
        # predicate between steps, which lets another thread add or drop a
        # peer mid-iteration and raises "set changed size during
        # iteration" — in a loop that then dies for the rest of the run.
        snapshot = list(self.peers)
        info = dict(self.peer_info)
        routable = [u for u in snapshot if is_routable_url(u)]
        alive = [u for u in routable if info.get(u, {}).get("alive")]
        out = alive or routable
        mine = self.public_url()
        if mine and self.reachable and mine not in out:
            out = out + [mine]
        return sorted(set(out))

    def _mark(self, url: str, ok: bool, height=None, *, work=None,
              software=None):
        info = self.peer_info.setdefault(
            url, {"alive": False, "height": None, "last": 0, "fails": 0})
        if ok:
            info.update(alive=True, last=time.time(), fails=0,
                        ever_alive=True)
            # Whatever a peer says about itself is a claim, and it ends up
            # formatted in tables and compared with numbers. A height that
            # is not a plain non-negative integer is simply not recorded.
            h = _as_int(height, None)
            if h is not None and h >= 0:
                info["height"] = h
                if work is not None:
                    info["work"] = _as_int(work)
            if isinstance(software, str):
                info["software"] = "".join(
                    ch for ch in software[:40] if ch.isprintable())
        else:
            info["fails"] += 1
            if info["fails"] >= 2:
                info["alive"] = False
            # an address that has NEVER answered (typical of stale entries
            # from the worldwide directory) is given up on quickly; one
            # that worked before gets the full benefit of the doubt
            limit = MAX_FAILS if info.get("ever_alive") else NEW_PEER_FAILS
            if info["fails"] >= limit and url not in self.seeds:
                self.peers.discard(url)
                self.peer_info.pop(url, None)
                self._save_peers()

    def alive_peers(self) -> list[str]:
        info = dict(self.peer_info)           # snapshot; see shareable_peers
        return [u for u in list(self.peers)
                if info.get(u, {}).get("alive")]

    def _drop_self_peer(self, url: str):
        """A peer that turned out to be us, seen through another address."""
        self.peers.discard(url)
        self.peer_info.pop(url, None)
        self._save_peers()

    def _on_lan_peer(self, url: str, nid: str):
        if nid == self.node_id or url in self.peers:
            return
        if self.add_peers([url]):
            self._log(f"Found a Kestrel node on your network: {url}", "good")
            threading.Thread(target=self._greet_and_sync, args=(url,),
                             daemon=True).start()

    def _on_world_peer(self, url: str):
        """A peer learned from the worldwide directory. If it turns out
        to be ourselves seen from outside, the node-id handshake in
        sync_peer detects and drops it automatically."""
        if url in self.peers or not is_routable_url(url):
            return
        if self.add_peers([url]):
            self._log(f"Found a Kestrel node across the internet: {url}",
                      "good")
            threading.Thread(target=self._greet_and_sync, args=(url,),
                             daemon=True).start()

    # ------------------------------------------------------------- transport

    @staticmethod
    def _read_capped(resp, max_bytes: int, deadline: float) -> bytes:
        chunks, got = [], 0
        while True:
            if time.time() > deadline:
                raise TimeoutError("peer took too long to answer")
            # read1 returns whatever has arrived, so a peer dripping a byte
            # at a time still hits the deadline; read(n) would wait for n.
            reader = getattr(resp, "read1", None) or resp.read
            chunk = reader(64 * 1024)
            if not chunk:
                break
            got += len(chunk)
            if got > max_bytes:
                raise ValueError("response too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _http_json(method: str, url: str, payload: dict = None,
                   timeout: int = 10, max_bytes: int = SMALL_RESPONSE):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "User-Agent": SOFTWARE},
        )
        deadline = time.time() + max(timeout, 1) + RESPONSE_DEADLINE
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = Node._read_capped(resp, max_bytes, deadline)
        return strict_loads(raw)

    @classmethod
    def _post_reply(cls, url: str, payload: dict, timeout: int = 60):
        """POST and return (status, json-or-None) without raising on 4xx."""
        try:
            return 200, cls._http_json("POST", url, payload, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                body = strict_loads(e.read(SMALL_RESPONSE))
            except Exception:
                body = None
            return e.code, body if isinstance(body, dict) else None

    def broadcast(self, path: str, payload: dict):
        def push(peer):
            try:
                self._http_json("POST", peer + path, payload, timeout=5)
            except Exception:
                pass
        targets = self.alive_peers() or list(self.peers)
        for peer in targets:
            threading.Thread(target=push, args=(peer,), daemon=True).start()

    def gossip_block(self, block):
        """Announce a block we just accepted, and heal forks the peer
        cannot heal by itself.

        Plain broadcast is fire-and-forget, and syncing is pull-based: a
        peer that falls behind is expected to fetch the chain from us.
        That assumption breaks for the most common setup there is — a
        miner at home behind NAT. The peer can't open a connection back,
        so when the two chains diverge neither side can repair it and both
        keep mining in parallel forever.

        So we read the reply. A peer that says the block doesn't sit on
        its tip, and reports less work than us, gets our chain pushed to
        it. If it reports MORE work, we leave it alone — our own sync loop
        pulls from it, and whichever chain is genuinely heavier wins.
        """
        payload = {"block": block.to_dict()}

        def push(peer):
            try:
                got = self._http_json("POST", peer + "/block", payload,
                                      timeout=5)
            except Exception:
                return
            if not isinstance(got, dict):
                return
            if got.get("accepted") or got.get("reason") != "not on tip":
                return
            theirs = _as_int(got.get("total_work"))
            their_height = _as_int(got.get("height"), -1)
            with self.lock:
                ours, height = self.chain.total_work(), self.chain.height
            if theirs >= ours:
                return          # they're heavier — sync_once will pull
            # Send the recent suffix rather than the entire chain, and
            # tell them where it starts. Pushing every block from genesis
            # meant this healing path switched itself off once the chain
            # outgrew MAX_PUSH_BLOCKS, stranding exactly the NAT'd peers it
            # exists to rescue. A peer that is merely behind gets exactly
            # what it is missing; one that forked deeper than the first
            # push reaches gets a longer one, as far back as undo data
            # goes on either side.
            starts = []
            if 0 <= their_height < height - PUSH_WINDOW:
                # never more than a receiver accepts in one push
                starts.append(max(their_height + 1,
                                  height - MAX_PUSH_BLOCKS + 1))
            for window in (PUSH_WINDOW, UNDO_DEPTH):
                s = max(1, height - window + 1)
                if not starts or s < starts[-1]:
                    starts.append(s)
            for start in starts:
                with self.lock:
                    picked = self.chain.blocks[start:]
                blocks = [b.to_dict() for b in picked]   # outside the lock
                _status, reply = self._post_reply(
                    peer + "/chain", {"blocks": blocks, "from": start},
                    timeout=120)
                if not (reply and reply.get("reason") == "deeper"):
                    return

        targets = self.alive_peers() or list(self.peers)
        for peer in targets:
            threading.Thread(target=push, args=(peer,), daemon=True).start()

    # ----------------------------------------------------------- announcing

    def announce_to(self, peer: str) -> bool:
        """Say hello: the peer records our caller-IP + this port."""
        try:
            got = self._http_json("POST", peer + "/announce",
                                  {"port": self.port, "id": self.node_id},
                                  timeout=5)
            if not isinstance(got, dict):
                return False
            if got.get("id") == self.node_id:
                self._drop_self_peer(peer)
                return False
            me = str(got.get("your_ip", ""))[:64]
            if me and is_routable_host(me):
                self.public_ip = me       # how the world sees us
            self.add_network_peers(got.get("peers", []))
            return True
        except Exception:
            return False

    def _announce_loop(self):
        while not self._stop.is_set():
            try:
                self._announce_round()
            except Exception as e:
                logfile.exception("announce loop", e)
            self._stop.wait(ANNOUNCE_INTERVAL)

    def _announce_round(self):
        """Tell peers and seeds we are here. One pass."""
        # live peers and seeds always; a handful of untried ones too —
        # in parallel, so a pile of dead addresses can't stall the loop
        known = list(self.peers)
        info = dict(self.peer_info)
        targets = set(self.alive_peers()) | (self.seeds & set(known))
        untried = [u for u in known if u not in targets and u not in info]
        targets |= set(untried[:8])
        if not targets:
            return
        with ThreadPoolExecutor(
                max_workers=min(SYNC_WORKERS, len(targets))) as ex:
            list(ex.map(self.announce_to, targets))

    def _greet_and_sync(self, url: str):
        # runs in a background thread — must never raise (unreachable peers
        # are normal on the open internet, not an error worth crashing over)
        try:
            self.announce_to(url)
            self.sync_peer(url)
        except Exception:
            pass

    # ---------------------------------------------------------------- sync

    def sync_peer(self, peer: str) -> str:
        """Handshake + catch-up with one peer. Returns a status string."""
        peer = peer.rstrip("/")
        try:
            info = self._http_json("GET", peer + "/info", timeout=5)
        except Exception as e:
            self._mark(peer, False)
            raise ValidationError(f"unreachable ({e.__class__.__name__})")
        if not isinstance(info, dict) or \
                info.get("magic") != params.NETWORK_MAGIC:
            self._mark(peer, False)
            raise ValidationError("not a Kestrel node")
        if info.get("node_id") == self.node_id:
            self._drop_self_peer(peer)
            return "that address is this node itself"
        first_contact = not self.peer_info.get(peer, {}).get("alive")
        their_work = _as_int(info.get("total_work"))
        their_height = _as_int(info.get("height"), -1)
        self._mark(peer, True, their_height, work=their_work,
                   software=info.get("software"))
        self.add_network_peers(info.get("peers", []))
        if first_contact:   # make sure they know our address too
            threading.Thread(target=self.announce_to, args=(peer,),
                             daemon=True).start()

        result = self._catch_up(peer, their_work, their_height)

        # Mempool sync: pick up pending transactions we don't have.
        # add_transaction refuses anything this node recently gave up on, so
        # a peer that still holds an expired payment can't hand it straight
        # back and restart the clock. Without that, expiry cannot work at
        # all on a network of more than one node.
        try:
            mp = self._http_json("GET", peer + "/mempool", timeout=10)
            raw = mp.get("raw", []) if isinstance(mp, dict) else []
            txs = []
            for d in raw[:500] if isinstance(raw, list) else []:
                try:
                    txs.append(Transaction.from_dict(d))
                except Exception:
                    pass
            with self.lock:
                for tx in txs:
                    try:
                        self.chain.add_transaction(tx)
                    except ValidationError:
                        pass
        except Exception:
            pass
        return result

    def _catch_up(self, peer: str, their_work: int, their_height: int) -> str:
        """Get level with a peer that has more work than us.

        Downloads happen with the node lock released, and validation takes
        it in short slices, so a node that is catching up — even from
        genesis, even across a fork — keeps answering its wallet and keeps
        mining. It used to hold the lock across the download *and* a full
        re-verification of the whole chain.
        """
        with self.lock:
            our_work, our_height = self.chain.total_work(), self.chain.height
        if their_work <= our_work:
            # a taller claim with no more work behind it (a lighter fork,
            # or a made-up height) is not a chain we would ever follow, so
            # it must not hold the UI at "downloading" or /health at 503
            self._set_proven(peer, their_height <= our_height)
            return f"in sync at block {our_height:,}"

        # 1. usually they are simply ahead of us on the same branch
        added_total = 0
        for _ in range(MAX_CATCHUP_BATCHES):
            if self._stop.is_set():
                break
            data = self._http_json(
                "GET", f"{peer}/chain?from={our_height + 1}"
                       f"&limit={FETCH_BATCH}",
                timeout=60, max_bytes=CHAIN_RESPONSE)
            blocks = data.get("blocks") if isinstance(data, dict) else None
            if not isinstance(blocks, list) or not blocks:
                break
            added = self._extend(blocks)
            added_total += added
            with self.lock:
                our_work, our_height = (self.chain.total_work(),
                                        self.chain.height)
            # an older peer ignores `limit` and sends everything at once
            if not added or our_work >= their_work \
                    or len(blocks) > FETCH_BATCH:
                break
        if added_total:
            self._log(f"Downloaded {added_total:,} block(s) from {peer} — "
                      f"now at block {our_height:,}", "good")
        if our_work >= their_work:
            self._set_proven(peer, their_height <= our_height)
            return f"caught up to block {our_height:,}"

        # 2. otherwise their chain forks off ours somewhere
        took, status = self._fork_switch(peer, their_height)
        with self.lock:
            height = self.chain.height
        if took:
            self._set_proven(peer, True)
            return f"switched to the heavier chain (block {height:,})"
        if status != "busy":
            # they claimed more work and could not back it up; don't let
            # that claim keep the UI saying "downloading" forever
            self._set_proven(peer, False)
        if added_total:
            return f"caught up to block {height:,}"
        return f"in sync at block {height:,}"

    def _set_proven(self, peer: str, ok: bool):
        info = self.peer_info.get(peer)
        if info is not None:
            info["unproven"] = not ok

    def _extend(self, blocks: list) -> int:
        """Append blocks that build on our tip, a slice at a time."""
        added = 0
        for i in range(0, len(blocks), LOCK_SLICE):
            with self.lock:
                n = self.chain.extend_with(blocks[i:i + LOCK_SLICE])
            added += n
            if n == 0:
                break
        return added

    def _fork_switch(self, peer: str, their_height: int):
        """Fetch as much of a peer's chain as it takes to find where it
        forks from ours, then try to switch to it."""
        if not self._switching.acquire(blocking=False):
            return False, "busy"
        try:
            with self.lock:
                our_height = self.chain.height
            base = min(our_height, max(their_height, 0))
            status = "deeper"
            for window in (PUSH_WINDOW, UNDO_DEPTH):
                start = max(1, base - window + 1)
                data = self._http_json("GET", f"{peer}/chain?from={start}",
                                       timeout=120,
                                       max_bytes=CHAIN_RESPONSE)
                blocks = data.get("blocks") if isinstance(data, dict) \
                    else None
                took, status = self._try_switch(blocks, start, peer=peer,
                                                locked=True)
                if status != "deeper" or start == 1:
                    return took, status
            data = self._http_json("GET", f"{peer}/chain", timeout=300,
                                   max_bytes=CHAIN_RESPONSE)
            blocks = data.get("blocks") if isinstance(data, dict) else None
            return self._try_switch(blocks, 0, peer=peer, locked=True)
        finally:
            self._switching.release()

    def _try_switch(self, blocks, start: int, *, peer: str = None,
                    locked: bool = False):
        """Plan (cheap, locked), validate (expensive, unlocked), adopt
        (cheap, locked). Returns (switched, status)."""
        if not locked:
            if not self._switching.acquire(blocking=False):
                return False, "busy"
        try:
            blocks = self._worked_prefix(blocks, start)
            with self.lock:
                plan, status = self.chain.plan_switch(blocks, start)
            if plan is None:
                return False, status
            try:
                candidate = plan.build()
            except ValidationError as e:
                self._log(f"Refused a chain from {peer or 'a peer'}: {e}",
                          "warn")
                return False, "invalid"
            with self.lock:
                took = self.chain.adopt(candidate, plan)
                height, tip = self.chain.height, self.chain.tip
            if not took:
                return False, "lighter"
            self._log("Switched to a heavier chain"
                      + (f" from {peer}" if peer else "")
                      + f" — now at block {height:,}", "good")
            self.broadcast("/block", {"block": tip.to_dict()})
            return True, "switched"
        finally:
            if not locked:
                self._switching.release()

    def _worked_prefix(self, blocks, start):
        """Their blocks, cut at the first new one whose proof-of-work does
        not meet its own stated target.

        Claimed work is read from the targets a peer writes into its
        headers, so without this one tiny POST naming an absurd target
        could make us rewind thousands of blocks under the lock, only for
        validation to throw the result away. Checking the new blocks'
        hashes first (outside the lock, and only the ones after the fork,
        which validation would hash anyway) makes a claim cost real work.
        """
        if not isinstance(blocks, list) or not all(
                isinstance(d, dict) for d in blocks):
            return blocks                       # plan_switch says "bad"
        try:
            with self.lock:
                fork, status = self.chain.find_fork(blocks, int(start))
        except Exception:
            return blocks
        if status != "ok":
            return blocks
        first = fork - int(start)
        for i in range(first, len(blocks)):
            try:
                ok = Block.from_dict(blocks[i]).has_valid_pow()
            except Exception:
                ok = False
            if not ok:
                return blocks[:i]
        return blocks

    def _sync_quiet(self, peer: str):
        try:
            self.sync_peer(peer)
        except Exception:
            pass

    def sync_once(self):
        """Try every known peer — in parallel. One unreachable address
        used to hold the whole loop hostage for its full timeout; now a
        dead peer costs nothing and a live one connects immediately."""
        peers = list(self.peers)
        if not peers or self._stop.is_set():
            self._sync_attempts += 1
            return
        try:
            with ThreadPoolExecutor(
                    max_workers=min(SYNC_WORKERS, len(peers))) as ex:
                list(ex.map(self._sync_quiet, peers))
        except RuntimeError:
            # The app is closing and Python is tearing the interpreter
            # down underneath us; new threads can no longer be started.
            # There is nothing left to sync to, so this is the end of the
            # loop, not an error worth printing a traceback about.
            return
        self._sync_attempts += 1
        if self.alive_peers():
            self.joined_network = True

    def network_state(self) -> str:
        """Where we stand relative to the rest of the network.

        Mining before we know this is how a node ends up quietly building
        its own private chain: at the starting difficulty a few seconds of
        solo mining can outweigh the real network, after which this node
        correctly refuses to switch and the two never reconcile again.

        'joined'   — we have reached at least one other node
        'looking'  — still trying; too early to say we are alone
        'alone'    — nobody answered after a fair number of attempts, so
                     this is genuinely a new or isolated network
        """
        if self.joined_network or self.alive_peers():
            return "joined"
        if self._sync_attempts < ALONE_AFTER_ATTEMPTS:
            return "looking"
        return "alone"

    def wait_until_known(self, timeout: float = 45.0) -> str:
        """Block until we know whether we are on the network or alone.

        This drives the search itself rather than waiting on the periodic
        sync loop, which only ticks every SYNC_INTERVAL seconds — far too
        slow to keep a user staring at a Start button. It never returns
        "looking": once the time is up having reached nobody, the honest
        answer is that we are alone.
        """
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            if self.joined_network or self.alive_peers():
                return "joined"
            self.sync_once()
            if self.joined_network or self.alive_peers():
                return "joined"
            if self.network_state() == "alone":
                return "alone"
            self._stop.wait(1.5)
        return "joined" if self.alive_peers() else "alone"

    def resync_from_network(self) -> tuple[bool, str]:
        """Rebuild this node's ledger from the network.

        The manual version of this was 'quit the app and delete your
        chain file', which is alarming, easy to get wrong, and sits one
        slip away from deleting the wallet next to it. This does the same
        job safely: it asks every peer where it stands and moves to the
        heaviest valid chain on offer, keeping our own if ours still wins.

        Returns (changed, human-readable message).
        """
        peers = self.alive_peers() or list(self.peers)
        if not peers:
            return False, ("No other nodes are reachable right now, so "
                           "there is nothing to rebuild from. Check your "
                           "internet connection and try again in a moment.")
        with self.lock:
            before_tip, before = self.chain.tip.block_id, self.chain.height
        offers = []
        for peer in peers:
            try:
                info = self._http_json("GET", peer + "/info", timeout=5)
                if isinstance(info, dict) and \
                        info.get("magic") == params.NETWORK_MAGIC:
                    offers.append((_as_int(info.get("total_work")),
                                   _as_int(info.get("height"), -1), peer))
            except Exception:
                continue
        if not offers:
            return False, ("Could not reach any node to rebuild from. "
                           "They may be busy — try again shortly.")
        for work, height, peer in sorted(offers, reverse=True):
            try:
                self._catch_up(peer, work, height)
            except Exception:
                continue
        with self.lock:
            after_tip, after = self.chain.tip.block_id, self.chain.height
        if after_tip != before_tip:
            return True, (f"Rebuilt from the network — now on the shared "
                          f"chain at block {after:,} (was {before:,}).")
        return False, (f"Already on the best chain the network has "
                       f"(block {before:,}). Nothing needed changing.")

    def _maintain(self):
        """Periodic self-healing, called from the sync loop.

        Routers reboot, leases expire, ISPs change your IP, seed lists
        gain new nodes — a node that only checked these things once at
        startup slowly goes deaf. Re-checking keeps it connectable for
        as long as it runs."""
        now = time.time()
        if now - self._last_remap > REMAP_INTERVAL:
            self._last_remap = now
            threading.Thread(target=self._setup_reachability,
                             kwargs={"renew": True}, daemon=True).start()
        # Re-examine pending transactions even when no block has arrived.
        # add_block does this on every new tip, but a node that is offline,
        # still syncing or simply on a quiet network gets no tips — and
        # that is exactly when a payment sits there claiming to be on its
        # way. The clock has to run regardless of the chain.
        with self.lock:
            changed = self.chain.revalidate_mempool()
            for txid, why in changed:
                if why != "confirmed":
                    self._log(f"Pending transaction {txid[:12]}… dropped "
                              f"({why})", "warn")
            if changed:
                # write it down, or a restart brings the expired payment
                # back as pending for as long as it takes to notice again
                try:
                    self.chain._save_pool()
                except OSError as e:
                    logfile.write(f"could not write the mempool to disk "
                                  f"({e})", level="warn")

        if now - self._last_rebroadcast > REBROADCAST_INTERVAL:
            self._last_rebroadcast = now
            self._rebroadcast()

        if self.alive_peers():
            if (self.reachable is None
                    or now - self._last_recheck > RECHECK_INTERVAL):
                self.check_reachability()
        else:
            # peerless: hit the worldwide directory again right away and
            # re-pull the published seed lists — someone may have joined
            self.rendezvous.kick()
            if now - self._last_seedfetch > SEED_REFRESH:
                self._last_seedfetch = now
                threading.Thread(target=self._refresh_seeds,
                                 daemon=True).start()

    def _rebroadcast(self):
        """Offer our pending transactions to our peers again.

        Relaying a payment was a single fire-and-forget broadcast. A
        wallet behind NAT whose one broadcast missed — the peer was
        restarting, the connection blipped — held a payment nobody else
        knew about, and nothing ever tried again; the other nodes could
        not pull it because they cannot dial in. Peers that already have
        a transaction just say so.
        """
        peers = self.alive_peers()
        if not peers:
            return
        with self.lock:
            pool = sorted(self.chain.mempool.items(),
                          key=lambda kv: self.chain.mempool_seen.get(kv[0], 0))
            payloads = [tx.to_dict() for _t, tx in pool[:REBROADCAST_MAX]]
        if not payloads:
            return

        def push_all(peer):
            for p in payloads:
                if self._stop.is_set():
                    return
                try:
                    self._http_json("POST", peer + "/tx", {"tx": p},
                                    timeout=5)
                except urllib.error.HTTPError:
                    continue          # "already in mempool" and friends
                except Exception:
                    return            # the peer went away; next round
        for peer in peers:
            threading.Thread(target=push_all, args=(peer,),
                             daemon=True).start()

    def _refresh_seeds(self):
        remote = fetch_remote_seeds(self.chain.data_dir)
        if remote:
            self.seeds |= {s.rstrip("/") for s in remote}
            for url in self.add_peers(remote):
                threading.Thread(target=self._greet_and_sync, args=(url,),
                                 daemon=True).start()

    def _sync_loop(self):
        """Peer sync and periodic self-healing, forever.

        This one loop owns mempool expiry, reachability re-checks, router
        lease renewal and seed refresh. It is started once and never
        restarted, so an exception escaping here did not skip a round —
        it ended all of that for the rest of the process, silently, while
        the node carried on looking healthy. Nothing gets to do that.
        """
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception as e:
                logfile.exception("sync loop", e)
            try:
                self._maintain()
            except Exception as e:
                logfile.exception("maintenance", e)
            self._stop.wait(SYNC_INTERVAL)

    def public_url(self):
        return f"http://{self.public_ip}:{self.port}" if self.public_ip else None

    def sync_status(self) -> tuple[int, int]:
        """(our height, tallest height the network has shown it can back).

        A peer's claimed height only counts while it has not failed to
        deliver on its claimed work — otherwise one peer reporting a
        height it cannot prove would keep every screen saying
        "downloading" for as long as it stayed connected.
        """
        with self.lock:
            h = self.chain.height
        best = h
        for info in list(self.peer_info.values()):
            ph = info.get("height")
            if info.get("alive") and not info.get("unproven") and \
                    isinstance(ph, int) and ph > best:
                best = ph
        self.best_height = best
        return h, best

    @staticmethod
    def _is_lan_ip(ip: str) -> bool:
        try:
            a = __import__("ipaddress").ip_address(ip)
        except ValueError:
            return False
        return a.is_private or a.is_loopback

    def _probe(self, ip: str, port: int) -> bool:
        """Connect back to ip:port and confirm a Kestrel node answers."""
        try:
            info = self._http_json("GET", f"http://{ip}:{port}/info",
                                   timeout=4)
            return isinstance(info, dict) and \
                info.get("magic") == params.NETWORK_MAGIC
        except Exception:
            return False

    def _setup_reachability(self, renew: bool = False):
        """Best-effort automatic port opening (UPnP, then NAT-PMP) so home
        nodes accept incoming connections — the same trick early Bitcoin
        used. Called again periodically: NAT-PMP leases expire and routers
        reboot, and a re-ask is how the mapping survives both."""
        was_mapped = self.upnp_mapped
        mapped, ext = upnp.open_port(self.port, get_lan_ip(),
                                     description="Kestrel node")
        self.upnp_mapped = mapped
        if ext and not self.public_ip:
            self.public_ip = ext
        if mapped and not was_mapped:
            self._log(f"Your router opened port {self.port} automatically "
                      f"— this node can accept connections from the "
                      f"internet", "good")
            if renew and self.reachable is False:
                self.reachable = None      # worth re-testing now

    def check_reachability(self):
        """Ask an already-connected node to connect BACK to us, so we learn
        whether the outside world can reach this node. Sets self.reachable
        and, on success, self.public_ip. Logs plain-language guidance —
        but only when the answer CHANGES, not every re-check."""
        self._last_recheck = time.time()
        for peer in self.alive_peers():
            if not is_routable_url(peer):
                continue
            try:
                got = self._http_json("POST", peer + "/checkreach",
                                      {"port": self.port}, timeout=8)
            except Exception:
                continue
            if not isinstance(got, dict):
                continue
            ip = str(got.get("your_ip", ""))[:64]
            if is_routable_host(ip):
                self.public_ip = ip
            before = self.reachable
            self.reachable = bool(got.get("reachable"))
            if self.reachable == before:
                return
            if self.reachable:
                self._log(f"This node is reachable from the internet at "
                          f"{self.public_url()} — others can connect to you.",
                          "good")
            else:
                self._log(
                    "Heads up: your computer can reach the network, but "
                    "others cannot connect IN to you (your router/firewall "
                    "blocks it). You'll still sync and mine normally. To let "
                    f"others connect to you, forward TCP {self.port} on your "
                    "router, or run a node on a VPS. A public network needs "
                    "at least one reachable node.", "bad")
            return
        # nobody to ask yet — unknown for now, retried next cycle
        self.reachable = None

    def bootstrap(self):
        """First contact: published seeds + router setup + announce + sync."""
        self._last_seedfetch = self._last_remap = time.time()
        remote = fetch_remote_seeds(self.chain.data_dir)
        if remote:
            self.seeds |= {s.rstrip("/") for s in remote}
            fresh = self.add_peers(remote)
            self._log(f"Seed list: {len(remote)} public node(s) published"
                      + (f", {len(fresh)} new" if fresh else ""), "good")
        self._setup_reachability()
        if self.peers:
            self._log(f"Connecting to {len(self.peers)} known "
                      f"node(s)…")
        targets = list(self.peers)
        if targets:
            with ThreadPoolExecutor(
                    max_workers=min(SYNC_WORKERS, len(targets))) as ex:
                list(ex.map(self.announce_to, targets))
        self.sync_once()
        n = len(self.alive_peers())
        if n:
            self._log(f"Connected — {n} node(s) reachable, "
                      f"block {self.chain.height:,}", "good")
            # anything that was waiting while we were offline goes out now
            self._last_rebroadcast = time.time()
            self._rebroadcast()
            self.check_reachability()
        else:
            self._log("No other nodes reached yet. Still searching the "
                      "worldwide directory and your network… (a brand-new "
                      "network needs at least one always-on, reachable node "
                      "for everyone to find — see the README.)")
        self.warm_indexes()

    # -------------------------------------------------------- chain indexing

    def warm_indexes(self, step: int = 2000):
        """Build the explorer indexes in the background, a slice at a time.

        The first request that needed them used to build all of them in
        one go, holding the node lock for as long as that took — seconds,
        on a long chain, right after start-up, which is exactly when a
        wallet is asking for its balance.
        """
        while not self._stop.is_set():
            with self.lock:
                done = self._reindex(limit=step)
            if done:
                return
            time.sleep(0.01)

    def _reindex(self, limit: int = None) -> bool:
        """Keep the txid / block-id / address lookups level with the chain.

        This used to rebuild all three from genesis whenever the height
        changed — once per block, over the whole chain, holding the node
        lock. On a young chain nobody notices; by a few hundred thousand
        blocks it is seconds of work every two minutes, and every wallet
        asking for its balance waits behind it. The chain almost always
        just grew by a block or two, so index only what is new; after a
        reorg, wind back to the fork and index the new branch, rather
        than starting again from genesis.

        With `limit`, index at most that many blocks and return whether
        the indexes are now complete.
        """
        c = self.chain
        if self._index_at == c.height and self._index_tip is not None \
                and self._index_tip == c.tip.block_id:
            return True

        if self._index_at >= 0 and not self._still_on_branch():
            if not self._rewind_index():
                self._clear_index()

        start = self._index_at + 1
        stop = c.height + 1 if limit is None else min(c.height + 1,
                                                      start + limit)
        chunk = c.blocks[start:stop]
        for b in chunk:
            bid = b.block_id
            txids = []
            self._block_index[bid] = b.height
            for tx in b.transactions:
                t = tx.txid
                txids.append(t)
                self._tx_index[t] = (b.height, tx)
            self._index_log[b.height] = (bid, txids, [])
        # addresses in a second pass: an input's address can only be
        # resolved once the transaction that created it is in _tx_index
        for b in chunk:
            touched = self._index_log[b.height][2]
            for tx, t in zip(b.transactions, self._index_log[b.height][1]):
                for addr, delta in self._deltas_of(tx).items():
                    self._addr_index.setdefault(addr, []).append(
                        (b.height, t, delta, tx.is_coinbase, tx.timestamp))
                    touched.append(addr)
        if chunk:
            self._index_at = chunk[-1].height
            self._index_tip = self._index_log[self._index_at][0]
            floor = self._index_at - UNDO_DEPTH
            if len(self._index_log) > UNDO_DEPTH + 512:
                for h in [h for h in self._index_log if h < floor]:
                    del self._index_log[h]
        return self._index_at == c.height

    def _still_on_branch(self) -> bool:
        c = self.chain
        at = self._index_at
        return at <= c.height and self._index_tip is not None and \
            c.blocks[at].block_id == self._index_tip

    def _clear_index(self):
        self._tx_index, self._block_index, self._addr_index = {}, {}, {}
        self._index_log = {}
        self._index_at, self._index_tip = -1, None

    def _rewind_index(self) -> bool:
        """Undo index entries above the point where the chain changed."""
        c = self.chain
        h = min(self._index_at, c.height)
        while h >= 0:
            entry = self._index_log.get(h)
            if entry is None:
                return False               # older than we kept: rebuild
            if c.blocks[h].block_id == entry[0]:
                break
            h -= 1
        for k in range(self._index_at, h, -1):
            entry = self._index_log.pop(k, None)
            if entry is None:
                return False
            bid, txids, addrs = entry
            self._block_index.pop(bid, None)
            for t in txids:
                hit = self._tx_index.get(t)
                if hit and hit[0] == k:
                    del self._tx_index[t]
            for a in addrs:
                lst = self._addr_index.get(a)
                while lst and lst[-1][0] == k:
                    lst.pop()
                if lst is not None and not lst:
                    del self._addr_index[a]
        self._index_at = h
        self._index_tip = self._index_log[h][0] if h >= 0 else None
        return True

    def _deltas_of(self, tx: Transaction) -> dict:
        """How much this transaction moves for each address it touches."""
        deltas: dict[str, int] = {}
        for o in tx.outputs:
            if type(o.address) is str:   # see Blockchain._apply
                deltas[o.address] = deltas.get(o.address, 0) + o.amount
        if not tx.is_coinbase:
            for i in tx.inputs:
                got = self._resolve_output(i.txid, i.vout)
                if got and type(got[1]) is str:
                    amt, addr = got
                    deltas[addr] = deltas.get(addr, 0) - amt
        return {a: d for a, d in deltas.items() if d}

    def _resolve_output(self, txid: str, vout: int):
        """(amount, address) of a previously created output, or None."""
        hit = self._tx_index.get(txid)
        if not hit:
            return None
        _, tx = hit
        if 0 <= vout < len(tx.outputs):
            o = tx.outputs[vout]
            return o.amount, o.address
        return None

    def address_history(self, addr: str, limit: int = 50) -> list:
        """Newest-first confirmed history for an address: (height, txid,
        delta, coinbase, timestamp) tuples. Call with the lock held."""
        self._reindex()
        entries = self._addr_index.get(addr, ())
        return list(reversed(entries[-limit:])) if limit else \
            list(reversed(entries))

    # ------------------------------------------------------------ enrichment

    def _confirmations(self, height: int) -> int:
        return self.chain.height - height + 1

    def _tx_view(self, tx: Transaction, block_height=None) -> dict:
        c = self.chain
        confirmed = block_height is not None
        txid = tx.txid
        outs = []
        for vout, o in enumerate(tx.outputs):
            outs.append({
                "n": vout,
                "address": o.address,
                "amount": o.amount,
                "amount_ksl": format_ksl(o.amount),
                # only meaningful once confirmed; a mempool tx's outputs
                # aren't in the UTXO set yet but aren't "spent" either
                "spent": confirmed and (txid, vout) not in c.utxos,
            })
        ins, amount_in, resolved = [], 0, True
        if tx.is_coinbase:
            ins.append({"coinbase": True})
        else:
            for i in tx.inputs:
                got = self._resolve_output(i.txid, i.vout)
                if got:
                    amt, addr = got
                    amount_in += amt
                    ins.append({"txid": i.txid, "vout": i.vout,
                                "address": addr, "amount": amt,
                                "amount_ksl": format_ksl(amt)})
                else:
                    resolved = False
                    ins.append({"txid": i.txid, "vout": i.vout})
        amount_out = tx.total_output
        view = {
            "txid": txid,
            "is_coinbase": tx.is_coinbase,
            "timestamp": tx.timestamp,
            "size": tx.size(),
            "inputs": ins,
            "outputs": outs,
            "amount_out": amount_out,
            "amount_out_ksl": format_ksl(amount_out),
        }
        if not tx.is_coinbase and resolved:
            view["fee"] = amount_in - amount_out
            view["fee_ksl"] = format_ksl(amount_in - amount_out)
            view["amount_in"] = amount_in
        if block_height is not None:
            view["block_height"] = block_height
            view["confirmations"] = self._confirmations(block_height)
            view["status"] = "confirmed"
        else:
            view["status"] = "mempool"
            age = c.mempool_age(txid)
            view["first_seen"] = c.mempool_seen.get(txid)
            view["age_seconds"] = round(age, 1)
            view["expires_in"] = round(max(MEMPOOL_TTL - age, 0), 1)
        return view

    def _block_view(self, block: Block, full: bool = False) -> dict:
        c = self.chain
        coinbase = block.transactions[0]
        reward = coinbase.total_output
        miner = coinbase.outputs[0].address if coinbase.outputs else None
        total_out = sum(t.total_output for t in block.transactions)
        view = {
            "height": block.height,
            "block_id": block.block_id,
            "prev_hash": block.prev_hash,
            "merkle_root": block.merkle_root,
            "timestamp": block.timestamp,
            "version": block.version,
            "nonce": block.nonce,
            "target": f"{block.target:064x}",
            "difficulty": c.difficulty_of(block.target),
            "size": block.size(),
            "tx_count": len(block.transactions),
            "reward": reward,
            "reward_ksl": format_ksl(reward),
            "miner": miner,
            "total_out": total_out,
            "total_out_ksl": format_ksl(total_out),
            "confirmations": self._confirmations(block.height),
        }
        if full:
            view["pow_hash"] = block.pow_hash
            view["work"] = block.work
            view["transactions"] = [
                self._tx_view(t, block.height) for t in block.transactions
            ]
        return view

    def _supply_stats(self) -> dict:
        c = self.chain
        self._reindex()
        circ = c.circulating_supply()
        # every txid in the chain is in the index exactly once, so its size
        # is the transaction count — no walk over every block
        tx_count = len(self._tx_index)
        # average interval over the most recent blocks
        recent = [b.timestamp for b in c.blocks[-21:]]
        avg = None
        if len(recent) >= 2:
            avg = round((recent[-1] - recent[0]) / (len(recent) - 1), 1)
        halving_at = ((c.height // params.HALVING_INTERVAL) + 1) * params.HALVING_INTERVAL
        _h, best = self.sync_status()
        return {
            "network": "kestrel",
            "magic": params.NETWORK_MAGIC,
            "software": SOFTWARE,
            "height": c.height,
            "tip": c.tip.block_id,
            "difficulty": c.difficulty_of(c.tip.target),
            "target": f"{c.tip.target:064x}",
            "total_work": c.total_work(),
            "tx_count": tx_count,
            "mempool": len(c.mempool),
            "peers": sorted(self.peers),
            "peer_count": len(self.peers),
            "peers_alive": len(self.alive_peers()),
            "sync_target": best,
            "public_ip": self.public_ip,
            "public_url": self.public_url(),
            "upnp": self.upnp_mapped,
            "reachable": self.reachable,
            "worldwide_discovery": self.rendezvous.active,
            "circulating": circ,
            "circulating_ksl": format_ksl(circ),
            "max_supply": params.MAX_SUPPLY,
            "max_supply_ksl": format_ksl(params.MAX_SUPPLY),
            "pct_mined": round(circ / params.MAX_SUPPLY * 100, 4),
            "block_reward": c.block_subsidy(c.height),
            "block_reward_ksl": format_ksl(c.block_subsidy(c.height)),
            "next_reward": c.block_subsidy(c.height + 1),
            "next_reward_ksl": format_ksl(c.block_subsidy(c.height + 1)),
            "halving_interval": params.HALVING_INTERVAL,
            "next_halving_height": halving_at,
            "blocks_to_halving": halving_at - c.height,
            "target_block_time": params.TARGET_BLOCK_TIME,
            "avg_block_time": avg,
        }

    def health(self) -> dict:
        """What a monitoring probe wants to know, in one small answer."""
        h, best = self.sync_status()
        alive = len(self.alive_peers())
        behind = max(best - h, 0)
        if not alive:
            status = "looking for peers" if \
                self.network_state() == "looking" else "no peers"
        elif behind > 2:
            status = "syncing"
        else:
            status = "synced"
        with self.lock:
            tip_age = max(time.time() - self.chain.tip.timestamp, 0)
        return {"ok": status == "synced", "status": status,
                "height": h, "best_height": best, "behind": behind,
                "peers_alive": alive, "tip_age_seconds": round(tip_age),
                "uptime_seconds": round(time.time() - self.started),
                "software": SOFTWARE}

    def _address_view(self, addr: str, limit: int = 50) -> dict:
        """Everything a wallet needs about one address, in one request.

        Including the pending payments: a wallet used to ask for this and
        then ask for the whole mempool separately, which meant its idea of
        "confirmed" and its idea of "on the way" came from two different
        moments and could disagree — a payment could be in neither, and
        vanish from the screen, or in both, and be counted twice.
        """
        from .crypto_utils import is_valid_address
        c = self.chain
        if not is_valid_address(addr):
            return {"address": addr[:100], "valid": False,
                    "error": "not a Kestrel address"}
        bal = c.balance(addr)
        utxos = c.utxos_for(addr, spendable_only=False)
        entries = self._addr_index.get(addr, ())
        received = sum(d for _h, _t, d, _cb, _ts in entries if d > 0)
        sent = -sum(d for _h, _t, d, _cb, _ts in entries if d < 0)
        history = [
            {"txid": txid, "height": h, "timestamp": ts, "delta": d,
             "delta_ksl": format_ksl(d), "coinbase": cb,
             "confirmations": self._confirmations(h)}
            for h, txid, d, cb, ts in reversed(entries[-limit:])
        ]

        now = time.time()
        pending = []
        for txid, tx in c.mempool.items():
            deltas = self._deltas_of(tx)
            if addr not in deltas:
                continue
            age = c.mempool_age(txid, now=now)
            pending.append({
                "txid": txid,
                "timestamp": tx.timestamp,
                "delta": deltas[addr],
                "delta_ksl": format_ksl(deltas[addr]),
                "outgoing": deltas[addr] < 0,
                "first_seen": c.mempool_seen.get(txid, now),
                "age_seconds": round(age, 1),
                # how long it may still wait before this node gives up
                "expires_in": round(max(MEMPOOL_TTL - age, 0), 1),
            })
        pending.sort(key=lambda p: -p["first_seen"])
        p_in = sum(p["delta"] for p in pending if p["delta"] > 0)
        p_out = -sum(p["delta"] for p in pending if p["delta"] < 0)

        return {
            "address": addr,
            "valid": True,
            "height": c.height,
            "confirmed": bal["confirmed"],
            "confirmed_ksl": format_ksl(bal["confirmed"]),
            "spendable": bal["spendable"],
            "spendable_ksl": format_ksl(bal["spendable"]),
            "received": received,
            "received_ksl": format_ksl(received),
            "sent": sent,
            "sent_ksl": format_ksl(sent),
            "tx_count": len(entries),
            "utxos": [{**u, "amount_ksl": format_ksl(u["amount"])} for u in utxos],
            "history": history,
            "pending": pending,
            "pending_in": p_in,
            "pending_in_ksl": format_ksl(p_in),
            "pending_out": p_out,
            "pending_out_ksl": format_ksl(p_out),
            "mempool_ttl": MEMPOOL_TTL,
        }

    def _richlist(self, n: int = 20) -> list[dict]:
        """The largest balances. Ranked once per block, not per request —
        the dashboard asks every five seconds for every open tab."""
        key = self.chain.version
        if self._rich is None or self._rich[0] != key:
            totals = self.chain.address_totals()
            ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:100]
            self._rich = (key, ranked)
        circ = self.chain.circulating_supply() or 1
        return [{"address": a, "amount": v, "amount_ksl": format_ksl(v),
                 "pct": round(v / circ * 100, 4)}
                for a, v in self._rich[1][:n]]

    def _search(self, q: str) -> dict:
        q = q.strip()[:128]
        c = self.chain
        if q.isdigit() and q.isascii():
            h = int(q)
            if 0 <= h <= c.height:
                return {"type": "height", "value": h}
        if len(q) == 64:
            ql = q.lower()
            if ql in self._block_index:
                return {"type": "block", "value": ql}
            if ql in self._tx_index or ql in c.mempool:
                return {"type": "tx", "value": ql}
        from .crypto_utils import is_valid_address
        if is_valid_address(q):
            return {"type": "address", "value": q}
        return {"type": "none", "value": q}

    # ---------------------------------------------------------------- mining

    def mine_blocks(self, address: str, count: int, threads: int = 1) -> list:
        """Mine `count` blocks to `address`, gossiping each as it's found.

        The chain lock is held only to assemble a candidate and to append
        a solved block — never during the scrypt grind itself. That keeps
        the node's JSON API, dashboard and background sync fully responsive
        while mining, and lets an incoming network block interrupt the
        round (a watcher aborts the search the moment our tip moves, so we
        reassemble on the new tip instead of wasting work on a stale one).
        Returns the heights actually mined.
        """
        mined: list[int] = []
        while len(mined) < count and not self._stop.is_set():
            with self.lock:
                block = assemble_candidate(self.chain, address,
                                           message="kestrel-cli-mine")
                tip_id = block.prev_hash

            round_stop = threading.Event()

            def _watch(tip=tip_id, rs=round_stop):
                while not rs.is_set():
                    if self._stop.is_set():
                        rs.set(); return
                    with self.lock:
                        moved = self.chain.tip.block_id != tip
                    if moved:
                        rs.set(); return
                    rs.wait(0.5)

            watcher = threading.Thread(target=_watch, daemon=True)
            watcher.start()
            found = find_pow(block, threads=threads, stop=round_stop,
                             max_seconds=25)
            round_stop.set()
            if not found:
                continue  # tip moved or the round elapsed — fresh candidate
            try:
                with self.lock:
                    self.chain.add_block(block)
            except ValidationError:
                continue  # a peer beat us to this height; try again
            mined.append(block.height)
            self.gossip_block(block)
        return mined

    # ---------------------------------------------------------------- server

    def stop(self):
        self._stop.set()
        self.discovery.stop()
        self.rendezvous.stop()
        try:
            self._server.shutdown()
        except Exception:
            pass

    def serve_forever(self):
        node = self

        def qint(query: str, key: str, default: int, lo: int, hi: int) -> int:
            for part in query.split("&"):
                if part.startswith(key + "="):
                    try:
                        return max(lo, min(int(part[len(key) + 1:]), hi))
                    except ValueError:
                        return default
            return default

        class _TooLarge(Exception):
            pass

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # drop idle/half-open connections so scanners and slow clients
            # on a public port can't tie up a thread forever
            timeout = 30
            server_version = SOFTWARE
            sys_version = ""

            def log_message(self, *args):
                pass

            # --------------------------------------------------- low-level IO
            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            # The response is BUILT here and SENT after the handler has
            # returned — see do_GET/do_POST below. Writing from inside the
            # handler meant writing to the socket while still holding
            # node.lock, and wfile is unbuffered, so the write blocks until
            # the client drains it. One slow or stalled reader asking for
            # /chain therefore froze mining, block acceptance and every
            # other request for as long as it cared to take.
            def _raw(self, body: bytes, content_type: str, status=200):
                self._out = (status, content_type, body)

            def _send(self, obj, status=200):
                self._raw(json.dumps(obj).encode(), "application/json", status)

            def _flush(self):
                """Actually write the response. Never called under a lock."""
                out, self._out = getattr(self, "_out", None), None
                if out is None:
                    return
                status, content_type, body = out
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self._cors()
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass          # client hung up mid-response; not our problem

            def _body(self, cap: int = MAX_BODY) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    length = -1
                if length < 0:
                    raise ValueError("bad Content-Length")
                if length > cap:
                    # Refused outright rather than truncated: a truncated
                    # body is bad JSON, and the unread remainder would be
                    # parsed as the next request on this connection.
                    self.close_connection = True
                    raise _TooLarge()
                body = strict_loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
                return body

            def _client_ip(self) -> str:
                ip = self.client_address[0]
                if ip.startswith("::ffff:"):
                    ip = ip[7:]
                return ip

            def _is_loopback(self) -> bool:
                return self._client_ip() in ("127.0.0.1", "::1")

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()

            # ----------------------------------------------------------- GET
            def do_GET(self):
                try:
                    self._route_get()
                except Exception as e:
                    # A bug in one endpoint answers that one request with
                    # a 500 — not a dropped connection, and never a dead
                    # handler thread.
                    node._log(f"request handler error on GET "
                              f"{self.path[:80]}: {type(e).__name__}: {e}",
                              "bad")
                    self._send({"error": "internal error"}, 500)
                finally:
                    self._flush()

            def _route_get(self):
                path, _, query = self.path.partition("?")
                if path == "/":
                    # A browser gets the live dashboard; API clients (curl,
                    # wallets, other nodes) get the JSON welcome. Same open
                    # endpoints underneath — the page is only a view.
                    accept = self.headers.get("Accept", "")
                    if "text/html" in accept:
                        return self._raw(dashboard.page(), "text/html; charset=utf-8")
                    return self._send({
                        "name": "kestrel",
                        "network": params.NETWORK_MAGIC,
                        "software": SOFTWARE,
                        "message": "Kestrel node — plain JSON over HTTP, "
                                   "CORS open. Start with /info or /supply. "
                                   "Open this URL in a browser for the live "
                                   "dashboard. Explorers, wallets and apps "
                                   "are yours to build on these endpoints.",
                        "endpoints": [
                            "/info", "/health", "/supply", "/latest?n=15",
                            "/chain?from=H&limit=N", "/block/<height>",
                            "/blockhash/<id>", "/tx/<txid>",
                            "/address/<addr>", "/balance/<addr>",
                            "/utxos/<addr>", "/richlist?n=20",
                            "/search/<q>", "/mempool", "/peers",
                            "POST /tx", "POST /block", "POST /chain",
                            "POST /announce", "POST /checkreach",
                            "POST /peers/add", "POST /mine (loopback)",
                        ],
                    })
                parts = [p for p in path.split("/") if p]
                c = node.chain

                if path == "/chain":
                    # Copy the list under the lock (a pointer copy — fast)
                    # and serialise outside it. Blocks never change once
                    # they are in the chain, so the copy is safe to read,
                    # and a peer downloading the whole ledger no longer
                    # holds up everyone else while it is turned into JSON.
                    start = qint(query, "from", 0, 0, 10**9)
                    limit = qint(query, "limit", 0, 0, 10**9)
                    with node.lock:
                        snap = (c.blocks[start:start + limit] if limit
                                else c.blocks[start:])
                    body = b'{"blocks":[' + b",".join(
                        json.dumps(b.to_dict(),
                                   separators=(",", ":")).encode()
                        for b in snap) + b"]}"
                    return self._raw(body, "application/json")
                if path == "/health":
                    h = node.health()
                    return self._send(h, 200 if h["ok"] else 503)

                with node.lock:
                    if path == "/info":
                        _h, best = node.sync_status()
                        supply = c.circulating_supply()
                        return self._send({
                            "network": "kestrel",
                            "magic": params.NETWORK_MAGIC,
                            "version": params.PROTOCOL_VERSION,
                            "software": SOFTWARE,
                            "node_id": node.node_id,
                            "port": node.port,
                            "height": c.height,
                            "tip": c.tip.block_id,
                            "difficulty": c.difficulty_of(c.tip.target),
                            "total_work": c.total_work(),
                            "supply_feathers": supply,
                            "supply": format_ksl(supply),
                            "max_supply": format_ksl(params.MAX_SUPPLY),
                            "next_reward": format_ksl(c.block_subsidy(c.height + 1)),
                            "mempool": len(c.mempool),
                            "best_height": best,
                            "public_ip": node.public_ip,
                            "reachable": node.reachable,
                            "peers": node.shareable_peers(),
                        })
                    if path == "/supply":
                        return self._send(node._supply_stats())
                    if path == "/latest":
                        n = qint(query, "n", 15, 1, 100)
                        blocks = [node._block_view(b) for b in c.blocks[-n:][::-1]]
                        return self._send({"blocks": blocks, "height": c.height})
                    if len(parts) == 2 and parts[0] == "block":
                        try:
                            h = int(parts[1])
                        except ValueError:
                            return self._send({"error": "bad height"}, 400)
                        if 0 <= h <= c.height:
                            node._reindex()
                            return self._send(node._block_view(c.blocks[h], full=True))
                        return self._send({"error": "no such height"}, 404)
                    if len(parts) == 2 and parts[0] == "blockhash":
                        node._reindex()
                        h = node._block_index.get(parts[1].lower())
                        if h is not None:
                            return self._send(node._block_view(c.blocks[h], full=True))
                        return self._send({"error": "no such block"}, 404)
                    if len(parts) == 2 and parts[0] == "tx":
                        node._reindex()
                        txid = parts[1].lower()
                        hit = node._tx_index.get(txid)
                        if hit:
                            height, tx = hit
                            return self._send(node._tx_view(tx, height))
                        if txid in c.mempool:
                            return self._send(node._tx_view(c.mempool[txid]))
                        return self._send({"error": "no such transaction"}, 404)
                    if len(parts) == 2 and parts[0] == "address":
                        node._reindex()
                        n = qint(query, "n", 50, 1, 500)
                        return self._send(node._address_view(parts[1], n))
                    if len(parts) == 2 and parts[0] == "balance":
                        return self._send(c.balance(parts[1]))
                    if len(parts) == 2 and parts[0] == "utxos":
                        return self._send({"utxos": c.utxos_for(parts[1])})
                    if path == "/richlist":
                        n = qint(query, "n", 20, 1, 100)
                        return self._send({"richlist": node._richlist(n)})
                    if len(parts) == 2 and parts[0] == "search":
                        node._reindex()
                        from urllib.parse import unquote
                        return self._send(node._search(unquote(parts[1])))
                    if path == "/mempool":
                        node._reindex()
                        txs = [node._tx_view(t) for t in c.mempool.values()]
                        return self._send({
                            "txids": list(c.mempool),
                            "transactions": txs,
                            "raw": [t.to_dict() for t in c.mempool.values()],
                            "ttl_seconds": MEMPOOL_TTL,
                        })
                    if path == "/peers":
                        return self._send({
                            "peers": sorted(node.peers),
                            "alive": sorted(node.alive_peers()),
                            "info": {u: dict(i)
                                     for u, i in list(node.peer_info.items())},
                        })
                self._send({"error": "not found"}, 404)

            # ---------------------------------------------------------- POST
            def do_POST(self):
                try:
                    self._route_post()
                except Exception as e:
                    node._log(f"request handler error on POST "
                              f"{self.path[:80]}: {type(e).__name__}: {e}",
                              "bad")
                    self._send({"error": "internal error"}, 500)
                finally:
                    self._flush()

            def _route_post(self):
                cap = MAX_CHAIN_BODY if self.path == "/chain" else MAX_BODY
                try:
                    body = self._body(cap)
                except _TooLarge:
                    return self._send({"error": "request too large"}, 413)
                except (ValueError, UnicodeDecodeError) as e:
                    return self._send({"error": f"bad json: {e}"}, 400)
                c = node.chain
                try:
                    if self.path == "/tx":
                        tx = Transaction.from_dict(body["tx"])
                        with node.lock:
                            # the person at this machine retrying a payment
                            # outranks our own "we gave up on that one"
                            txid = c.add_transaction(
                                tx, allow_readmit=self._is_loopback())
                            try:
                                c.save()
                            except OSError as e:
                                # it is in the mempool and about to be
                                # gossiped; a disk that won't take the
                                # copy must not read as "payment failed"
                                logfile.write(f"could not write the mempool "
                                              f"to disk ({e})", level="warn")
                        node.broadcast("/tx", {"tx": tx.to_dict()})
                        return self._send({"accepted": True, "txid": txid})

                    if self.path == "/block":
                        block = Block.from_dict(body["block"])
                        with node.lock:
                            if block.block_id == c.tip.block_id:
                                # re-gossip of the block we already hold —
                                # normal network echo, nothing to do
                                return self._send({"accepted": False,
                                                   "reason": "already have it"})
                            if block.prev_hash != c.tip.block_id:
                                if block.height > c.height:  # they're ahead
                                    threading.Thread(target=node.sync_once,
                                                     daemon=True).start()
                                # Tell them where we stand. If we're the
                                # lighter chain and they can't be pulled from
                                # (NAT), they use this to push us their chain.
                                return self._send({"accepted": False,
                                                   "reason": "not on tip",
                                                   "height": c.height,
                                                   "total_work": c.total_work()})
                            c.add_block(block)
                        node._log(f"New block {block.height:,} arrived "
                                  f"from the network", "good")
                        node.gossip_block(block)
                        return self._send({"accepted": True,
                                           "block_id": block.block_id})

                    if self.path == "/chain":
                        # The push counterpart to GET /chain. Sync is
                        # otherwise pull-only, which silently fails when the
                        # heavier chain lives behind NAT: we can't fetch from
                        # them, so both sides mine on forever in parallel.
                        # The same work gate and full validation a pulled
                        # chain gets apply here, so an attacker gains
                        # nothing by pushing instead of serving — and the
                        # validation runs with the node lock released.
                        blocks = body.get("blocks")
                        if not isinstance(blocks, list):
                            return self._send({"error": "blocks must be a list"},
                                              400)
                        if len(blocks) > MAX_PUSH_BLOCKS:
                            return self._send({"error": "too many blocks"}, 400)
                        start = _as_int(body.get("from", 0) or 0, -1)
                        if start < 0:
                            return self._send({"error": "bad from"}, 400)
                        with node.lock:
                            height = c.height
                        # `from` must be a height we hold (or the one just
                        # past our tip), and never 0 — the genesis block is
                        # not replaceable. 0 / absent means a whole chain.
                        if start and not 1 <= start <= height + 1:
                            return self._send(
                                {"error": "from out of range",
                                 "height": height}, 409)
                        took, status = node._try_switch(blocks, start)
                        with node.lock:
                            height = c.height
                        return self._send({"accepted": took, "height": height,
                                           "reason": status})

                    if self.path == "/announce":
                        nid = str(body.get("id", ""))[:64]
                        if nid and nid == node.node_id:
                            return self._send({"id": node.node_id,
                                               "your_ip": self._client_ip(),
                                               "peers": node.shareable_peers()})
                        port = _as_int(body.get("port"), 0)
                        if not (0 < port < 65536):
                            return self._send({"error": "bad port"}, 400)
                        ip = self._client_ip()
                        url = f"http://{ip}:{port}"
                        # only remember a joiner we could actually reach back;
                        # a private IP from off-LAN is a dead address
                        if is_routable_host(ip) or node._is_lan_ip(ip):
                            if node.add_peers([url]):
                                node._mark(url, True)
                                node._log(f"New node joined: {url}", "good")
                                # greet back right away: we may be behind
                                # THEIR chain, and now both sides know
                                # each other without waiting for a cycle
                                threading.Thread(target=node._greet_and_sync,
                                                 args=(url,),
                                                 daemon=True).start()
                        return self._send({"id": node.node_id,
                                           "your_ip": ip,
                                           "peers": node.shareable_peers()})

                    if self.path == "/checkreach":
                        # the caller wants to know if IT is reachable: try to
                        # connect back to caller_ip:port and report the result
                        port = _as_int(body.get("port"), 0)
                        if not (0 < port < 65536):
                            return self._send({"error": "bad port"}, 400)
                        ip = self._client_ip()
                        reachable = node._probe(ip, port)
                        return self._send({"reachable": reachable,
                                           "your_ip": ip, "your_port": port})

                    if self.path == "/peers/add":
                        # loose input welcome: "1.2.3.4", "1.2.3.4:4444"
                        # and full URLs all work
                        url = normalize_peer_url(str(body.get("url", ""))[:300])
                        if url:
                            # the local operator may point us anywhere (their
                            # own LAN, a test node); strangers may only hand
                            # us globally-routable addresses
                            if self._is_loopback():
                                node.add_peers([url])
                            else:
                                node.add_network_peers([url])
                            if url in node.peers:
                                # connect NOW — say hello (so they learn our
                                # address too) and sync, instead of leaving
                                # the person staring at "connecting…" until
                                # the next background cycle
                                threading.Thread(
                                    target=node._greet_and_sync, args=(url,),
                                    daemon=True).start()
                        return self._send({"ok": url is not None
                                           and url in node.peers,
                                           "url": url,
                                           "peers": sorted(node.peers)})

                    if self.path == "/mine":
                        if not self._is_loopback():
                            return self._send(
                                {"error": "mining is restricted to local requests"}, 403)
                        count = min(int(body.get("count", 1)), 500)
                        threads = min(int(body.get("threads", 1)), 64)
                        address = body["address"]
                        from .crypto_utils import is_valid_address
                        if not is_valid_address(address):
                            return self._send(
                                {"error": "not a valid Kestrel address"}, 400)
                        mined = node.mine_blocks(address, count, threads)
                        return self._send({"mined": mined, "height": c.height})
                except ValidationError as e:
                    return self._send({"accepted": False, "error": str(e)}, 400)
                except (KeyError, ValueError, TypeError, AttributeError,
                        IndexError, OverflowError, RecursionError) as e:
                    return self._send({"error": f"bad request: "
                                                f"{type(e).__name__}: {e}"[:300]},
                                      400)
                self._send({"error": "not found"}, 404)

        class _QuietServer(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                # A reachable node on a public port is constantly touched by
                # port scanners and peers that hang up mid-request. Those
                # raise OSError (connection reset/aborted, broken pipe,
                # timeout) — ordinary internet noise, not a fault. Swallow
                # them silently; surface only genuine (non-network) errors,
                # one concise line each instead of a scary traceback.
                exc = sys.exc_info()[1]
                if isinstance(exc, OSError):
                    return
                try:
                    node._log("request handler error: "
                              f"{exc.__class__.__name__}: {exc}", "bad")
                except Exception:
                    pass

        server = _QuietServer((self.host, self.port), Handler)
        self._server = server

        shown = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        base = f"http://{shown}:{self.port}"
        # console() prints where there is a console and nothing where
        # there isn't — in a windowed app this all went over whatever the
        # person had open in the terminal behind it
        logfile.console(
            f"\nKestrel node listening on http://{self.host}:{self.port}  "
            f"(height {self.chain.height}, {len(self.peers)} known peers)")
        logfile.console(f"  Dashboard  {base}/   (open in a browser)")
        logfile.console(f"  JSON API   {base}/info  ·  {base}/health")
        logfile.write(f"node listening on {self.host}:{self.port} "
                      f"(height {self.chain.height}, {SOFTWARE})")

        # zero-config networking: LAN + worldwide discovery + seeds + loops
        self.discovery.start()
        if self.discovery.active:
            logfile.console("  LAN auto-discovery on — nodes on this network "
                            "will find each other")
        self.rendezvous.start()
        if self.rendezvous.started:
            logfile.console(
                "  Worldwide auto-discovery on — announcing on the public "
                "DHT so\n  Kestrel nodes anywhere on the internet find "
                "each other")
        threading.Thread(target=self.bootstrap, daemon=True).start()
        threading.Thread(target=self._sync_loop, daemon=True).start()
        threading.Thread(target=self._announce_loop, daemon=True).start()

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            self.discovery.stop()
            self.rendezvous.stop()
            server.server_close()
