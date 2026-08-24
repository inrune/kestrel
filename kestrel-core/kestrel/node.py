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
  GET  /supply               rich chain statistics
  GET  /latest[?n=15]        newest blocks (light view)
  GET  /chain[?from=H]       full blocks from height H (default 0)
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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import params
from .block import Block
from .blockchain import Blockchain, ValidationError
from .transaction import Transaction, COINBASE_TXID
from .wallet import format_ksl
from .miner import assemble_candidate, find_pow
from . import upnp
from . import dashboard
from .discovery import (LanDiscovery, load_seed_nodes, fetch_remote_seeds,
                        new_node_id, get_lan_ip, is_routable_url,
                        is_routable_host, host_of, normalize_peer_url)
from .rendezvous import DhtRendezvous

SYNC_INTERVAL = int(os.environ.get("KESTREL_SYNC_INTERVAL", "15"))
ANNOUNCE_INTERVAL = 120
MAX_PEERS = 40
MAX_FAILS = 6          # consecutive failures before a non-seed peer is dropped
NEW_PEER_FAILS = 3     # ...but a peer that NEVER answered is dropped sooner
SYNC_WORKERS = 8       # parallel peer connections (dead peers can't stall us)
REMAP_INTERVAL = 15 * 60    # re-ask the router for the port (renew/reboot)
RECHECK_INTERVAL = 10 * 60  # re-test reachability (things change)
SEED_REFRESH = 10 * 60      # re-fetch published seed lists while peerless
# (v1.4: connections that just work — loose addresses accepted everywhere,
#  instant mutual handshake on manual add, parallel sync/announce loops,
#  NAT-PMP + UPnP renewal, DHT node caching + fast retry while peerless,
#  and peer-book hygiene so dead addresses can't crowd out live ones)


