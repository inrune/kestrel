#!/usr/bin/env python3
"""
Kestrel Wallet — the desktop wallet application.

Zero setup: on first launch it creates your wallet (and shows the backup
key once), finds a Kestrel node on this machine — or quietly runs its own —
and connects. Sidebar views: Overview, Send, Receive, Transactions,
Contacts. Keys are created and every transaction is signed locally; the
network only ever sees the finished signature.

Requires Python 3.10+ with tkinter and the `ecdsa` package.
"""

import csv
import errno
import ipaddress
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (_HERE, os.path.abspath(os.path.join(_HERE, "..", ".."))):
    if os.path.isdir(os.path.join(cand, "kestrel")):
        sys.path.insert(0, cand)
        break

from kestrel import params, __version__ as KVER              # noqa: E402
from kestrel.wallet import Wallet, format_ksl, parse_ksl      # noqa: E402
from kestrel.crypto_utils import is_valid_address, private_to_wif  # noqa: E402
from kestrel.blockchain import Blockchain, ValidationError    # noqa: E402
from kestrel.node import Node                                 # noqa: E402
from kestrel.discovery import load_seed_nodes, get_lan_ip     # noqa: E402


def _enable_dpi():
    """Crisp text on Windows high-DPI screens. Call before Tk()."""
    if sys.platform == "win32":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_dpi()

import tkinter as tk                                          # noqa: E402
from tkinter import ttk, messagebox, filedialog               # noqa: E402
from tkinter import font as tkfont                            # noqa: E402

WALLET_FILE = os.path.join(_HERE, "kestrel-wallet.json")
BOOK_FILE = os.path.join(_HERE, "kestrel-address-book.json")
SETTINGS_FILE = os.path.join(_HERE, "wallet-settings.json")
DATA_DIR = os.path.join(_HERE, "kestrel-data")
PORTS = tuple(params.DEFAULT_PORT + i for i in range(10))

# ------------------------------------------------------------------ palette
DUSK, DUSK2, DUSK3, SPOT = "#1B212C", "#222A38", "#2A3444", "#10141B"
RAIL = "#141924"
BUFF, MUTED, FAINT = "#EAE1CE", "#A79F8D", "#736c5e"
RUFOUS, RUFOUS_HI = "#C4552A", "#DB6636"
GREEN, RED, SLATE = "#5FA46A", "#C15b4b", "#8CA7C4"
AMBER = "#D9A441"
ZEBRA = "#131822"
HOVER = "#33405A"

# Fonts are resolved per-platform once a Tk root exists (see _resolve_fonts).
SANS = ("Segoe UI", 10)
SANS_B = ("Segoe UI", 10, "bold")
SANS_9 = ("Segoe UI", 9)
SANS_9B = ("Segoe UI", 9, "bold")
TINY = ("Segoe UI", 8)
TINY_B = ("Segoe UI", 8, "bold")
MICRO_B = ("Segoe UI", 7, "bold")
TITLE = ("Segoe UI", 16, "bold")
BRAND = ("Segoe UI", 14, "bold")
MONO = ("Consolas", 10)
MONO_8 = ("Consolas", 8)
MONO_9 = ("Consolas", 9)
MONO_11B = ("Consolas", 11, "bold")
MONO_12 = ("Consolas", 12)
MONO_13 = ("Consolas", 13)
MONO_26B = ("Consolas", 26, "bold")


def _resolve_fonts(root):
    """Pick the best available family per platform and rebuild font tuples."""
    global SANS, SANS_B, SANS_9, SANS_9B, TINY, TINY_B, MICRO_B, TITLE, BRAND
    global MONO, MONO_8, MONO_9, MONO_11B, MONO_12, MONO_13, MONO_26B
    try:
        fams = set(tkfont.families(root))
    except Exception:
        return
    sans = next((f for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue",
                             "DejaVu Sans", "Arial") if f in fams),
                "TkDefaultFont")
    mono = next((f for f in ("Cascadia Mono", "Consolas", "SF Mono", "Menlo",
                             "DejaVu Sans Mono", "Courier New") if f in fams),
                "TkFixedFont")
    SANS = (sans, 10)
    SANS_B = (sans, 10, "bold")
    SANS_9 = (sans, 9)
    SANS_9B = (sans, 9, "bold")
    TINY = (sans, 8)
    TINY_B = (sans, 8, "bold")
    MICRO_B = (sans, 7, "bold")
    TITLE = (sans, 16, "bold")
    BRAND = (sans, 14, "bold")
    MONO = (mono, 10)
    MONO_8 = (mono, 8)
    MONO_9 = (mono, 9)
    MONO_11B = (mono, 11, "bold")
    MONO_12 = (mono, 12)
    MONO_13 = (mono, 13)
    MONO_26B = (mono, 26, "bold")


