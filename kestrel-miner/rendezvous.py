"""
Kestrel worldwide rendezvous — automatic peer discovery across the
internet, with zero servers and zero setup.

The problem every peer-to-peer network has on day one: two strangers in
different countries both run the app, but neither knows the other's
address. Bitcoin's first releases solved it by meeting in an IRC
channel; later came DNS seeds. Kestrel uses the modern equivalent: the
public mainline BitTorrent DHT — a free, decentralized directory of
tens of millions of nodes that has run continuously since 2005 and
belongs to no one.

Every Kestrel node:
  1. announces "a Kestrel node is at <my-ip>:<my-port>" under a fixed
     key (the SHA-1 of the network magic) on the DHT, and
  2. looks up that same key to find everyone else who announced.

The DHT stores the announcer's public IP as seen from outside, so this
works from home connections (combined with UPnP port-opening from
upnp.py, most home nodes are directly reachable too). Everything is
best-effort: on networks where UDP is blocked, the node quietly falls
back to seed lists, LAN discovery and manual connections.

Implementation: a minimal, self-contained slice of BEP-5 — bencoding
plus the KRPC queries get_peers / announce_peer over UDP, iterating
from the well-known bootstrap routers toward the nodes closest to our
key. Standard library only.

Override the bootstrap routers with KESTREL_DHT_BOOTSTRAP
("host:port,host:port"); disable entirely with KESTREL_DHT=0.
"""

import hashlib
import json
import os
import socket
import threading
import time

from . import params

BOOTSTRAP = (
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
    ("dht.libtorrent.org", 25401),
    ("dht.aelitis.com", 6881),
    ("router.silotis.us", 6881),
)
INFO_HASH = hashlib.sha1(
    ("kestrel-net:" + params.NETWORK_MAGIC).encode()).digest()
QUERY_TIMEOUT = 0.8         # seconds per UDP round-trip
MAX_QUERIES = 64            # per lookup run
RUN_INTERVAL = 300          # seconds between runs (announce TTL is ~30 min)
RETRY_INTERVAL = 75         # much sooner retry while nothing has been found
CACHE_NODES = 32            # good DHT nodes remembered between runs/launches


# ------------------------------------------------------------- bencoding

def bencode(x) -> bytes:
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, str):
        return bencode(x.encode())
    if isinstance(x, list):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        items = [(k.encode() if isinstance(k, str) else k, v)
                 for k, v in x.items()]
        items.sort(key=lambda kv: kv[0])
        return (b"d" + b"".join(bencode(k) + bencode(v) for k, v in items)
                + b"e")
    raise TypeError(f"cannot bencode {type(x)}")