class Node:
    def __init__(self, chain: Blockchain, host: str = "0.0.0.0",
                 port: int = params.DEFAULT_PORT, peers: list[str] = None):
        self.chain = chain
        self.host, self.port = host, port
        self.node_id = new_node_id()
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
        try:
            with open(self._peers_path) as fh:
                for p in json.load(fh):
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
        os.makedirs(chain.data_dir, exist_ok=True)
        self._save_peers()
        self._stop = threading.Event()
        self._last_remap = 0.0        # when we last asked the router
        self._last_recheck = 0.0      # when we last tested reachability
        self._last_seedfetch = 0.0    # when we last pulled the seed lists
        self.discovery = LanDiscovery(self.port, self.node_id,
                                      on_peer=self._on_lan_peer)
        self.rendezvous = DhtRendezvous(
            self.port, on_peer=self._on_world_peer, on_log=self._log,
            data_dir=chain.data_dir,
            # advertise while reachable or still unknown; stop once we
            # KNOW inbound is blocked (a dead address helps no one)
            should_announce=lambda: self.reachable is not False)
        # lazy indexes (rebuilt when the chain height changes)
        self._index_at = -1
        self._tx_index: dict[str, tuple] = {}     # txid -> (height, tx)
        self._block_index: dict[str, int] = {}    # block_id -> height

    # ------------------------------------------------------------------ log

    def _log(self, msg: str, level: str = "info"):
        print(f"[node] {msg}")
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
        try:
            with open(self._peers_path, "w") as fh:
                json.dump(sorted(self.peers), fh)
        except Exception:
            pass

    def _evict_dead_peer(self) -> bool:
        """Drop one known-dead, non-seed peer to make room for a fresh one.
        Dead addresses must never crowd live newcomers out of a full book."""
        worst, worst_fails = None, 0
        for u in self.peers:
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
        for u in urls:
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
        return self.add_peers([u for u in urls if is_routable_url(str(u))])

    def shareable_peers(self) -> list[str]:
        """The peers we advertise to others — only ones they could reach.

        We hand out addresses the wider internet can actually connect to,
        preferring peers we've confirmed are alive. Our own public URL is
        included when known so newcomers can find us."""
        routable = [u for u in self.peers if is_routable_url(u)]
        alive = [u for u in routable
                 if self.peer_info.get(u, {}).get("alive")]
        out = alive or routable
        mine = self.public_url()
        if mine and self.reachable and mine not in out:
            out = out + [mine]
        return sorted(set(out))

    def _mark(self, url: str, ok: bool, height: int = None):
        info = self.peer_info.setdefault(
            url, {"alive": False, "height": None, "last": 0, "fails": 0})
        if ok:
            info.update(alive=True, last=time.time(), fails=0,
                        ever_alive=True)
            if height is not None:
                info["height"] = height
                if height > self.best_height:
                    self.best_height = height
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
        return [u for u in self.peers
                if self.peer_info.get(u, {}).get("alive")]

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
    def _http_json(method: str, url: str, payload: dict = None,
                   timeout: int = 10):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def broadcast(self, path: str, payload: dict):
        def push(peer):
            try:
                self._http_json("POST", peer + path, payload, timeout=5)
            except Exception:
                pass
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
            if got.get("id") == self.node_id:
                self._drop_self_peer(peer)
                return False
            me = str(got.get("your_ip", ""))
            if me and is_routable_host(me):
                self.public_ip = me       # how the world sees us
            self.add_network_peers(got.get("peers", []))
            return True
        except Exception:
            return False

    def _announce_loop(self):
        while not self._stop.is_set():
            # live peers and seeds always; a handful of untried ones too —
            # in parallel, so a pile of dead addresses can't stall the loop
            targets = set(self.alive_peers()) | (self.seeds & self.peers)
            untried = [u for u in self.peers
                       if u not in targets and u not in self.peer_info]
            targets |= set(untried[:8])
            if targets:
                with ThreadPoolExecutor(
                        max_workers=min(SYNC_WORKERS, len(targets))) as ex:
                    list(ex.map(self.announce_to, targets))
            self._stop.wait(ANNOUNCE_INTERVAL)

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
        if info.get("magic") != params.NETWORK_MAGIC:
            self._mark(peer, False)
            raise ValidationError("not a Kestrel node")
        if info.get("node_id") == self.node_id:
            self._drop_self_peer(peer)
            return "that address is this node itself"
        first_contact = not self.peer_info.get(peer, {}).get("alive")
        self._mark(peer, True, info.get("height"))
        self.add_network_peers(info.get("peers", []))
        if first_contact:   # make sure they know our address too
            threading.Thread(target=self.announce_to, args=(peer,),
                             daemon=True).start()

        with self.lock:
            our_work, our_height = self.chain.total_work(), self.chain.height
        result = f"in sync at block {our_height:,}"
        try:
            their_work = int(info.get("total_work", 0))
        except (TypeError, ValueError):
            their_work = 0
        if their_work > our_work:
            # fast path: they are simply ahead — fetch only what we miss
            data = self._http_json(
                "GET", f"{peer}/chain?from={our_height + 1}", timeout=60)
            with self.lock:
                added = self.chain.extend_with(data.get("blocks", []))
                if added:
                    result = f"caught up to block {self.chain.height:,}"
                    self._log(f"Downloaded {added} block(s) from {peer} — "
                              f"now at block {self.chain.height:,}", "good")
                else:
                    # fork: re-validate their whole chain, adopt if heavier
                    full = self._http_json("GET", peer + "/chain", timeout=120)
                    if self.chain.maybe_replace(full.get("blocks", [])):
                        result = (f"switched to the heavier chain "
                                  f"(block {self.chain.height:,})")
                        self._log(f"Adopted heavier chain from {peer} "
                                  f"(block {self.chain.height:,})", "good")

        # mempool sync: pick up pending transactions we don't have
        try:
            mp = self._http_json("GET", peer + "/mempool", timeout=10)
            with self.lock:
                for raw in mp.get("raw", [])[:500]:
                    try:
                        self.chain.add_transaction(Transaction.from_dict(raw))
                    except (ValidationError, KeyError, ValueError):
                        pass
        except Exception:
            pass
        return result

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
        if not peers:
            return
        with ThreadPoolExecutor(
                max_workers=min(SYNC_WORKERS, len(peers))) as ex:
            list(ex.map(self._sync_quiet, peers))

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

    def _refresh_seeds(self):
        remote = fetch_remote_seeds(self.chain.data_dir)
        if remote:
            self.seeds |= {s.rstrip("/") for s in remote}
            for url in self.add_peers(remote):
                threading.Thread(target=self._greet_and_sync, args=(url,),
                                 daemon=True).start()

    def _sync_loop(self):
        while not self._stop.is_set():
            self.sync_once()
            self._maintain()
            self._stop.wait(SYNC_INTERVAL)

    def public_url(self):
        return f"http://{self.public_ip}:{self.port}" if self.public_ip else None

    def sync_status(self) -> tuple[int, int]:
        """(our height, tallest height seen on the network)."""
        with self.lock:
            h = self.chain.height
        return h, max(self.best_height, h)

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
            return info.get("magic") == params.NETWORK_MAGIC
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
            ip = str(got.get("your_ip", ""))
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
        for peer in list(self.peers):
            self.announce_to(peer)
        self.sync_once()
        n = len(self.alive_peers())
        if n:
            self._log(f"Connected — {n} node(s) reachable, "
                      f"block {self.chain.height:,}", "good")
            self.check_reachability()
        else:
            self._log("No other nodes reached yet. Still searching the "
                      "worldwide directory and your network… (a brand-new "
                      "network needs at least one always-on, reachable node "
                      "for everyone to find — see the README.)")

    # -------------------------------------------------------- chain indexing

    def _reindex(self):
        """Rebuild txid / block-id lookups if the chain has moved."""
        if self._index_at == self.chain.height and self._tx_index:
            return
        tx_idx, blk_idx = {}, {}
        for b in self.chain.blocks:
            blk_idx[b.block_id] = b.height
            for tx in b.transactions:
                tx_idx[tx.txid] = (b.height, tx)
        self._tx_index, self._block_index = tx_idx, blk_idx
        self._index_at = self.chain.height

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

    # ------------------------------------------------------------ enrichment

    def _confirmations(self, height: int) -> int:
        return self.chain.height - height + 1

    def _tx_view(self, tx: Transaction, block_height=None) -> dict:
        c = self.chain
        confirmed = block_height is not None
        outs = []
        for vout, o in enumerate(tx.outputs):
            outs.append({
                "n": vout,
                "address": o.address,
                "amount": o.amount,
                "amount_ksl": format_ksl(o.amount),
                # only meaningful once confirmed; a mempool tx's outputs
                # aren't in the UTXO set yet but aren't "spent" either
                "spent": confirmed and (tx.txid, vout) not in c.utxos,
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
            "txid": tx.txid,
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
        circ = c.circulating_supply()
        tx_count = sum(len(b.transactions) for b in c.blocks)
        # average interval over the most recent blocks
        recent = [b.timestamp for b in c.blocks[-21:]]
        avg = None
        if len(recent) >= 2:
            avg = round((recent[-1] - recent[0]) / (len(recent) - 1), 1)
        halving_at = ((c.height // params.HALVING_INTERVAL) + 1) * params.HALVING_INTERVAL
        return {
            "network": "kestrel",
            "magic": params.NETWORK_MAGIC,
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
            "sync_target": max(self.best_height, c.height),
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

    def _address_view(self, addr: str) -> dict:
        from .crypto_utils import is_valid_address
        c = self.chain
        if not is_valid_address(addr):
            return {"address": addr, "valid": False}
        bal = c.balance(addr)
        utxos = c.utxos_for(addr, spendable_only=False)
        received = sent = 0
        history = []
        for b in c.blocks:
            for tx in b.transactions:
                delta = 0
                for o in tx.outputs:
                    if o.address == addr:
                        delta += o.amount
                        received += o.amount
                if not tx.is_coinbase:
                    for i in tx.inputs:
                        got = self._resolve_output(i.txid, i.vout)
                        if got and got[1] == addr:
                            delta -= got[0]
                            sent += got[0]
                if delta != 0:
                    history.append({
                        "txid": tx.txid,
                        "height": b.height,
                        "timestamp": tx.timestamp,
                        "delta": delta,
                        "delta_ksl": format_ksl(delta),
                        "coinbase": tx.is_coinbase,
                    })
        history.reverse()
        return {
            "address": addr,
            "valid": True,
            "confirmed": bal["confirmed"],
            "confirmed_ksl": format_ksl(bal["confirmed"]),
            "spendable": bal["spendable"],
            "spendable_ksl": format_ksl(bal["spendable"]),
            "received": received,
            "received_ksl": format_ksl(received),
            "sent": sent,
            "sent_ksl": format_ksl(sent),
            "tx_count": len(history),
            "utxos": [{**u, "amount_ksl": format_ksl(u["amount"])} for u in utxos],
            "history": history[:50],
        }

    def _richlist(self, n: int = 20) -> list[dict]:
        totals: dict[str, int] = {}
        for u in self.chain.utxos.values():
            totals[u.address] = totals.get(u.address, 0) + u.amount
        ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:n]
        circ = self.chain.circulating_supply() or 1
        return [{"address": a, "amount": v, "amount_ksl": format_ksl(v),
                 "pct": round(v / circ * 100, 4)} for a, v in ranked]

    def _search(self, q: str) -> dict:
        q = q.strip()
        c = self.chain
        if q.isdigit():
            h = int(q)
            if 0 <= h <= c.height:
                return {"type": "height", "value": h}
        if len(q) == 64:
            ql = q.lower()
            if ql in self._block_index:
                return {"type": "block", "value": ql}
            if ql in self._tx_index:
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
            self.broadcast("/block", {"block": block.to_dict()})
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

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # drop idle/half-open connections so scanners and slow clients
            # on a public port can't tie up a thread forever
            timeout = 30

            def log_message(self, *args):
                pass

            # --------------------------------------------------- low-level IO
            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            def _raw(self, body: bytes, content_type: str, status=200):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def _send(self, obj, status=200):
                self._raw(json.dumps(obj).encode(), "application/json", status)

            def _body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    length = 0
                # cap the body: nothing legitimate exceeds a few blocks
                length = max(0, min(length, 4 * params.MAX_BLOCK_SIZE))
                return json.loads(self.rfile.read(length) or b"{}")

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
                        "message": "Kestrel node — plain JSON over HTTP, "
                                   "CORS open. Start with /info or /supply. "
                                   "Open this URL in a browser for the live "
                                   "dashboard. Explorers, wallets and apps "
                                   "are yours to build on these endpoints.",
                        "endpoints": [
                            "/info", "/supply", "/latest?n=15",
                            "/chain?from=H", "/block/<height>",
                            "/blockhash/<id>", "/tx/<txid>",
                            "/address/<addr>", "/balance/<addr>",
                            "/utxos/<addr>", "/richlist?n=20",
                            "/search/<q>", "/mempool", "/peers",
                            "POST /tx", "POST /block", "POST /announce",
                            "POST /checkreach", "POST /peers/add",
                            "POST /mine (loopback)",
                        ],
                    })
                parts = [p for p in path.split("/") if p]
                c = node.chain
                with node.lock:
                    node._reindex()
                    if path == "/info":
                        return self._send({
                            "network": "kestrel",
                            "magic": params.NETWORK_MAGIC,
                            "version": params.PROTOCOL_VERSION,
                            "node_id": node.node_id,
                            "port": node.port,
                            "height": c.height,
                            "tip": c.tip.block_id,
                            "difficulty": c.difficulty_of(c.tip.target),
                            "total_work": c.total_work(),
                            "supply_feathers": c.circulating_supply(),
                            "supply": format_ksl(c.circulating_supply()),
                            "max_supply": format_ksl(params.MAX_SUPPLY),
                            "next_reward": format_ksl(c.block_subsidy(c.height + 1)),
                            "mempool": len(c.mempool),
                            "best_height": max(node.best_height, c.height),
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
                    if path == "/chain":
                        start = qint(query, "from", 0, 0, 10**9)
                        return self._send({
                            "blocks": [b.to_dict() for b in c.blocks[start:]]
                        })
                    if len(parts) == 2 and parts[0] == "block":
                        try:
                            h = int(parts[1])
                        except ValueError:
                            return self._send({"error": "bad height"}, 400)
                        if 0 <= h <= c.height:
                            return self._send(node._block_view(c.blocks[h], full=True))
                        return self._send({"error": "no such height"}, 404)
                    if len(parts) == 2 and parts[0] == "blockhash":
                        h = node._block_index.get(parts[1].lower())
                        if h is not None:
                            return self._send(node._block_view(c.blocks[h], full=True))
                        return self._send({"error": "no such block"}, 404)
                    if len(parts) == 2 and parts[0] == "tx":
                        hit = node._tx_index.get(parts[1].lower())
                        if hit:
                            height, tx = hit
                            return self._send(node._tx_view(tx, height))
                        if parts[1] in c.mempool:
                            return self._send(node._tx_view(c.mempool[parts[1]]))
                        return self._send({"error": "no such transaction"}, 404)
                    if len(parts) == 2 and parts[0] == "address":
                        return self._send(node._address_view(parts[1]))
                    if len(parts) == 2 and parts[0] == "balance":
                        return self._send(c.balance(parts[1]))
                    if len(parts) == 2 and parts[0] == "utxos":
                        return self._send({"utxos": c.utxos_for(parts[1])})
                    if path == "/richlist":
                        n = qint(query, "n", 20, 1, 100)
                        return self._send({"richlist": node._richlist(n)})
                    if len(parts) == 2 and parts[0] == "search":
                        return self._send(node._search(parts[1]))
                    if path == "/mempool":
                        txs = [node._tx_view(t) for t in c.mempool.values()]
                        return self._send({
                            "txids": list(c.mempool),
                            "transactions": txs,
                            "raw": [t.to_dict() for t in c.mempool.values()],
                        })
                    if path == "/peers":
                        return self._send({
                            "peers": sorted(node.peers),
                            "alive": sorted(node.alive_peers()),
                            "info": {u: {k: v for k, v in i.items()}
                                     for u, i in node.peer_info.items()},
                        })
                self._send({"error": "not found"}, 404)

            # ---------------------------------------------------------- POST
            def do_POST(self):
                try:
                    body = self._body()
                except json.JSONDecodeError:
                    return self._send({"error": "bad json"}, 400)
                c = node.chain
                try:
                    if self.path == "/tx":
                        tx = Transaction.from_dict(body["tx"])
                        with node.lock:
                            txid = c.add_transaction(tx)
                            c.save()
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
                                return self._send({"accepted": False,
                                                   "reason": "not on tip"})
                            c.add_block(block)
                        node._log(f"New block {block.height:,} arrived "
                                  f"from the network", "good")
                        node.broadcast("/block", {"block": block.to_dict()})
                        return self._send({"accepted": True,
                                           "block_id": block.block_id})

                    if self.path == "/announce":
                        nid = str(body.get("id", ""))
                        if nid and nid == node.node_id:
                            return self._send({"id": node.node_id,
                                               "your_ip": self._client_ip(),
                                               "peers": node.shareable_peers()})
                        try:
                            port = int(body["port"])
                        except (KeyError, ValueError, TypeError):
                            return self._send({"error": "bad port"}, 400)
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
                        try:
                            port = int(body["port"])
                        except (KeyError, ValueError, TypeError):
                            return self._send({"error": "bad port"}, 400)
                        if not (0 < port < 65536):
                            return self._send({"error": "bad port"}, 400)
                        ip = self._client_ip()
                        reachable = node._probe(ip, port)
                        return self._send({"reachable": reachable,
                                           "your_ip": ip, "your_port": port})

                    if self.path == "/peers/add":
                        # loose input welcome: "1.2.3.4", "1.2.3.4:4444"
                        # and full URLs all work
                        url = normalize_peer_url(str(body.get("url", "")))
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
                except (KeyError, ValueError, TypeError, AttributeError) as e:
                    return self._send({"error": f"bad request: {e}"}, 400)
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
        print(f"\nKestrel node listening on http://{self.host}:{self.port}  "
              f"(height {self.chain.height}, {len(self.peers)} known peers)")
        print(f"  Dashboard  {base}/   (open in a browser)")
        print(f"  JSON API   {base}/info  ·  {base}/supply")

        # zero-config networking: LAN + worldwide discovery + seeds + loops
        self.discovery.start()
        if self.discovery.active:
            print("  LAN auto-discovery on — nodes on this network "
                  "will find each other")
        self.rendezvous.start()
        if self.rendezvous.started:
            print("  Worldwide auto-discovery on — announcing on the public "
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