# ------------------------------------------------------------------ helpers
def http_json(method, url, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def kestrel_at(url, timeout=1.5):
    try:
        return http_json("GET", url + "/info",
                         timeout=timeout).get("magic") == params.NETWORK_MAGIC
    except Exception:
        return False


def port_free(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as fh:
            return dict(json.load(fh))
    except Exception:
        return {}


def save_settings(d: dict):
    try:
        with open(SETTINGS_FILE, "w") as fh:
            json.dump(d, fh, indent=2)
    except Exception:
        pass


def open_folder(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)                       # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def mid_ellipsis(s: str, keep: int = 10) -> str:
    if len(s) <= keep * 2 + 1:
        return s
    return s[:keep] + "…" + s[-keep:]


def invite_text(url: str) -> str:
    """A ready-to-paste message that gets a friend onto the network."""
    return (
        "Join my Kestrel (KSL) network — takes a minute:\n"
        "\n"
        "1. Get Kestrel Miner (or Kestrel Wallet) and open it —\n"
        "   it runs a node for you automatically.\n"
        f"2. In the app: Network ▸ “Add a node by address” ▸ paste\n"
        f"   {url}\n"
        "   (in the wallet: Settings ▸ Connect to a network node)\n"
        "3. Press Connect. Done — the mesh remembers itself, and nodes\n"
        "   pass peers to each other, so the network spreads on its own\n"
        "   from here.\n"
        "\n"
        "Press “Start mining” and every block you find pays you directly."
    )


# --------------------------------------------------- connection diagnosis
# Turn a failed "connect to a node" into a plain-language reason the user can
# act on, instead of a cryptic "unreachable (URLError)".

_BLOCKED_ERRNOS = {getattr(errno, n) for n in
                   ("ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EHOSTDOWN",
                    "WSAETIMEDOUT", "WSAEHOSTUNREACH", "WSAENETUNREACH")
                   if hasattr(errno, n)}


def parse_node_url(text):
    """(url, host, port) from loose user input, or (None, None, None)."""
    text = (text or "").strip()
    if not text:
        return None, None, None
    if "://" not in text:
        text = "http://" + text
    try:
        u = urlparse(text)
    except Exception:
        return None, None, None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None, None, None
    port = u.port or params.DEFAULT_PORT
    return f"{u.scheme}://{u.hostname}:{port}", u.hostname, port


def _addr_is_private(host):
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def diagnose_node(text, timeout=4):
    """Plain-language reason a node address can't be reached, or None if it
    looks reachable. Does a quick socket probe — call off the UI thread."""
    url, host, port = parse_node_url(text)
    if not url:
        return ("That doesn't look like a node address. Use something like "
                f"http://12.34.56.78:{params.DEFAULT_PORT}")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return (f"“{host}” couldn't be found. Check the spelling — or if it's "
                "a name, it doesn't exist on the internet.")
    blocked = (f"Can't reach {host}:{port}. Almost always the other "
               "computer's firewall or router is blocking incoming "
               "connections (or its node isn't running). Ask them to press "
               "“Fix my connection” in their Kestrel Miner — or, easier, give "
               "them YOUR address and let them connect to you instead. Once "
               "either side connects, you're both on the mesh.")
    if _addr_is_private(host):
        blocked += (f"\n\nAlso: {host} is a home/LAN address — it only works "
                    "if that computer is on the same local network as you. "
                    "For a friend across the internet you need their public "
                    "address.")
    ip, fam = infos[0][4][0], infos[0][0]
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return None                                   # port open — reachable
    except ConnectionRefusedError:
        return (f"Something answered at {host}, but nothing is listening on "
                f"port {port}. The node there is probably off, or the port is "
                f"wrong (Kestrel uses {params.DEFAULT_PORT}).")
    except (socket.timeout, TimeoutError):
        return blocked
    except OSError as e:
        if getattr(e, "errno", None) in _BLOCKED_ERRNOS:
            return blocked
        return f"Couldn't connect to {host}:{port} ({e.__class__.__name__})."
    finally:
        s.close()


# ------------------------------------------------------------------ QR code
# Stdlib QR encoder (byte mode, error-correction level M, versions 1-10).
# Output verified cell-by-cell against the reference `qrcode` library and
# scan-tested. Plenty for addresses (~34 chars → version 3).

def _qr_gf():
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_QR_EXP, _QR_LOG = _qr_gf()

_QR_BLOCKS = {
    1: [(26, 16)], 2: [(44, 28)], 3: [(70, 44)],
    4: [(50, 32)] * 2, 5: [(67, 43)] * 2, 6: [(43, 27)] * 4,
    7: [(49, 31)] * 4, 8: [(60, 38)] * 2 + [(61, 39)] * 2,
    9: [(58, 36)] * 3 + [(59, 37)] * 2, 10: [(69, 43)] * 4 + [(70, 44)],
}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
             6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
             10: [6, 28, 50]}


def _qr_rs(data, n_ec):
    g = [1]
    for i in range(n_ec):
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= c
            ng[j + 1] ^= _QR_EXP[(_QR_LOG[c] + i) % 255] if c else 0
        g = ng
    res = list(data) + [0] * n_ec
    for i in range(len(data)):
        c = res[i]
        if c:
            lc = _QR_LOG[c]
            for j in range(1, len(g)):
                if g[j]:
                    res[i + j] ^= _QR_EXP[(lc + _QR_LOG[g[j]]) % 255]
    return res[len(data):]


def _qr_bch15(d5):
    d = d5 << 10
    for i in range(4, -1, -1):
        if d & (1 << (i + 10)):
            d ^= 0b10100110111 << i
    return ((d5 << 10) | d) ^ 0b101010000010010


def _qr_bch18(v6):
    d = v6 << 12
    for i in range(5, -1, -1):
        if d & (1 << (i + 12)):
            d ^= 0b1111100100101 << i
    return (v6 << 12) | d


def _qr_build(version, bits, mask):
    size = 17 + 4 * version
    M = [[None] * size for _ in range(size)]

    def finder(r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < size and 0 <= cc < size:
                    on = (0 <= dr <= 6 and dc in (0, 6)) or \
                         (0 <= dc <= 6 and dr in (0, 6)) or \
                         (2 <= dr <= 4 and 2 <= dc <= 4)
                    M[rr][cc] = 1 if on else 0

    finder(0, 0); finder(0, size - 7); finder(size - 7, 0)
    for pos in range(8, size - 8):
        v = 1 if pos % 2 == 0 else 0
        if M[6][pos] is None:
            M[6][pos] = v
        if M[pos][6] is None:
            M[pos][6] = v
    cs = _QR_ALIGN[version]
    if cs:
        lo, hi = cs[0], cs[-1]
        for r in cs:
            for c in cs:
                if ((r == lo and c == lo) or (r == lo and c == hi)
                        or (r == hi and c == lo)):
                    continue  # would overlap a finder pattern
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        on = max(abs(dr), abs(dc)) != 1
                        M[r + dr][c + dc] = 1 if on else 0
    M[size - 8][8] = 1
    for i in range(9):
        if M[8][i] is None:
            M[8][i] = 0
        if M[i][8] is None:
            M[i][8] = 0
    for i in range(8):
        if M[8][size - 1 - i] is None:
            M[8][size - 1 - i] = 0
        if M[size - 1 - i][8] is None:
            M[size - 1 - i][8] = 0
    if version >= 7:
        vi = _qr_bch18(version)
        for i in range(18):
            b = (vi >> i) & 1
            M[i // 3][size - 11 + i % 3] = b
            M[size - 11 + i % 3][i // 3] = b

    masks = (
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    )
    mf = masks[mask]
    idx, col, upward, nbits = 0, size - 1, True, len(bits)
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if M[r][c] is not None:
                    continue
                b = bits[idx] if idx < nbits else 0
                idx += 1
                if mf(r, c):
                    b ^= 1
                M[r][c] = b
        upward = not upward
        col -= 2

    fmt = _qr_bch15(mask)          # EC level M = 00 → data bits are the mask
    fbits = [(fmt >> (14 - i)) & 1 for i in range(15)]
    ca = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
          (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    cb = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
          (size - 5, 8), (size - 6, 8), (size - 7, 8),
          (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
          (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]
    for (r, c), b in zip(ca, fbits):
        M[r][c] = b
    for (r, c), b in zip(cb, fbits):
        M[r][c] = b
    return M


def _qr_penalty(M):
    size = len(M)
    p = 0
    for grid in (M, [list(t) for t in zip(*M)]):
        for row in grid:
            run = 1
            for i in range(1, size):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        p += 3 + run - 5
                    run = 1
            if run >= 5:
                p += 3 + run - 5
    for r in range(size - 1):
        for c in range(size - 1):
            if M[r][c] == M[r][c + 1] == M[r + 1][c] == M[r + 1][c + 1]:
                p += 3
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = pat1[::-1]
    for grid in (M, [list(t) for t in zip(*M)]):
        for row in grid:
            for i in range(size - 10):
                seg = row[i:i + 11]
                if seg == pat1 or seg == pat2:
                    p += 40
    dark = sum(sum(r) for r in M)
    p += (int(abs(dark * 100 / (size * size) - 50)) // 5) * 10
    return p


def qr_matrix(text):
    """Encode text as a QR matrix (list of rows of 0/1), or None if huge."""
    data = text.encode("utf-8")
    version = None
    for v in range(1, 11):
        cap = sum(d for _t, d in _QR_BLOCKS[v])
        if 4 + (16 if v >= 10 else 8) + 8 * len(data) <= 8 * cap:
            version = v
            break
    if version is None:
        return None
    blocks = _QR_BLOCKS[version]
    n_data = sum(d for _t, d in blocks)
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)
    put(len(data), 16 if version >= 10 else 8)
    for byte in data:
        put(byte, 8)
    put(0, min(4, 8 * n_data - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    i = 0
    while len(bits) < 8 * n_data:
        put((0xEC, 0x11)[i % 2], 8)
        i += 1
    cw = [int("".join(map(str, bits[j:j + 8])), 2)
          for j in range(0, len(bits), 8)]
    db, eb, pos = [], [], 0
    for total, d in blocks:
        db.append(cw[pos:pos + d])
        eb.append(_qr_rs(cw[pos:pos + d], total - d))
        pos += d
    inter = []
    for k in range(max(len(b) for b in db)):
        for b in db:
            if k < len(b):
                inter.append(b[k])
    for k in range(max(len(b) for b in eb)):
        for b in eb:
            if k < len(b):
                inter.append(b[k])
    final_bits = []
    for c in inter:
        for k in range(7, -1, -1):
            final_bits.append((c >> k) & 1)
    best, best_p = None, None
    for m in range(8):
        M = _qr_build(version, final_bits, m)
        p = _qr_penalty(M)
        if best_p is None or p < best_p:
            best, best_p = M, p
    return best


class Tooltip:
    """Small hover hint for any widget."""

    def __init__(self, widget, text, delay=550):
        self.w, self.text, self.delay = widget, text, delay
        self.tip = None
        self._id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._id = self.w.after(self.delay, self._show)

    def _cancel(self):
        if self._id:
            self.w.after_cancel(self._id)
            self._id = None

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.w.winfo_rootx() + 12
        y = self.w.winfo_rooty() + self.w.winfo_height() + 6
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        f = tk.Frame(self.tip, bg=DUSK3)
        f.pack()
        tk.Label(f, text=self.text, bg="#0C0F14", fg=BUFF,
                 font=SANS_9, justify="left", relief="flat",
                 padx=10, pady=6, wraplength=320).pack(padx=1, pady=1)
        try:
            self.tip.attributes("-topmost", True)
        except Exception:
            pass

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Toasts:
    """Non-blocking notifications, bottom-right, click to dismiss."""

    def __init__(self, root):
        self.root = root
        self.items = []

    def show(self, text, kind="info", ms=4500):
        edge = {"good": GREEN, "bad": RED, "warn": AMBER}.get(kind, SLATE)
        f = tk.Frame(self.root, bg=SPOT, highlightbackground=edge,
                     highlightthickness=1)
        tk.Frame(f, bg=edge, width=3).pack(side="left", fill="y")
        tk.Label(f, text=text, bg=SPOT, fg=BUFF, font=SANS_9, padx=12,
                 pady=8, wraplength=380, justify="left").pack(side="left")
        x = tk.Label(f, text="✕", bg=SPOT, fg=FAINT, font=TINY, padx=8,
                     cursor="hand2")
        x.pack(side="right", fill="y")
        for w in (f, x):
            w.bind("<Button-1>", lambda _e, ff=f: self.close(ff))
        self.items.append(f)
        self._layout()
        self.root.after(ms, lambda: self.close(f))

    def close(self, f):
        if f in self.items:
            self.items.remove(f)
        try:
            f.destroy()
        except Exception:
            pass
        self._layout()

    def _layout(self):
        while len(self.items) > 4:
            self.close(self.items[0])
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        y = -44
        for f in reversed(self.items):
            f.place(relx=1.0, rely=1.0, x=-14, y=y, anchor="se")
            f.lift()
            y -= max(f.winfo_reqheight(), 36) + 8


class App(tk.Tk):
    VIEWS = ("Overview", "Send", "Receive", "Transactions", "Contacts")
    ICONS = {"Overview": "◈", "Send": "↑", "Receive": "↓",
             "Transactions": "☰", "Contacts": "❖"}

    def __init__(self):
        super().__init__()
        _resolve_fonts(self)
        self.title(f"Kestrel Wallet {KVER}")
        self.configure(bg=DUSK)
        self.minsize(880, 600)
        self.settings = load_settings()
        self._restore_geometry()
        self._make_icon()

        self.wallet: Wallet | None = None
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.node_var = tk.StringVar(value=f"http://127.0.0.1:{PORTS[0]}")
        self.online = False
        self._was_online = None
        self.tip_height = 0
        self._avail = 0
        self._history = []
        self._pending = []
        self._rows = []
        self.book = self._load_book()
        self.embedded = None  # our own node, if we had to start one
        self.toasts = Toasts(self)
        self._tx_filter = "All"

        self._init_style()
        self._build_menu()
        self._build_ui()
        self._bind_keys()
        self._ensure_wallet()
        self.after(150, self._drain_queue)
        threading.Thread(target=self._auto_node, daemon=True).start()
        self._poll()
        self._auto_refresh()
        self.protocol("WM_DELETE_WINDOW", self._quit)

    # ------------------------------------------------------------ window
    def _restore_geometry(self):
        geo = self.settings.get("geometry", "")
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            if geo:
                size, _, rest = geo.partition("+")
                w, h = (int(v) for v in size.split("x"))
                x, y = (int(v) for v in rest.split("+"))
                if 700 <= w <= sw and 480 <= h <= sh and \
                        -50 <= x < sw - 200 and -50 <= y < sh - 200:
                    self.geometry(geo)
                    return
            self.geometry(f"1000x680+{max((sw - 1000) // 2, 0)}"
                          f"+{max((sh - 680) // 2 - 20, 20)}")
        except Exception:
            self.geometry("1000x680")

    def _make_icon(self):
        try:
            img = tk.PhotoImage(width=32, height=32)
            img.put(DUSK2, to=(0, 0, 32, 32))
            for y in range(5, 26):
                w = int((y - 5) * 0.62) + 1
                img.put(RUFOUS, to=(16 - w, y, 16 + w, y + 1))
            img.put(RUFOUS_HI, to=(9, 25, 23, 27))
            img.put(BUFF, to=(15, 12, 18, 15))
            img.put(DUSK2, to=(16, 13, 17, 14))
            self.iconphoto(True, img)
            self._icon_img = img
        except Exception:
            pass

    def _bind_keys(self):
        for i, name in enumerate(self.VIEWS, start=1):
            self.bind_all(f"<Control-Key-{i}>",
                          lambda _e, n=name: self.show_view(n))
        self.bind_all("<F5>", lambda _e: self._refresh_now())

    def toast(self, text, kind="info"):
        self.toasts.show(text, kind)

    # ---------------------------------------------------- zero-setup start
    def _ensure_wallet(self):
        if os.path.exists(WALLET_FILE):
            try:
                self.wallet = Wallet.load(WALLET_FILE)
            except Exception as e:
                messagebox.showerror("Wallet file problem", str(e))
        else:
            self.wallet = Wallet.create()
            self.wallet.save(WALLET_FILE)
            self.settings["backed_up"] = False
            save_settings(self.settings)
            messagebox.showinfo(
                "Welcome to Kestrel",
                "Your wallet is ready.\n\nAddress (share to receive):\n"
                + self.wallet.address +
                "\n\nBackup key (keep secret):\n"
                + private_to_wif(self.wallet.private_key) +
                "\n\nWrite the backup key down now — it is the only way to "
                "restore your coins. File ▸ Backup saves it as a file.")
        if self.wallet:
            self.addr_var.set(self.wallet.address)
            self._draw_qr()
        self._update_banner()

    def _auto_node(self):
        """Use a node already on this machine, or quietly run our own.
        The embedded node auto-connects: seeds, saved peers, LAN + DHT."""
        for p in PORTS[:4]:                    # a node already running here?
            u = f"http://127.0.0.1:{p}"
            if kestrel_at(u):
                self.q.put(("setnode", u, "found a node already running "
                                          "on this machine"))
                return
        for s in load_seed_nodes(DATA_DIR):    # a public node, if configured
            if kestrel_at(s):
                self.q.put(("setnode", s, "connected to a public node"))
                return
        for p in PORTS:                        # run our own full node
            if port_free(p):
                try:
                    chain = Blockchain(data_dir=DATA_DIR)
                    self.embedded = Node(chain, host="0.0.0.0", port=p)
                    threading.Thread(target=self.embedded.serve_forever,
                                     daemon=True).start()
                    self.q.put(("setnode", f"http://127.0.0.1:{p}",
                                "started this app's own built-in node"))
                    return
                except Exception:
                    continue

    # -------------------------------------------------------------- theming
    def _init_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("KV.Treeview", background=SPOT, fieldbackground=SPOT,
                    foreground=BUFF, bordercolor=DUSK3, borderwidth=0,
                    rowheight=28, font=SANS_9)
        s.configure("KV.Treeview.Heading", background=DUSK3, foreground=MUTED,
                    font=TINY_B, relief="flat", padding=6)
        s.map("KV.Treeview", background=[("selected", DUSK3)],
              foreground=[("selected", BUFF)])
        s.map("KV.Treeview.Heading", background=[("active", HOVER)])
        s.configure("KV.Vertical.TScrollbar", background=DUSK3,
                    troughcolor=SPOT, bordercolor=SPOT, arrowcolor=MUTED,
                    relief="flat", gripcount=0)
        s.map("KV.Vertical.TScrollbar", background=[("active", HOVER)])

    def _attach_edit_menu(self, w):
        """Right-click Cut/Copy/Paste/Select-all on an Entry + Ctrl+A."""
        m = tk.Menu(w, tearoff=0, bg=DUSK2, fg=BUFF, activebackground=DUSK3,
                    activeforeground=BUFF, bd=0, font=SANS)
        m.add_command(label="Cut", command=lambda: w.event_generate("<<Cut>>"))
        m.add_command(label="Copy", command=lambda: w.event_generate("<<Copy>>"))
        m.add_command(label="Paste",
                      command=lambda: w.event_generate("<<Paste>>"))
        m.add_separator()
        m.add_command(label="Select all",
                      command=lambda: w.select_range(0, "end"))

        def pop(e):
            w.focus_set()
            try:
                m.tk_popup(e.x_root, e.y_root)
            finally:
                m.grab_release()
            return "break"

        w.bind("<Button-3>", pop)
        w.bind("<Button-2>", pop)
        w.bind("<Control-a>",
               lambda e: (w.select_range(0, "end"), "break")[1])
        return w

    def _entry(self, parent, **kw):
        e = tk.Entry(parent, bg=SPOT, fg=BUFF, insertbackground=BUFF,
                     relief="flat", font=MONO, highlightthickness=1,
                     highlightbackground=DUSK3, highlightcolor=SLATE, **kw)
        return self._attach_edit_menu(e)

    def _placeholder(self, e, text):
        """Grey hint text inside an Entry that clears itself on focus."""
        e._ph, e._ph_on = text, False

        def show():
            if not e.get():
                e._ph_on = True
                e.configure(fg=FAINT)
                e.insert(0, text)

        def hide(_ev=None):
            if e._ph_on:
                e._ph_on = False
                e.delete(0, "end")
                e.configure(fg=BUFF)

        e.bind("<FocusIn>", hide, add="+")
        e.bind("<FocusOut>", lambda _ev: show(), add="+")
        show()
        return e

    @staticmethod
    def _entry_value(e):
        return "" if getattr(e, "_ph_on", False) else e.get().strip()

    def _dialog(self, title):
        """A styled, transient Toplevel. The modal grab is taken only after
        the dialog is fully built (see _present_dialog), so a build error
        can never leave the app stuck behind an empty grabbed window."""
        top = tk.Toplevel(self)
        top.withdraw()                    # shown centered by _present_dialog
        top.configure(bg=DUSK2)
        top.title(title)
        top.transient(self)
        top.resizable(False, False)
        top.bind("<Escape>", lambda _e: top.destroy())
        return top

    def _present_dialog(self, top, modal=True):
        """Center `top` over the main window, show it, and (optionally)
        make it modal."""
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        top.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        top.deiconify()
        if modal:
            top.grab_set()

    def _ask_string(self, title, prompt, secret=False, initial=""):
        """A paste-friendly replacement for simpledialog.askstring."""
        top = self._dialog(title)
        tk.Label(top, text=prompt, bg=DUSK2, fg=BUFF, font=SANS,
                 justify="left", padx=16).pack(anchor="w", pady=(14, 4))
        e = self._entry(top, width=56, **({"show": "•"} if secret else {}))
        if initial:
            e.insert(0, initial)
        e.pack(padx=16, pady=6, ipady=4)
        out = {"value": None}

        def done(_ev=None):
            out["value"] = e.get()
            top.destroy()

        row = tk.Frame(top, bg=DUSK2); row.pack(pady=(4, 14))
        self._btn(row, "OK", done, primary=True).pack(side="left", padx=4)
        self._btn(row, "Cancel", top.destroy).pack(side="left", padx=4)
        e.bind("<Return>", done)
        self._present_dialog(top)
        e.focus_set()
        top.wait_window()
        return out["value"]

    def _btn(self, parent, text, cmd, primary=False, tip=None, **kw):
        base = RUFOUS if primary else DUSK3
        hov = RUFOUS_HI if primary else HOVER
        b = tk.Button(parent, text=text, command=cmd, bg=base,
                      fg=DUSK if primary else BUFF,
                      activebackground=hov,
                      activeforeground=DUSK if primary else BUFF,
                      relief="flat", font=SANS_B, padx=15, pady=7,
                      cursor="hand2", bd=0, **kw)
        b.bind("<Enter>", lambda _e: b.configure(
            bg=hov if b["state"] != "disabled" else base), add="+")
        b.bind("<Leave>", lambda _e: b.configure(bg=base), add="+")
        if tip:
            Tooltip(b, tip)
        return b

    def _linkbtn(self, parent, text, cmd, tip=None):
        b = tk.Button(parent, text=text, command=cmd, bg=DUSK, fg=SLATE,
                      activebackground=DUSK, activeforeground=BUFF,
                      relief="flat", font=SANS_9B, padx=6, pady=2,
                      cursor="hand2", bd=0)
        b.bind("<Enter>", lambda _e: b.configure(fg=BUFF), add="+")
        b.bind("<Leave>", lambda _e: b.configure(fg=SLATE), add="+")
        if tip:
            Tooltip(b, tip)
        return b

    def _zebra(self, tv):
        for i, iid in enumerate(tv.get_children()):
            tags = [t for t in tv.item(iid, "tags")
                    if t not in ("even", "odd")]
            tags.append("even" if i % 2 == 0 else "odd")
            tv.item(iid, tags=tags)

    def _style_table(self, tv):
        """Zebra colors, hint style and click-to-sort headings."""
        tv.tag_configure("even", background=SPOT)
        tv.tag_configure("odd", background=ZEBRA)
        tv.tag_configure("hint", foreground=FAINT)

        def sortkey(v):
            s = (str(v).replace(",", "").replace("+", "")
                 .replace(" KSL", "").replace("…", "").strip())
            try:
                return (0, float(s), "")
            except ValueError:
                return (1, 0.0, s.lower())

        def sort(col, rev):
            rows = [(tv.set(i, col), i) for i in tv.get_children()]
            rows.sort(key=lambda t: sortkey(t[0]), reverse=rev)
            for pos, (_v, i) in enumerate(rows):
                tv.move(i, "", pos)
            tv.heading(col, command=lambda c=col: sort(c, not rev))
            self._zebra(tv)

        for c in tv["columns"]:
            tv.heading(c, command=lambda c=c: sort(c, False))

    def _hint_if_empty(self, tv, text):
        if not tv.get_children():
            vals = [""] * len(tv["columns"])
            vals[0] = text
            tv.insert("", "end", values=vals, tags=("hint",))

    def _tree(self, parent, spec, height=8, stretch=None):
        """Treeview + auto-hiding styled scrollbar. spec: (name,width,anchor)."""
        wrap = tk.Frame(parent, bg=DUSK)
        cols = [n for n, _w, _a in spec]
        tv = ttk.Treeview(wrap, columns=cols, show="headings",
                          style="KV.Treeview", height=height,
                          selectmode="browse")
        for name, wdt, anc in spec:
            tv.heading(name, text=name.upper())
            tv.column(name, width=wdt, anchor=anc, stretch=(name == stretch))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview,
                           style="KV.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)

        def set_sb(lo, hi):
            sb.set(lo, hi)
            try:
                if float(lo) <= 0.0 and float(hi) >= 1.0:
                    sb.pack_forget()
                elif not sb.winfo_ismapped():
                    sb.pack(side="right", fill="y", before=tv)
            except Exception:
                pass

        tv.configure(yscrollcommand=set_sb)
        tv.tag_configure("pos", foreground=GREEN)
        tv.tag_configure("neg", foreground=RED)
        tv.tag_configure("pend", foreground=AMBER)
        tv.tag_configure("dim", foreground=FAINT)
        self._style_table(tv)
        return wrap, tv

    # ----------------------------------------------------------------- menu
    def _build_menu(self):
        mk = dict(bg=DUSK2, fg=BUFF, activebackground=DUSK3,
                  activeforeground=BUFF, bd=0, font=SANS)
        bar = tk.Menu(self, **mk)
        fm = tk.Menu(bar, tearoff=0, **mk)
        fm.add_command(label="Backup wallet file…", command=self._backup_wallet)
        fm.add_command(label="Show backup key (WIF)", command=self.show_wif)
        fm.add_separator()
        fm.add_command(label="New wallet…", command=self.new_wallet)
        fm.add_command(label="Restore from key…", command=self.import_wif)
        fm.add_separator()
        fm.add_command(label="Export transactions (CSV)…",
                       command=self._export_csv)
        fm.add_command(label="Open data folder",
                       command=lambda: open_folder(_HERE))
        fm.add_separator()
        fm.add_command(label="Exit", command=self._quit)
        bar.add_cascade(label="File", menu=fm)
        sm = tk.Menu(bar, tearoff=0, **mk)
        sm.add_command(label="Connect to a network node…",
                       command=self._add_peer)
        sm.add_command(label="Share this computer's node…",
                       command=self._share_node)
        sm.add_separator()
        sm.add_command(label="Node address…", command=self._set_node)
        sm.add_command(label="Find a node automatically",
                       command=self._re_auto_node)
        sm.add_separator()
        sm.add_command(label="Open node dashboard in browser",
                       command=self._open_dashboard)
        bar.add_cascade(label="Settings", menu=sm)
        hm = tk.Menu(bar, tearoff=0, **mk)
        hm.add_command(label="About Kestrel Wallet", command=self._about)
        hm.add_command(label="Keyboard shortcuts", command=self._shortcuts)
        bar.add_cascade(label="Help", menu=hm)
        self.config(menu=bar)

    def _open_dashboard(self):
        url = self.node_url()
        if url.startswith("http"):
            webbrowser.open(url + "/")
        else:
            self.toast("No node address set yet.", "warn")

    def _set_node(self):
        v = self._ask_string("Node address",
                             "URL of the Kestrel node to talk to:",
                             initial=self.node_var.get())
        if v:
            self.node_var.set(v.strip())
            self.refresh()
            self.toast(f"Now talking to {v.strip()}")

    def _re_auto_node(self):
        self.toast("Looking for the best node automatically…")
        threading.Thread(target=self._auto_node, daemon=True).start()

    def _add_peer(self):
        v = self._ask_string(
            "Connect to a network node",
            "Paste a friend's node address (like "
            f"http://their-ip:{params.DEFAULT_PORT}).\n"
            "Your node remembers it and passes it on — one connection\n"
            "is enough for the whole mesh to find itself:")
        if not v:
            return
        # loose input welcome: "1.2.3.4", "1.2.3.4:4444" and full URLs
        # all work — parse_node_url fills in the scheme and default port
        url, _host, _port = parse_node_url(v)
        if not url:
            return self.toast("That doesn't look like a node address — "
                              "use something like "
                              f"http://12.34.56.78:{params.DEFAULT_PORT}.",
                              "warn")
        self.toast(f"Connecting to {url}…")
        threading.Thread(target=self._add_peer_worker, args=(url,),
                         daemon=True).start()

    def _add_peer_worker(self, url):
        reason = diagnose_node(url)      # quick reachability probe first
        try:
            http_json("POST", self.node_url() + "/peers/add",
                      {"url": url}, timeout=8)
        except Exception as e:
            return self.q.put(("toast", f"Could not save that node: {e}",
                               "bad"))
        if reason:
            self.q.put(("connfail", "Saved, but couldn't reach it yet",
                        "Your node saved that address and will keep trying "
                        "in the background.\n\n" + reason))
        else:
            self.q.put(("toast", "Connected — your node saved that address "
                                 "and will spread the word.", "good"))
        self.q.put(("refresh",))

    def _share_node(self):
        threading.Thread(target=self._share_worker, daemon=True).start()

    def _share_worker(self):
        try:
            s = http_json("GET", self.node_url() + "/supply", timeout=6)
            pub = s.get("public_url")
            if pub:
                url, public = pub, True
            else:
                port = self.node_url().rsplit(":", 1)[-1]
                url, public = f"http://{get_lan_ip()}:{port}", False
            self.q.put(("sharedlg", url, public))
        except Exception as e:
            self.q.put(("toast", f"Could not ask the node: {e}", "bad"))

    def _show_share_dialog(self, url, public):
        top = self._dialog("Share this computer's node")
        note = ("Send this to friends — they paste it into Network ▸ "
                "“Add a node” in Kestrel Miner (or Settings ▸ Connect "
                "here in the wallet). One connection is enough: nodes "
                "pass peers around, so the network spreads on its own.")
        if not public:
            note += ("\n\nRight now this address works for people on "
                     "your own network. For friends across the internet, "
                     "run Kestrel Miner and check its Network page.")
        tk.Label(top, text=note, bg=DUSK2, fg=MUTED, font=SANS_9,
                 wraplength=430, justify="left", padx=16
                 ).pack(anchor="w", pady=(14, 4))
        e = self._entry(top, width=44)
        e.insert(0, url)
        e.configure(state="readonly", readonlybackground=SPOT)
        e.pack(padx=16, pady=6, ipady=4)
        row = tk.Frame(top, bg=DUSK2)
        row.pack(pady=(4, 14))
        self._btn(row, "Copy address",
                  lambda: (self.clipboard_clear(),
                           self.clipboard_append(url),
                           self.toast("Node address copied.", "good")),
                  primary=True).pack(side="left", padx=4)
        self._btn(row, "Copy invite",
                  lambda: (self.clipboard_clear(),
                           self.clipboard_append(invite_text(url)),
                           self.toast("Invite copied — paste it into "
                                      "any chat.", "good"))
                  ).pack(side="left", padx=4)
        self._btn(row, "Close", top.destroy).pack(side="left", padx=4)
        self._present_dialog(top, modal=False)

    def _about(self):
        messagebox.showinfo(
            "About",
            f"Kestrel Wallet {KVER}\n\nSelf-custody wallet for Kestrel (KSL) "
            "— fixed supply of 44,000,000, secured by proof-of-work.\n\n"
            "Keys are created and transactions signed on this machine.\n"
            "Open source, MIT license.")

    def _shortcuts(self):
        messagebox.showinfo(
            "Keyboard shortcuts",
            "Ctrl+1…5   switch view (Overview / Send / Receive / "
            "Transactions / Contacts)\n"
            "F5             refresh balance and transactions\n"
            "Enter         in the Send form: send\n"
            "Ctrl+A        select all in any text box\n"
            "Right-click  cut / copy / paste in any text box")

    def _backup_wallet(self):
        if not os.path.exists(WALLET_FILE):
            return messagebox.showinfo("No wallet", "Create a wallet first.")
        path = filedialog.asksaveasfilename(
            title="Backup wallet file", defaultextension=".json",
            initialfile="kestrel-wallet-backup.json",
            filetypes=[("Wallet file", "*.json")])
        if path:
            shutil.copyfile(WALLET_FILE, path)
            self.settings["backed_up"] = True
            save_settings(self.settings)
            self._update_banner()
            self.toast("Wallet backed up — store the file somewhere safe.",
                       "good")

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        self.addr_var = tk.StringVar(value="…")

        body = tk.Frame(self, bg=DUSK)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ------------------------------------------------------ sidebar
        rail = tk.Frame(body, bg=RAIL, width=182)
        rail.grid(row=0, column=0, sticky="nsw")
        rail.pack_propagate(False)
        brand = tk.Frame(rail, bg=RAIL)
        brand.pack(fill="x", padx=18, pady=(18, 22))
        tk.Label(brand, text="▲ kestrel", bg=RAIL, fg=BUFF,
                 font=BRAND).pack(anchor="w")
        tk.Label(brand, text="WALLET", bg=RAIL, fg=RUFOUS_HI,
                 font=TINY_B).pack(anchor="w")

        self._navbtn = {}
        for name in self.VIEWS:
            rowf = tk.Frame(rail, bg=RAIL)
            rowf.pack(fill="x")
            ind = tk.Frame(rowf, bg=RAIL, width=3)
            ind.pack(side="left", fill="y")
            b = tk.Button(rowf, anchor="w", relief="flat",
                          text=f"  {self.ICONS.get(name, '')}  {name}",
                          bd=0, bg=RAIL, fg=MUTED, activebackground=DUSK2,
                          activeforeground=BUFF, font=SANS_B, padx=14, pady=10,
                          cursor="hand2",
                          command=lambda n=name: self.show_view(n))
            b.pack(side="left", fill="x", expand=True)

            def hov(_e, nm=name, on=True):
                bb, _i, _r = self._navbtn[nm]
                if nm != getattr(self, "_active_view", None):
                    bb.configure(bg="#1A2130" if on else RAIL)
            b.bind("<Enter>", lambda e, nm=name: hov(e, nm, True))
            b.bind("<Leave>", lambda e, nm=name: hov(e, nm, False))
            self._navbtn[name] = (b, ind, rowf)

        foot = tk.Frame(rail, bg=RAIL)
        foot.pack(side="bottom", fill="x", padx=18, pady=16)
        row = tk.Frame(foot, bg=RAIL); row.pack(anchor="w")
        self.dot_l = tk.Label(row, text="●", bg=RAIL, fg=FAINT,
                              font=SANS)
        self.dot_l.pack(side="left")
        self.conn_var = tk.StringVar(value="Connecting…")
        tk.Label(row, textvariable=self.conn_var, bg=RAIL, fg=MUTED,
                 font=SANS_9).pack(side="left", padx=(5, 0))
        rbtn = tk.Button(row, text=" ⟳", command=self._refresh_now, bg=RAIL,
                         fg=MUTED, activebackground=RAIL,
                         activeforeground=BUFF, relief="flat", bd=0,
                         font=SANS_B, cursor="hand2")
        rbtn.pack(side="left", padx=(6, 0))
        rbtn.bind("<Enter>", lambda _e: rbtn.configure(fg=BUFF))
        rbtn.bind("<Leave>", lambda _e: rbtn.configure(fg=MUTED))
        Tooltip(rbtn, "Refresh balance and transactions now (F5)")
        self.node_lbl_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self.node_lbl_var, bg=RAIL, fg=FAINT,
                 font=MONO_8).pack(anchor="w", pady=(3, 0))
        self.block_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self.block_var, bg=RAIL, fg=FAINT,
                 font=MONO_8).pack(anchor="w", pady=(3, 0))
        self.updated_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self.updated_var, bg=RAIL, fg=FAINT,
                 font=MONO_8).pack(anchor="w", pady=(3, 0))
        tk.Label(foot, text=f"v{KVER} · MIT", bg=RAIL, fg=FAINT,
                 font=MONO_8).pack(anchor="w", pady=(3, 0))

        # ------------------------------------------------------ content
        content = tk.Frame(body, bg=DUSK)
        content.grid(row=0, column=1, sticky="nsew")
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self._views = {}
        for name, builder in (("Overview", self._view_overview),
                              ("Send", self._view_send),
                              ("Receive", self._view_receive),
                              ("Transactions", self._view_transactions),
                              ("Contacts", self._view_contacts)):
            f = tk.Frame(content, bg=DUSK)
            f.grid(row=0, column=0, sticky="nsew")
            builder(f)
            self._views[name] = f
        self.show_view("Overview")

    def show_view(self, name):
        self._active_view = name
        self._views[name].tkraise()
        for nm, (b, ind, rowf) in self._navbtn.items():
            on = (nm == name)
            b.configure(bg=DUSK2 if on else RAIL, fg=BUFF if on else MUTED)
            ind.configure(bg=RUFOUS_HI if on else RAIL)
            rowf.configure(bg=DUSK2 if on else RAIL)

    def _page(self, f, title, subtitle=""):
        p = tk.Frame(f, bg=DUSK)
        p.pack(fill="both", expand=True, padx=28, pady=22)
        tk.Label(p, text=title, bg=DUSK, fg=BUFF,
                 font=TITLE).pack(anchor="w")
        if subtitle:
            tk.Label(p, text=subtitle, bg=DUSK, fg=FAINT, font=SANS_9
                     ).pack(anchor="w", pady=(1, 0))
        tk.Frame(p, bg=DUSK3, height=1).pack(fill="x", pady=(12, 16))
        return p

    def _card(self, parent):
        c = tk.Frame(parent, bg=DUSK2, highlightbackground=DUSK3,
                     highlightthickness=1)
        inner = tk.Frame(c, bg=DUSK2)
        inner.pack(fill="both", expand=True, padx=18, pady=14)
        return c, inner

    # ------------------------------------------------------------- overview
    def _view_overview(self, f):
        p = self._page(f, "Overview", "Your balances and latest activity")

        # back-up-your-key banner (hidden once backed up / dismissed)
        self.banner = tk.Frame(p, bg="#2E2718", highlightbackground=AMBER,
                               highlightthickness=1)
        brow = tk.Frame(self.banner, bg="#2E2718")
        brow.pack(fill="x", padx=12, pady=8)
        tk.Label(brow, text="⚠  Back up your key — it is the only way to "
                 "restore your coins if this computer dies.",
                 bg="#2E2718", fg=AMBER, font=SANS_9,
                 wraplength=520, justify="left").pack(side="left")
        self._linkbtn(brow, "I've already done this",
                      self._dismiss_banner).pack(side="right")
        bb = self._btn(brow, "Back up now", self._backup_wallet)
        bb.configure(padx=10, pady=3, font=SANS_9B)
        bb.pack(side="right", padx=(0, 8))

        card, c = self._card(p)
        card.pack(fill="x")
        self._ov_card = card
        self.avail_var = tk.StringVar(value="0.00000000 KSL")
        self.immature_var = tk.StringVar(value="0.00000000 KSL")
        self.total_var = tk.StringVar(value="0.00000000 KSL")
        self.pending_var = tk.StringVar(value="—")
        tk.Label(c, text="AVAILABLE", bg=DUSK2, fg=RUFOUS_HI,
                 font=TINY_B).pack(anchor="w")
        tk.Label(c, textvariable=self.avail_var, bg=DUSK2, fg=BUFF,
                 font=MONO_26B).pack(anchor="w")
        row = tk.Frame(c, bg=DUSK2); row.pack(anchor="w", pady=(8, 0))
        for label, var, color in (
                ("Immature (maturing rewards)", self.immature_var, MUTED),
                ("Total", self.total_var, MUTED),
                ("Unconfirmed", self.pending_var, AMBER)):
            box = tk.Frame(row, bg=DUSK2); box.pack(side="left", padx=(0, 34))
            tk.Label(box, text=label, bg=DUSK2, fg=FAINT,
                     font=TINY).pack(anchor="w")
            tk.Label(box, textvariable=var, bg=DUSK2, fg=color,
                     font=MONO_12).pack(anchor="w")
        self.pend_note = tk.Label(c, text="", bg=DUSK2, fg=AMBER,
                                  font=SANS_9, justify="left")

        qa = tk.Frame(p, bg=DUSK); qa.pack(fill="x", pady=(14, 0))
        self._btn(qa, "↑  Send KSL", lambda: self.show_view("Send"),
                  primary=True).pack(side="left")
        self._btn(qa, "↓  Receive", lambda: self.show_view("Receive")
                  ).pack(side="left", padx=(8, 0))
        self._btn(qa, "Copy my address", self.copy_address,
                  tip="Copy your address — share it to get paid"
                  ).pack(side="left", padx=(8, 0))

        rhead = tk.Frame(p, bg=DUSK); rhead.pack(fill="x", pady=(18, 6))
        tk.Label(rhead, text="RECENT ACTIVITY", bg=DUSK, fg=RUFOUS_HI,
                 font=TINY_B).pack(side="left")
        self._linkbtn(rhead, "See all →",
                      lambda: self.show_view("Transactions")
                      ).pack(side="left", padx=(10, 0))
        self._btn(rhead, "⟳ Refresh", self._refresh_now,
                  tip="Fetch the newest balance and activity"
                  ).pack(side="right")
        wrap, self.recent_tv = self._tree(
            p, (("date", 160, "w"), ("type", 100, "w"), ("amount", 140, "e"),
                ("status", 110, "w")),
            height=7, stretch="amount")
        wrap.pack(fill="both", expand=True)
        self.recent_tv.bind("<Double-1>",
                            lambda _e: self.show_view("Transactions"))

    def _update_banner(self):
        if not hasattr(self, "banner"):
            return
        if self.settings.get("backed_up"):
            self.banner.pack_forget()
        else:
            self.banner.pack(fill="x", pady=(0, 12), before=self._ov_card)

    def _dismiss_banner(self):
        self.settings["backed_up"] = True
        save_settings(self.settings)
        self._update_banner()
        self.toast("Okay — you can always back up again from the File menu.")

    # ----------------------------------------------------------------- send
    def _view_send(self, f):
        p = self._page(f, "Send",
                       "Signed on this machine, confirmed in ~2 minutes")
        card, c = self._card(p)
        card.pack(fill="x")
        row0 = tk.Frame(c, bg=DUSK2); row0.pack(fill="x", pady=(0, 10))
        tk.Label(row0, text="AVAILABLE", bg=DUSK2, fg=FAINT,
                 font=TINY_B).pack(side="left")
        tk.Label(row0, textvariable=self.avail_var, bg=DUSK2, fg=SLATE,
                 font=MONO_11B).pack(side="left", padx=(8, 0))

        tk.Label(c, text="PAY TO", bg=DUSK2,
                 fg=RUFOUS_HI, font=TINY_B).pack(anchor="w")
        row = tk.Frame(c, bg=DUSK2); row.pack(fill="x", pady=(4, 2))
        self.to_e = self._entry(row)
        self.to_e.pack(side="left", fill="x", expand=True, ipady=5)
        self.to_e.bind("<KeyRelease>", lambda _e: self._validate_send())
        self._btn(row, "Paste", self._paste_to,
                  tip="Paste an address from the clipboard"
                  ).pack(side="left", padx=(6, 0))
        self._btn(row, "Contacts", lambda: self.show_view("Contacts")
                  ).pack(side="left", padx=(6, 0))
        self.to_hint = tk.Label(c, text="", bg=DUSK2, fg=FAINT, font=TINY,
                                anchor="w")
        self.to_hint.pack(fill="x", pady=(0, 8))

        tk.Label(c, text="LABEL  —  optional, saved to Contacts", bg=DUSK2,
                 fg=RUFOUS_HI, font=TINY_B).pack(anchor="w")
        self.label_e = self._entry(c)
        self.label_e.pack(fill="x", ipady=4, pady=(4, 12))

        tk.Label(c, text="AMOUNT", bg=DUSK2, fg=RUFOUS_HI,
                 font=TINY_B).pack(anchor="w")
        row2 = tk.Frame(c, bg=DUSK2); row2.pack(fill="x", pady=(4, 2))
        self.amt_e = self._entry(row2, width=16)
        self.amt_e.pack(side="left", ipady=5)
        self.amt_e.bind("<KeyRelease>", lambda _e: self._validate_send())
        self.max_btn = self._btn(row2, "Max", self.fill_max,
                                 tip="Send everything available,\n"
                                     "minus the network fee")
        self.max_btn.pack(side="left", padx=(6, 0))
        tk.Label(row2, text="KSL", bg=DUSK2, fg=MUTED,
                 font=SANS).pack(side="left", padx=(8, 18))
        tk.Label(row2, text="fee", bg=DUSK2, fg=MUTED,
                 font=SANS).pack(side="left", padx=(0, 6))
        self.fee_e = self._entry(row2, width=10)
        self.fee_e.insert(0, "0.00001")
        self.fee_e.pack(side="left", ipady=5)
        self.fee_e.bind("<KeyRelease>", lambda _e: self._validate_send())
        Tooltip(self.fee_e, "The network fee paid to the miner who\n"
                            "confirms this payment (minimum 0.00001 KSL)")
        for label, mult in (("min", 1), ("×10", 10), ("×100", 100)):
            self._linkbtn(row2, label,
                          lambda m=mult: self._set_fee(m),
                          tip="Bigger fees can help when many payments\n"
                              "compete for the next block"
                          ).pack(side="left", padx=(4, 0))

        self.after_var = tk.StringVar(value="")
        self.after_lbl = tk.Label(c, textvariable=self.after_var, bg=DUSK2,
                                  fg=FAINT, font=TINY, anchor="w")
        self.after_lbl.pack(fill="x", pady=(2, 8))

        self.send_btn = self._btn(c, "Send", self.send, primary=True)
        self.send_btn.pack(anchor="w", pady=(2, 4))
        for e in (self.to_e, self.amt_e, self.fee_e):
            e.bind("<Return>", lambda _ev: self.send())
        self.send_note = tk.Label(p, text="", bg=DUSK, fg=MUTED, font=SANS,
                                  anchor="w", wraplength=680, justify="left")
        self.send_note.pack(fill="x", pady=(12, 0))
        self._validate_send()

    def _set_fee(self, mult):
        fee = params.MIN_RELAY_FEE * mult
        self.fee_e.delete(0, "end")
        self.fee_e.insert(0, f"{fee / params.COIN:.8f}".rstrip("0")
                          .rstrip(".") or "0")
        self._validate_send()

    def _paste_to(self):
        try:
            pasted = self.clipboard_get().strip()
        except Exception:
            return self.toast("Clipboard is empty.", "warn")
        self.to_e.delete(0, "end")
        self.to_e.insert(0, pasted)
        self._validate_send()

    def _validate_send(self, *_a):
        """Live-check the Send form; enable the button only when sendable."""
        if not hasattr(self, "send_btn"):
            return False
        to = self.to_e.get().strip()
        amt_s = self.amt_e.get().strip()
        fee_s = self.fee_e.get().strip()
        ok = True

        if not to:
            self.to_hint.configure(text="", fg=FAINT)
            ok = False
        elif not is_valid_address(to):
            self.to_hint.configure(
                text="✗ not a Kestrel address yet — they start with K",
                fg=RED)
            ok = False
        elif self.wallet and to == self.wallet.address:
            self.to_hint.configure(text="⚠ that is your own address",
                                   fg=AMBER)
        else:
            known = self.book.get(to)
            self.to_hint.configure(
                text=f"✓ valid address — your contact “{known}”" if known
                else "✓ valid Kestrel address", fg=GREEN)

        amount = fee = None
        try:
            amount = parse_ksl(amt_s)
            if amount <= 0:
                raise ValueError
        except Exception:
            amount = None
        try:
            fee = parse_ksl(fee_s)
        except Exception:
            fee = None

        if amount is None:
            self.after_var.set("Enter the amount as a number, like 1.5"
                               if amt_s else "")
            ok = False
        elif fee is None:
            self.after_var.set("The fee should be a number, like 0.00001")
            ok = False
        elif fee < params.MIN_RELAY_FEE:
            self.after_var.set("Fee is below the network minimum of "
                               + format_ksl(params.MIN_RELAY_FEE))
            ok = False
        elif amount + fee > self._avail:
            short = format_ksl(amount + fee - self._avail)
            self.after_var.set(f"That is {short} more than you have "
                               "available — try Max")
            ok = False
        else:
            left = self._avail - amount - fee
            self.after_var.set(
                f"After sending you'll have {format_ksl(left)} left "
                f"(fee {format_ksl(fee)})")

        good = self.after_var.get().startswith("After sending")
        self.after_lbl.configure(fg=MUTED if good else
                                 (RED if self.after_var.get() else FAINT))
        if amount is not None and fee is not None and ok:
            self.send_btn.configure(state="normal", bg=RUFOUS, fg=DUSK)
        else:
            self.send_btn.configure(state="disabled", bg=DUSK3, fg=FAINT)
        return ok

    # -------------------------------------------------------------- receive
    def _view_receive(self, f):
        p = self._page(f, "Receive",
                       "Share your address — payments appear in Transactions")
        card, c = self._card(p)
        card.pack(fill="x")
        inner = tk.Frame(c, bg=DUSK2)
        inner.pack(fill="x")
        self.qr_canvas = tk.Canvas(inner, width=210, height=210, bg="white",
                                   highlightthickness=1,
                                   highlightbackground=DUSK3)
        self.qr_canvas.pack(side="left", padx=(0, 20), pady=2)
        right = tk.Frame(inner, bg=DUSK2)
        right.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="YOUR ADDRESS", bg=DUSK2, fg=RUFOUS_HI,
                 font=TINY_B).pack(anchor="w")
        addr_l = tk.Label(right, textvariable=self.addr_var, bg=SPOT, fg=BUFF,
                          font=MONO_13, anchor="w", padx=12, pady=11,
                          cursor="hand2")
        addr_l.pack(fill="x", pady=(6, 4))
        addr_l.bind("<Button-1>", lambda _e: self.copy_address())
        Tooltip(addr_l, "Click to copy")
        brow = tk.Frame(right, bg=DUSK2); brow.pack(anchor="w", pady=(4, 0))
        self.copy_btn = self._btn(brow, "Copy", self.copy_address,
                                  primary=True,
                                  tip="Copy your address — share it to "
                                      "get paid")
        self.copy_btn.pack(side="left")
        self._btn(brow, "Save QR as image…", self._save_qr,
                  tip="Save the QR code as a PNG you can\n"
                      "print or send to someone"
                  ).pack(side="left", padx=(8, 0))
        tk.Label(right, text="Scan the code with a phone, or click the "
                 "address to copy it. Anyone who has it can pay you — "
                 "nobody can take anything with it.",
                 bg=DUSK2, fg=MUTED, font=SANS_9, wraplength=380,
                 justify="left").pack(anchor="w", pady=(12, 0))
        tk.Label(p, text="Incoming payments count toward your balance after "
                 "one confirmation (~2 minutes) and show up under "
                 "Unconfirmed the moment they are sent. Mining rewards "
                 "mature after 10 blocks. Back up your key once — File ▸ "
                 "Backup — and this address is yours forever.",
                 bg=DUSK, fg=MUTED, font=SANS, wraplength=680,
                 justify="left").pack(anchor="w", pady=(14, 0))

    def _draw_qr(self):
        if not hasattr(self, "qr_canvas") or not self.wallet:
            return
        cv = self.qr_canvas
        cv.delete("all")
        M = qr_matrix(self.wallet.address)
        if not M:
            cv.create_text(105, 105, text="QR unavailable", fill="#333")
            return
        n = len(M)
        px = int(cv.cget("width"))
        scale = max(2, px // (n + 8))
        off = (px - n * scale) // 2
        self._qr_scale, self._qr_off = scale, off
        for r in range(n):
            c0 = None
            for c in range(n + 1):
                dark = c < n and M[r][c]
                if dark and c0 is None:
                    c0 = c
                elif not dark and c0 is not None:
                    cv.create_rectangle(off + c0 * scale, off + r * scale,
                                        off + c * scale, off + (r + 1) * scale,
                                        fill="black", width=0)
                    c0 = None
        self._qr_matrix = M

    def _save_qr(self):
        if not getattr(self, "_qr_matrix", None):
            return
        path = filedialog.asksaveasfilename(
            title="Save QR code", defaultextension=".png",
            initialfile="kestrel-address-qr.png",
            filetypes=[("PNG image", "*.png")])
        if not path:
            return
        try:
            M = self._qr_matrix
            n = len(M)
            scale, quiet = 8, 4
            side = (n + 2 * quiet) * scale
            img = tk.PhotoImage(width=side, height=side)
            img.put("white", to=(0, 0, side, side))
            for r in range(n):
                c0 = None
                for c in range(n + 1):
                    dark = c < n and M[r][c]
                    if dark and c0 is None:
                        c0 = c
                    elif not dark and c0 is not None:
                        img.put("black", to=((quiet + c0) * scale,
                                             (quiet + r) * scale,
                                             (quiet + c) * scale,
                                             (quiet + r + 1) * scale))
                        c0 = None
            img.write(path, format="png")
            self.toast("QR code saved.", "good")
        except Exception as e:
            self.toast(f"Could not save the image: {e}", "bad")

    # --------------------------------------------------------- transactions
    def _view_transactions(self, f):
        p = self._page(f, "Transactions",
                       "Everything this wallet has sent, received and mined")
        bar = tk.Frame(p, bg=DUSK); bar.pack(fill="x", pady=(0, 8))
        self._tx_filter_btns = {}
        for name in ("All", "Sent", "Received", "Mined", "Pending"):
            b = tk.Button(bar, text=name, bd=0, relief="flat", bg=DUSK,
                          fg=MUTED, activebackground=DUSK,
                          activeforeground=BUFF, font=SANS_9B, padx=9,
                          pady=2, cursor="hand2",
                          command=lambda n=name: self._set_tx_filter(n))
            b.pack(side="left")
            self._tx_filter_btns[name] = b
        self.tx_search = self._entry(bar, width=26)
        self._placeholder(self.tx_search, "search txid / amount / date…")
        self.tx_search.pack(side="right", ipady=3)
        self.tx_search.bind("<KeyRelease>",
                            lambda _e: self._paint_tx_tables())

        wrap, self.tx_tv = self._tree(
            p, (("date", 150, "w"), ("type", 85, "w"),
                ("transaction id", 200, "w"), ("amount", 140, "e"),
                ("status", 110, "w")),
            height=15, stretch="transaction id")
        wrap.pack(fill="both", expand=True)
        self.tx_tv.bind("<Double-1>", lambda _e: self._tx_details())

        row = tk.Frame(p, bg=DUSK); row.pack(fill="x", pady=(10, 0))
        self._btn(row, "Details", self._tx_details,
                  tip="Everything about the selected transaction\n"
                      "(double-click a row works too)").pack(side="left")
        self._btn(row, "Copy transaction id", self._copy_txid
                  ).pack(side="left", padx=6)
        self._btn(row, "Export CSV…", self._export_csv,
                  tip="Save the list below as a spreadsheet-friendly file"
                  ).pack(side="left")
        self._btn(row, "⟳ Refresh", self._refresh_now,
                  tip="Fetch the newest transactions"
                  ).pack(side="left", padx=6)
        self.tx_count_var = tk.StringVar(value="")
        tk.Label(row, textvariable=self.tx_count_var, bg=DUSK, fg=FAINT,
                 font=TINY).pack(side="right")
        self._set_tx_filter("All")

    def _set_tx_filter(self, name):
        self._tx_filter = name
        for nm, b in self._tx_filter_btns.items():
            b.configure(fg=BUFF if nm == name else MUTED,
                        bg=DUSK3 if nm == name else DUSK)
        self._paint_tx_tables()

    def _selected_row(self):
        sel = self.tx_tv.selection()
        if not sel:
            return None
        vals = self.tx_tv.item(sel[0], "values")
        if not vals or "hint" in self.tx_tv.item(sel[0], "tags"):
            return None
        txid = vals[2]
        for r in self._rows:
            if r["txid"] == txid or mid_ellipsis(r["txid"], 10) == txid:
                return r
        return None

    def _copy_txid(self):
        r = self._selected_row()
        if r:
            self.clipboard_clear()
            self.clipboard_append(r["txid"])
            self.toast("Transaction id copied.", "good")
        else:
            self.toast("Select a transaction first.", "warn")

    def _tx_details(self):
        r = self._selected_row()
        if not r:
            return self.toast("Select a transaction first.", "warn")
        top = self._dialog("Transaction details")
        g = tk.Frame(top, bg=DUSK2)
        g.pack(padx=18, pady=14)
        rows = [
            ("Type", r["kind"]),
            ("Amount", r["amt"]),
            ("Date", r["when"]),
            ("Status", r["status"]),
        ]
        if r.get("height") is not None:
            rows.append(("Block", f"{r['height']:,}"))
        for i, (k, v) in enumerate(rows):
            tk.Label(g, text=k.upper(), bg=DUSK2, fg=FAINT, font=TINY_B
                     ).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 14))
            tk.Label(g, text=v, bg=DUSK2,
                     fg=GREEN if k == "Amount" and r["delta"] >= 0 else BUFF,
                     font=MONO).grid(row=i, column=1, sticky="w", pady=3)
        tk.Label(g, text="TRANSACTION ID", bg=DUSK2, fg=FAINT, font=TINY_B
                 ).grid(row=len(rows), column=0, sticky="w", pady=3,
                        padx=(0, 14))
        e = self._entry(g, width=66)
        e.insert(0, r["txid"])
        e.configure(state="readonly", readonlybackground=SPOT)
        e.grid(row=len(rows), column=1, sticky="w", pady=3)
        rowb = tk.Frame(top, bg=DUSK2)
        rowb.pack(pady=(2, 14))
        self._btn(rowb, "Copy id",
                  lambda: (self.clipboard_clear(),
                           self.clipboard_append(r["txid"]),
                           self.toast("Transaction id copied.", "good"))
                  ).pack(side="left", padx=4)
        self._btn(rowb, "Close", top.destroy).pack(side="left", padx=4)
        self._present_dialog(top)

    def _export_csv(self):
        if not self._rows:
            return self.toast("Nothing to export yet.", "warn")
        path = filedialog.asksaveasfilename(
            title="Export transactions", defaultextension=".csv",
            initialfile="kestrel-transactions.csv",
            filetypes=[("CSV file", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["date", "type", "transaction_id",
                            "amount_ksl", "status"])
                for r in self._rows:
                    w.writerow([r["when"], r["kind"], r["txid"],
                                r["amt"].replace(" KSL", ""), r["status"]])
            self.toast(f"Exported {len(self._rows)} transaction(s).", "good")
        except Exception as e:
            self.toast(f"Could not export: {e}", "bad")

    # ------------------------------------------------------------- contacts
    def _view_contacts(self, f):
        p = self._page(f, "Contacts", "Labeled addresses — double-click to send")
        wrap, self.book_tv = self._tree(
            p, (("label", 180, "w"), ("address", 200, "w")),
            height=11, stretch="address")
        wrap.pack(fill="both", expand=True, pady=(0, 12))
        self.book_tv.bind("<Double-1>", lambda _e: self._book_use())
        row = tk.Frame(p, bg=DUSK); row.pack(fill="x")
        self.bk_label = self._entry(row, width=18)
        self._placeholder(self.bk_label, "name…")
        self.bk_label.pack(side="left", ipady=4)
        self.bk_addr = self._entry(row)
        self._placeholder(self.bk_addr, "K… address")
        self.bk_addr.pack(side="left", fill="x", expand=True, ipady=4, padx=6)
        self.bk_addr.bind("<KeyRelease>", lambda _e: self._book_hint())
        self._btn(row, "Add / Update", self._book_add).pack(side="left")
        self.bk_hint = tk.Label(p, text="", bg=DUSK, fg=FAINT, font=TINY,
                                anchor="w")
        self.bk_hint.pack(fill="x", pady=(3, 0))
        row2 = tk.Frame(p, bg=DUSK); row2.pack(fill="x", pady=(8, 0))
        self._btn(row2, "Use in Send", self._book_use).pack(side="left")
        self._btn(row2, "Copy address", self._book_copy
                  ).pack(side="left", padx=6)
        self._btn(row2, "Delete", self._book_del).pack(side="left")
        self._paint_book()

    def _book_hint(self):
        addr = self._entry_value(self.bk_addr)
        if not addr:
            self.bk_hint.configure(text="", fg=FAINT)
        elif is_valid_address(addr):
            self.bk_hint.configure(text="✓ valid Kestrel address", fg=GREEN)
        else:
            self.bk_hint.configure(
                text="✗ not a Kestrel address yet — they start with K",
                fg=RED)

    def _load_book(self):
        try:
            with open(BOOK_FILE) as fh:
                return dict(json.load(fh))
        except Exception:
            return {}

    def _save_book(self):
        try:
            with open(BOOK_FILE, "w") as fh:
                json.dump(self.book, fh, indent=2)
        except Exception:
            pass

    def _paint_book(self):
        if not hasattr(self, "book_tv"):
            return
        self.book_tv.delete(*self.book_tv.get_children())
        for addr, label in sorted(self.book.items(),
                                  key=lambda kv: kv[1].lower()):
            self.book_tv.insert("", "end", values=(label, addr))
        self._hint_if_empty(self.book_tv,
                            "No contacts yet — add a name and address "
                            "below, or send with a label")
        self._zebra(self.book_tv)

    def _book_add(self):
        addr = self._entry_value(self.bk_addr)
        label = self._entry_value(self.bk_label) or "unnamed"
        if not is_valid_address(addr):
            return self.toast("Kestrel addresses start with K — "
                              "check the address.", "bad")
        self.book[addr] = label
        self._save_book(); self._paint_book()
        self.bk_addr.delete(0, "end"); self.bk_label.delete(0, "end")
        self.bk_hint.configure(text="", fg=FAINT)
        self.toast(f"Saved “{label}”.", "good")
        self._validate_send()

    def _book_sel(self):
        sel = self.book_tv.selection()
        if not sel or "hint" in self.book_tv.item(sel[0], "tags"):
            return None
        return self.book_tv.item(sel[0], "values")

    def _book_use(self):
        v = self._book_sel()
        if v:
            self.to_e.delete(0, "end"); self.to_e.insert(0, v[1])
            self.label_e.delete(0, "end"); self.label_e.insert(0, v[0])
            self.show_view("Send")
            self._validate_send()
            self.amt_e.focus_set()

    def _book_copy(self):
        v = self._book_sel()
        if v:
            self.clipboard_clear(); self.clipboard_append(v[1])
            self.toast("Address copied.", "good")

    def _book_del(self):
        v = self._book_sel()
        if v and v[1] in self.book and messagebox.askyesno(
                "Delete contact?", f"Remove “{v[0]}” from Contacts?"):
            del self.book[v[1]]
            self._save_book(); self._paint_book()

    # ------------------------------------------------------------ plumbing
    def node_url(self):
        return self.node_var.get().strip().rstrip("/")

    def _drain_queue(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "setnode":
                    url, how = rest
                    self.node_var.set(url)
                    self.toast(f"Connected — {how}.", "good")
                    self.refresh()
                elif kind == "status":
                    online, tip, mined, peers, target = rest
                    self.online = online
                    self.tip_height = tip
                    self.dot_l.configure(fg=GREEN if online else RED)
                    self.conn_var.set(
                        (f"Connected · {peers} node(s)" if peers
                         else "Connected") if online else "Offline")
                    host = self.node_url().replace("http://", "")
                    self.node_lbl_var.set(
                        f"node {host}" + (" (built-in)" if self.embedded
                                          else ""))
                    if online and target > tip:
                        self.block_var.set(
                            f"⬇ downloading ledger {tip:,} / {target:,}")
                    else:
                        self.block_var.set(
                            f"block {tip:,} · {mined} mined" if online else
                            "searching for a node…")
                    if self._was_online is not None and \
                            online != self._was_online:
                        self.toast("Connection restored." if online else
                                   "Lost the node — reconnecting…",
                                   "good" if online else "warn")
                    self._was_online = online
                elif kind == "balance":
                    self._apply_balance(*rest)
                elif kind == "note":
                    text, color = rest
                    self.send_note.configure(text=text, fg=color)
                elif kind == "toast":
                    self.toast(*rest)
                elif kind == "connfail":
                    title, msg = rest
                    self.toast("Couldn't connect — see the details.", "bad")
                    messagebox.showwarning(title, msg)
                elif kind == "sharedlg":
                    self._show_share_dialog(*rest)
                elif kind == "refresh":
                    self._refresh_now()
                elif kind == "sent":
                    self.to_e.delete(0, "end"); self.amt_e.delete(0, "end")
                    self.label_e.delete(0, "end")
                    self._validate_send()
                    self.refresh()
                    self.after(2000, self.refresh)
                elif kind == "senddone":
                    self.send_btn.configure(state="normal", text="Send")
                    self._validate_send()
        except queue.Empty:
            pass
        self.after(150, self._drain_queue)

    def _poll(self):
        threading.Thread(target=self._poll_worker, args=(self.node_url(),),
                         daemon=True).start()
        # poll the node less often while minimized (saves CPU and network)
        self.after(20000 if self.state() == "iconic" else 6000, self._poll)

    def _poll_worker(self, url):
        try:
            s = http_json("GET", url + "/supply", timeout=5)
            self.q.put(("status", True, s["height"],
                        s["circulating_ksl"].replace(" KSL", ""),
                        s.get("peers_alive", 0),
                        s.get("sync_target", 0)))
        except Exception:
            self.q.put(("status", False, 0, "", 0, 0))

    def _auto_refresh(self):
        if self.state() != "iconic":    # don't churn balance/history while hidden
            self.refresh()
        self.after(30000 if self.state() == "iconic" else 12000,
                   self._auto_refresh)

    # -------------------------------------------------------------- wallet
    def new_wallet(self):
        if self.wallet and not messagebox.askyesno(
                "Replace wallet?",
                "A wallet already exists here. Creating a new one replaces "
                "it in this app.\n\nIf the old one holds KSL, use File ▸ "
                "Backup first.\n\nContinue?"):
            return
        self.wallet = Wallet.create()
        self.wallet.save(WALLET_FILE)
        self.addr_var.set(self.wallet.address)
        self.settings["backed_up"] = False
        save_settings(self.settings)
        self._update_banner()
        self._draw_qr()
        self.show_view("Receive")
        messagebox.showinfo(
            "Back up your key",
            "Your new address:\n" + self.wallet.address +
            "\n\nNow use File ▸ Backup (or Show backup key) and store it "
            "safely — it is the only way to restore your wallet.")
        self.refresh()

    def import_wif(self):
        wif = self._ask_string("Restore wallet",
                               "Paste your backup key (right-click ▸ Paste "
                               "works too):", secret=True)
        if not wif:
            return
        try:
            self.wallet = Wallet.from_wif(wif.strip())
            self.wallet.save(WALLET_FILE)
            self.addr_var.set(self.wallet.address)
            self.settings["backed_up"] = True
            save_settings(self.settings)
            self._update_banner()
            self._draw_qr()
            self.refresh()
            self.toast("Wallet restored — balance loads in a moment.",
                       "good")
        except Exception as e:
            messagebox.showerror("Restore failed", str(e))

    def show_wif(self):
        if not self.wallet:
            return messagebox.showinfo("No wallet", "Create a wallet first.")
        top = self._dialog("Your backup key — keep it secret")
        tk.Label(top, text="Write this down. Anyone with it can spend your "
                 "KSL,\nand it is the only way to restore your wallet.",
                 bg=DUSK2, fg=RED, font=SANS, padx=16).pack(pady=(14, 4))
        e = self._entry(top, width=56)
        e.insert(0, private_to_wif(self.wallet.private_key))
        e.configure(state="readonly", readonlybackground=SPOT)
        e.pack(padx=16, pady=6, ipady=4)
        row = tk.Frame(top, bg=DUSK2); row.pack(pady=(4, 14))
        self._btn(row, "Copy",
                  lambda: (self.clipboard_clear(),
                           self.clipboard_append(
                               private_to_wif(self.wallet.private_key)),
                           self.toast("Key copied — paste it somewhere "
                                      "safe, then clear the clipboard.",
                                      "warn"))
                  ).pack(side="left", padx=4)
        self._btn(row, "Close", top.destroy).pack(side="left", padx=4)
        self._present_dialog(top, modal=False)

    def copy_address(self):
        if self.wallet:
            self.clipboard_clear()
            self.clipboard_append(self.wallet.address)
            if hasattr(self, "copy_btn"):
                self.copy_btn.configure(text="Copied ✓")
                self.after(1400, lambda: self.copy_btn.configure(text="Copy"))
            self.toast("Address copied — share it to get paid.", "good")

    def _refresh_now(self):
        self.refresh()
        threading.Thread(target=self._poll_worker, args=(self.node_url(),),
                         daemon=True).start()

    # ------------------------------------------------------------- balance
    def refresh(self):
        if not self.wallet:
            return
        threading.Thread(target=self._refresh_worker,
                         args=(self.node_url(), self.wallet.address),
                         daemon=True).start()

    def _refresh_worker(self, url, address):
        try:
            a = http_json("GET", f"{url}/address/{address}", timeout=8)
        except Exception:
            return
        pending = []
        try:
            mem = http_json("GET", f"{url}/mempool", timeout=6)
            for v in mem.get("transactions", []):
                inn = sum(o.get("amount", 0) for o in v.get("outputs", [])
                          if o.get("address") == address)
                out = sum(i.get("amount", 0) for i in v.get("inputs", [])
                          if i.get("address") == address)
                if inn or out:
                    pending.append({
                        "txid": v.get("txid", ""),
                        "timestamp": v.get("timestamp", time.time()),
                        "delta": inn - out,
                        "outgoing": out > 0,
                    })
        except Exception:
            pass
        self.q.put(("balance", a, pending))

    def _apply_balance(self, a, pending):
        self._avail = a["spendable"]
        self._history = a["history"]
        self._pending = pending
        self.avail_var.set(format_ksl(a["spendable"]))
        self.immature_var.set(format_ksl(a["confirmed"] - a["spendable"]))
        self.total_var.set(format_ksl(a["confirmed"]))
        p_in = sum(p["delta"] for p in pending if p["delta"] > 0)
        p_out = -sum(p["delta"] for p in pending if p["delta"] < 0)
        if p_in and p_out:
            self.pending_var.set(f"+{format_ksl(p_in)} / "
                                 f"−{format_ksl(p_out)}")
        elif p_in:
            self.pending_var.set("+" + format_ksl(p_in))
        elif p_out:
            self.pending_var.set("−" + format_ksl(p_out))
        else:
            self.pending_var.set("—")
        self._validate_send()
        self._paint_tx_tables()

    def _row_of(self, h):
        when = time.strftime("%d %b %Y, %H:%M", time.localtime(h["timestamp"]))
        if h.get("coinbase"):
            kind, tag = "Mined", "pos"
        elif h["delta"] >= 0:
            kind, tag = "Received", "pos"
        else:
            kind, tag = "Sent", "neg"
        amt = ("+" if h["delta"] >= 0 else "") + format_ksl(h["delta"])
        confs = max(self.tip_height - h["height"] + 1, 1)
        if h.get("coinbase") and confs < 10:
            status = f"Immature {confs}/10"
        else:
            status = f"{min(confs, 999)} conf"
        return {"when": when, "kind": kind, "txid": h["txid"], "amt": amt,
                "status": status, "tag": tag, "delta": h["delta"],
                "height": h.get("height"), "ts": h["timestamp"]}

    def _pending_row(self, p):
        when = time.strftime("%d %b %Y, %H:%M", time.localtime(p["timestamp"]))
        kind = "Sending" if p["outgoing"] else "Receiving"
        amt = ("+" if p["delta"] >= 0 else "") + format_ksl(p["delta"])
        return {"when": when, "kind": kind, "txid": p["txid"], "amt": amt,
                "status": "Pending", "tag": "pend", "delta": p["delta"],
                "height": None, "ts": p["timestamp"]}

    def _match_tx_filter(self, r):
        f = self._tx_filter
        if f == "All":
            return True
        if f == "Pending":
            return r["status"] == "Pending"
        return {"Sent": ("Sent", "Sending"),
                "Received": ("Received", "Receiving"),
                "Mined": ("Mined",)}.get(f, ()).__contains__(r["kind"])

    def _paint_tx_tables(self):
        if not hasattr(self, "recent_tv"):
            return
        rows = [self._pending_row(p)
                for p in sorted(self._pending, key=lambda p: -p["ts"])]
        rows += [self._row_of(h) for h in self._history]
        self._rows = rows

        self.recent_tv.delete(*self.recent_tv.get_children())
        for r in rows[:7]:
            self.recent_tv.insert(
                "", "end",
                values=(r["when"], r["kind"], r["amt"], r["status"]),
                tags=(r["tag"],))
        self._hint_if_empty(self.recent_tv,
                            "Nothing yet — mine or receive KSL and it "
                            "shows up here")
        self._zebra(self.recent_tv)

        if hasattr(self, "tx_tv"):
            needle = self._entry_value(self.tx_search).lower() \
                if hasattr(self, "tx_search") else ""
            self.tx_tv.delete(*self.tx_tv.get_children())
            shown = 0
            for r in rows:
                if not self._match_tx_filter(r):
                    continue
                if needle and needle not in \
                        f'{r["when"]} {r["kind"]} {r["txid"]} {r["amt"]} ' \
                        f'{r["status"]}'.lower():
                    continue
                self.tx_tv.insert(
                    "", "end",
                    values=(r["when"], r["kind"],
                            mid_ellipsis(r["txid"], 10), r["amt"],
                            r["status"]),
                    tags=(r["tag"],))
                shown += 1
            self._hint_if_empty(self.tx_tv,
                                "No transactions match — try another "
                                "filter, or share your Receive address")
            self._zebra(self.tx_tv)
            note = f"{shown} shown"
            if len(self._history) >= 50:
                note += " · showing the most recent 50 confirmed"
            self.tx_count_var.set(note)
        self.updated_var.set("updated " + time.strftime("%H:%M:%S"))

    # ---------------------------------------------------------------- send
    def fill_max(self):
        try:
            fee = parse_ksl(self.fee_e.get())
        except Exception:
            fee = params.MIN_RELAY_FEE
        amt = max(0, self._avail - fee)
        self.amt_e.delete(0, "end")
        self.amt_e.insert(0, format_ksl(amt).replace(" KSL", ""))
        self._validate_send()

    def _confirm_send(self, to, label, amount, fee):
        top = self._dialog("Confirm payment")
        out = {"ok": False}
        g = tk.Frame(top, bg=DUSK2)
        g.pack(padx=20, pady=(16, 8))
        to_disp = to if not label else f"{to}\n“{label}”"
        rows = [("Send", format_ksl(amount)),
                ("To", to_disp),
                ("Fee", format_ksl(fee)),
                ("Total", format_ksl(amount + fee)),
                ("Left after", format_ksl(max(self._avail - amount - fee,
                                              0)))]
        for i, (k, v) in enumerate(rows):
            tk.Label(g, text=k.upper(), bg=DUSK2, fg=FAINT, font=TINY_B
                     ).grid(row=i, column=0, sticky="nw", pady=3,
                            padx=(0, 16))
            tk.Label(g, text=v, bg=DUSK2,
                     fg=BUFF if k != "Send" else GREEN,
                     font=MONO if k != "Send" else MONO_11B,
                     justify="left").grid(row=i, column=1, sticky="w", pady=3)
        tk.Label(top, text="Payments cannot be reversed.",
                 bg=DUSK2, fg=AMBER, font=SANS_9).pack()
        row = tk.Frame(top, bg=DUSK2)
        row.pack(pady=(8, 16))

        def ok(_e=None):
            out["ok"] = True
            top.destroy()

        self._btn(row, "Confirm and send", ok, primary=True
                  ).pack(side="left", padx=4)
        self._btn(row, "Cancel", top.destroy).pack(side="left", padx=4)
        top.bind("<Return>", ok)
        self._present_dialog(top)
        top.wait_window()
        return out["ok"]

    def send(self):
        if not self.wallet:
            return messagebox.showinfo("No wallet",
                                       "Create a wallet first (File menu).")
        if not self._validate_send():
            return
        to = self.to_e.get().strip()
        amount = parse_ksl(self.amt_e.get())
        fee = parse_ksl(self.fee_e.get())
        label = self.label_e.get().strip()
        if not self._confirm_send(to, label or self.book.get(to, ""),
                                  amount, fee):
            return
        if label:
            self.book[to] = label
            self._save_book(); self._paint_book()
        self.send_btn.configure(state="disabled", text="Sending…")
        threading.Thread(target=self._send_worker,
                         args=(self.node_url(), to, amount, fee),
                         daemon=True).start()

    def _send_worker(self, url, to, amount, fee):
        try:
            utxos = http_json("GET",
                              f"{url}/utxos/{self.wallet.address}",
                              timeout=8)["utxos"]
            tx = self.wallet.build_transaction(utxos, to, amount, fee)
            try:
                http_json("POST", url + "/tx", {"tx": tx.to_dict()},
                          timeout=10)
            except urllib.error.HTTPError as e:
                # surface the node's real reason ("fee below minimum…"),
                # not a bare "HTTP Error 400"
                try:
                    detail = json.loads(e.read()).get("error") or str(e)
                except Exception:
                    detail = str(e)
                raise ValidationError(detail) from None
            self.q.put(("note",
                        f"✓ Sent {format_ksl(amount)}. The next block "
                        "confirms it — about 2 minutes.", GREEN))
            self.q.put(("toast", f"✓ Sent {format_ksl(amount)} — "
                                 "watch it confirm under Transactions.",
                        "good"))
            self.q.put(("sent",))
        except ValidationError as e:
            self.q.put(("note", f"Could not send: {e}", RED))
            self.q.put(("toast", f"Could not send: {e}", "bad"))
        except Exception as e:
            self.q.put(("note", f"Could not send: {e}", RED))
            self.q.put(("toast", f"Could not send: {e}", "bad"))
        finally:
            self.q.put(("senddone",))

    def _quit(self):
        try:
            self.settings["geometry"] = self.geometry()
            save_settings(self.settings)
        except Exception:
            pass
        if self.embedded:
            try:
                self.embedded.stop()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