def _bdecode_at(data: bytes, i: int):
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode_at(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode_at(data, i)
            v, i = _bdecode_at(data, i)
            out[k] = v
        return out, i + 1
    j = data.index(b":", i)
    ln = int(data[i:j])
    return data[j + 1:j + 1 + ln], j + 1 + ln


def bdecode(data: bytes):
    v, _ = _bdecode_at(data, 0)
    return v


# --------------------------------------------------------------- helpers

def _compact_to_peers(values) -> set:
    peers = set()
    for v in values or []:
        if isinstance(v, bytes) and len(v) == 6:
            ip = socket.inet_ntoa(v[:4])
            port = int.from_bytes(v[4:6], "big")
            if port > 0 and ip != "0.0.0.0":
                peers.add((ip, port))
    return peers


def _compact_to_nodes(blob) -> list:
    nodes = []
    if isinstance(blob, bytes):
        for i in range(0, len(blob) - 25, 26):
            nid = blob[i:i + 20]
            ip = socket.inet_ntoa(blob[i + 20:i + 24])
            port = int.from_bytes(blob[i + 24:i + 26], "big")
            if port > 0:
                nodes.append((nid, ip, port))
    return nodes


def bootstrap_addrs() -> list:
    override = os.environ.get("KESTREL_DHT_BOOTSTRAP", "").strip()
    hosts = []
    if override:
        for part in override.split(","):
            host, _, port = part.strip().rpartition(":")
            try:
                hosts.append((host, int(port)))
            except ValueError:
                continue
    else:
        hosts = list(BOOTSTRAP)
    addrs = []
    for host, port in hosts:
        try:
            addrs.append((socket.gethostbyname(host), port))
        except OSError:
            continue
    return addrs


class DhtRendezvous:
    """Announce + discover Kestrel nodes on the mainline BitTorrent DHT.

    Calls on_peer("http://ip:port") for every address found. All network
    work happens on a background thread; failures are silent and the
    next run simply tries again.
    """

    def __init__(self, tcp_port: int, on_peer=None, on_log=None,
                 data_dir: str = None, should_announce=None):
        self.tcp_port = tcp_port
        self.on_peer = on_peer or (lambda url: None)
        self.on_log = on_log or (lambda msg, lvl="info": None)
        # should_announce() -> bool: the node tells us whether advertising
        # our address is useful (False once we KNOW inbound is blocked —
        # announcing a dead address only pollutes the directory)
        self.should_announce = should_announce or (lambda: True)
        self.node_id = os.urandom(20)
        self.active = False          # a run has completed successfully
        self.started = False         # background thread is running
        self.last_run = 0
        self.last_found = 0
        self._cache_path = (os.path.join(data_dir, "dht-nodes.json")
                            if data_dir else None)
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ control

    def start(self):
        if self._thread or os.environ.get("KESTREL_DHT", "1") == "0":
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.started = True

    def stop(self):
        self._stop.set()
        self._kick.set()

    def kick(self):
        """Ask for a lookup run now (used when the node has no peers)."""
        self._kick.set()

    def _loop(self):
        first = True
        while not self._stop.is_set():
            found = 0
            try:
                found = self.run_once()
                if first and found:
                    self.on_log(f"Worldwide directory: found {found} "
                                f"Kestrel node(s) across the internet",
                                "good")
                first = False
            except Exception:
                pass
            # nothing found yet -> retry much sooner; a lonely first node
            # should notice a second one within a minute, not five
            wait = RUN_INTERVAL if found else RETRY_INTERVAL
            self._kick.clear()
            self._kick.wait(wait)

    # ------------------------------------------------------- node caching

    def _load_cached_nodes(self) -> list:
        """DHT nodes that answered close to our key last time — starting
        from them makes every run after the first fast, and keeps the
        rendezvous working even if the bootstrap routers are busy."""
        if not self._cache_path:
            return []
        try:
            with open(self._cache_path) as fh:
                raw = json.load(fh)
            return [(str(ip), int(port)) for ip, port in raw][:CACHE_NODES]
        except Exception:
            return []

    def _save_cached_nodes(self, nodes):
        if not self._cache_path:
            return
        try:
            with open(self._cache_path, "w") as fh:
                json.dump(nodes[:CACHE_NODES], fh)
        except Exception:
            pass

    # ------------------------------------------------------------- lookup

    def _query(self, sock, addr, q: str, args: dict):
        tid = os.urandom(2)
        msg = {b"t": tid, b"y": b"q", b"q": q.encode(),
               b"a": {b"id": self.node_id, **args}}
        try:
            sock.sendto(bencode(msg), addr)
            while True:
                data, src = sock.recvfrom(2048)
                try:
                    r = bdecode(data)
                except Exception:
                    continue
                if r.get(b"t") == tid and r.get(b"y") == b"r":
                    return r.get(b"r", {})
                if r.get(b"t") == tid:      # error or unexpected
                    return None
        except OSError:
            return None

    def run_once(self) -> int:
        """One announce+discover cycle. Returns how many peers were found."""
        cached = self._load_cached_nodes()
        addrs = bootstrap_addrs()
        if not addrs and not cached:
            return 0
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(QUERY_TIMEOUT)
        target = int.from_bytes(INFO_HASH, "big")
        peers: set = set()
        tokens: list = []            # (distance, addr, token)
        queried: set = set()
        # candidates: (xor distance to key, ip, port) — cached close nodes
        # first, bootstrap routers as the fallback entry point
        cands = [(0, ip, port) for ip, port in cached]
        cands += [(1, ip, port) for ip, port in addrs]
        queries = 0
        try:
            while cands and queries < MAX_QUERIES:
                cands.sort(key=lambda t: t[0])
                dist, ip, port = cands.pop(0)
                if (ip, port) in queried:
                    continue
                queried.add((ip, port))
                queries += 1
                r = self._query(sock, (ip, port), "get_peers",
                                {b"info_hash": INFO_HASH})
                if not r:
                    continue
                peers |= _compact_to_peers(r.get(b"values"))
                token = r.get(b"token")
                if isinstance(token, bytes) and token:
                    nid = r.get(b"id", b"")
                    d = (int.from_bytes(nid, "big") ^ target) if len(nid) == 20 else dist
                    tokens.append((d, (ip, port), token))
                for nid, nip, nport in _compact_to_nodes(r.get(b"nodes")):
                    if (nip, nport) not in queried:
                        cands.append(
                            (int.from_bytes(nid, "big") ^ target, nip, nport))
            # store our own address with the nodes closest to the key —
            # but only while being found is useful: once the node KNOWS
            # inbound connections are blocked, advertising the address
            # would only hand other people a peer that never answers
            tokens.sort(key=lambda t: t[0])
            announce = True
            try:
                announce = bool(self.should_announce())
            except Exception:
                pass
            if announce:
                for _d, addr, token in tokens[:8]:
                    self._query(sock, addr, "announce_peer",
                                {b"info_hash": INFO_HASH,
                                 b"port": self.tcp_port,
                                 b"token": token,
                                 b"implied_port": 0})
            self._save_cached_nodes([list(a) for _d, a, _t in tokens])
        finally:
            sock.close()
        self.active = True
        self.last_run = time.time()
        self.last_found = len(peers)
        for ip, port in peers:
            try:
                self.on_peer(f"http://{ip}:{port}")
            except Exception:
                pass
        return len(peers)
