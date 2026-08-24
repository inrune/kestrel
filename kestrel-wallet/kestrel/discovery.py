"""
Kestrel zero-config peer discovery.

Two mechanisms, both automatic:

1. LAN discovery — every node shouts a tiny UDP broadcast ("I am a Kestrel
   node on port X") every few seconds and listens for the same from others.
   Two computers on the same Wi-Fi/router find each other with no setup at
   all, exactly like the early Bitcoin experience on a local network.

2. Seed list — for the public internet, nodes read entry-point URLs from
   (in order) params.SEED_NODES, the KESTREL_SEEDS environment variable
   (comma-separated), and a plain-text `seeds.txt` (one URL per line,
   next to the app / in the data directory). Once one seed answers, peer
   exchange takes over and the rest of the mesh is learned automatically.

Every packet and API handshake carries a random node id so a node never
mistakes its own echo for a new peer.
"""

import ipaddress
import json
import os
import socket
import threading
import urllib.parse
import urllib.request
import uuid

from . import params

DISCOVERY_PORT = getattr(params, "DISCOVERY_PORT", params.DEFAULT_PORT + 100)
BEACON_INTERVAL = 8.0          # seconds between broadcasts
PACKET_PREFIX = b"KSLDISC1"    # guards against unrelated UDP noise

# For local testing / a purely-LAN network, set KESTREL_SHARE_LOCAL=1 so
# private and loopback addresses count as shareable. In production this is
# off: a node never tells the wider internet about a 192.168.x.x or
# 127.0.0.1 address that no one outside its own network could reach.
SHARE_LOCAL = os.environ.get("KESTREL_SHARE_LOCAL", "0") == "1"


def new_node_id() -> str:
    return uuid.uuid4().hex


def is_routable_host(host: str) -> bool:
    """True if `host` is an address the wider internet could actually reach.

    Hostnames (anything that isn't a bare IP — e.g. a seed's DNS name) are
    assumed routable. IP literals are routable only if they are global
    unicast: private ranges (10/8, 172.16/12, 192.168/16), loopback,
    link-local and the rest are rejected, because handing those out to a
    node on a different network just produces dead, unreachable peers.
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True            # a DNS name — let it through
    if SHARE_LOCAL and (ip.is_private or ip.is_loopback):
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def host_of(url: str) -> str:
    """Extract the bare host from an http URL."""
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""


def is_routable_url(url: str) -> bool:
    return is_routable_host(host_of(url))


def normalize_peer_url(text: str):
    """Canonical peer URL from loose input, or None.

    People share addresses in every shape — "12.34.56.78",
    "12.34.56.78:4444", "http://12.34.56.78:4444/", "kestrel.example.org"
    — and every one of them should just work, in the CLI, in seeds.txt
    and in the apps' connect boxes. Missing scheme and port are filled
    in with http and the default port."""
    text = (text or "").strip().rstrip("/")
    if not text or " " in text:
        return None
    if "://" not in text:
        text = "http://" + text
    try:
        u = urllib.parse.urlparse(text)
        port = u.port or params.DEFAULT_PORT
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname
    if ":" in host:                    # bare IPv6 needs brackets in a URL
        host = f"[{host}]"
    return f"{u.scheme}://{host}:{port}"


def get_lan_ip() -> str:
    """Best-effort local LAN IP (no traffic is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def load_seed_nodes(data_dir: str = None) -> list[str]:
    """Merge seeds from params, $KESTREL_SEEDS and seeds.txt files."""
    seeds: list[str] = []

    def add(u: str):
        u = normalize_peer_url(u)
        if u and u not in seeds:
            seeds.append(u)

    for s in params.SEED_NODES:
        add(s)
    for s in os.environ.get("KESTREL_SEEDS", "").split(","):
        add(s)
    candidates = []
    if data_dir:
        candidates.append(os.path.join(data_dir, "seeds.txt"))
        candidates.append(os.path.join(os.path.dirname(data_dir.rstrip("/\\")),
                                       "seeds.txt"))
    candidates.append(os.path.join(os.getcwd(), "seeds.txt"))
    for path in candidates:
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.split("#", 1)[0]
                    add(line)
        except OSError:
            continue
    return seeds


def fetch_remote_seeds(data_dir: str = None) -> list[str]:
    """Fetch seed URLs from the published seed lists (params.SEED_LIST_URLS
    and the KESTREL_SEED_LISTS env var). Successful fetches are cached in
    the data directory so offline launches still know the network.
    Network calls happen here, so call this from a background thread."""
    lists = list(getattr(params, "SEED_LIST_URLS", []))
    lists += [u for u in os.environ.get("KESTREL_SEED_LISTS", "").split(",")
              if u.strip()]
    seeds: list[str] = []

    def add(u: str):
        u = normalize_peer_url(u)
        if u and u not in seeds:
            seeds.append(u)

    fetched_any = False
    for url in lists:
        url = url.strip()
        if not url:
            continue
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                text = r.read(65536).decode("utf-8", "replace")
            fetched_any = True
            for line in text.splitlines():
                add(line.split("#", 1)[0])
        except Exception:
            continue

    cache = os.path.join(data_dir, "seeds-cache.txt") if data_dir else None
    if cache and fetched_any:
        try:
            with open(cache, "w") as fh:
                fh.write("\n".join(seeds) + "\n")
        except OSError:
            pass
    elif cache and not seeds:      # offline: fall back to the last good list
        try:
            with open(cache) as fh:
                for line in fh:
                    add(line.split("#", 1)[0])
        except OSError:
            pass
    return seeds


class LanDiscovery:
    """UDP-broadcast beacon + listener. Calls `on_peer(url, node_id)` for
    every *other* Kestrel node heard on the local network."""

    def __init__(self, node_port: int, node_id: str, on_peer=None):
        self.node_port = node_port
        self.node_id = node_id
        self.on_peer = on_peer or (lambda url, nid: None)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.active = False

    # ------------------------------------------------------------ lifecycle

    def start(self):
        if self.active:
            return
        try:
            self._rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:  # several nodes on one machine may share the port
                self._rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            self._rx.bind(("", DISCOVERY_PORT))
            self._rx.settimeout(1.0)
        except OSError:
            return  # firewall / port busy: LAN discovery just stays off
        self.active = True
        for fn in (self._listen_loop, self._beacon_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self.active = False
        try:
            self._rx.close()
        except Exception:
            pass

    # -------------------------------------------------------------- beacons

    def _payload(self) -> bytes:
        return PACKET_PREFIX + json.dumps({
            "magic": params.NETWORK_MAGIC,
            "port": self.node_port,
            "id": self.node_id,
        }).encode()

    def _beacon_loop(self):
        payload = self._payload()
        while not self._stop.is_set():
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                tx.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
                tx.sendto(payload, ("127.0.0.1", DISCOVERY_PORT))
            except OSError:
                pass
            finally:
                tx.close()
            self._stop.wait(BEACON_INTERVAL)

    def _listen_loop(self):
        while not self._stop.is_set():
            try:
                data, (ip, _port) = self._rx.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                return
            url, nid = self.parse_packet(data, ip, self.node_id)
            if url:
                try:
                    self.on_peer(url, nid)
                except Exception:
                    pass

    # ------------------------------------------------------------- parsing

    @staticmethod
    def parse_packet(data: bytes, sender_ip: str, own_id: str):
        """Validate a discovery packet. Returns (url, node_id) or (None, None)."""
        if not data.startswith(PACKET_PREFIX):
            return None, None
        try:
            msg = json.loads(data[len(PACKET_PREFIX):])
        except (ValueError, UnicodeDecodeError):
            return None, None
        if msg.get("magic") != params.NETWORK_MAGIC:
            return None, None
        nid = str(msg.get("id", ""))
        if not nid or nid == own_id:
            return None, None      # our own echo
        try:
            port = int(msg["port"])
        except (KeyError, ValueError, TypeError):
            return None, None
        if not (0 < port < 65536):
            return None, None
        return f"http://{sender_ip}:{port}", nid
