#!/usr/bin/env python3
"""
Kestrel Miner — the desktop mining application.

Zero setup: on first launch it creates your reward address (showing the
backup key once) and immediately starts a full node on this machine. The
node auto-connects — seed nodes, saved peers, LAN auto-discovery and
worldwide DHT discovery — so you are mining on a live network out of the
box. Mine to your own address by pasting it in (right-click or the Paste
button); it is remembered. Sidebar views: Mine, Explorer, Network,
Activity.

Requires Python 3.10+ with tkinter and the `ecdsa` package.
"""

import collections
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
import webbrowser
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (_HERE, os.path.abspath(os.path.join(_HERE, "..", ".."))):
    if os.path.isdir(os.path.join(cand, "kestrel")):
        sys.path.insert(0, cand)
        break

from kestrel import (params, updates, announcements,        # noqa: E402
                     __version__ as KVER)
from kestrel.blockchain import Blockchain, ValidationError   # noqa: E402
from kestrel.wallet import Wallet, format_ksl, parse_ksl    # noqa: E402
from kestrel.crypto_utils import is_valid_address, private_to_wif  # noqa: E402
from kestrel.miner import assemble_candidate, find_pow, default_threads  # noqa: E402
from kestrel.node import Node                                # noqa: E402
from kestrel.discovery import get_lan_ip                     # noqa: E402


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

import tkinter as tk                                         # noqa: E402
from tkinter import ttk, filedialog                          # noqa: E402
from tkinter import font as tkfont                           # noqa: E402

DATA_DIR = os.path.join(_HERE, "kestrel-data")
WALLET_FILE = os.path.join(_HERE, "kestrel-wallet.json")
SETTINGS_FILE = os.path.join(_HERE, "miner-settings.json")
PORTS = tuple(params.DEFAULT_PORT + i for i in range(10))

# ------------------------------------------------------------------ palette
# How often a running app re-checks the announcement feed. It also checks
# once on startup, so this only governs long-lived sessions. Kept modest:
# it is a small text file on a CDN, and 25 minutes is roughly 58 requests
# a day per app.
ANNOUNCE_EVERY_MS = 25 * 60 * 1000

DUSK, DUSK2, DUSK3, SPOT = "#1B212C", "#222A38", "#2A3444", "#10141B"
RAIL = "#141924"
BUFF, MUTED, FAINT = "#EAE1CE", "#A79F8D", "#736c5e"
RUFOUS, RUFOUS_HI = "#C4552A", "#DB6636"
GREEN, RED, SLATE = "#5FA46A", "#C15b4b", "#8CA7C4"
AMBER = "#D9A441"
ZEBRA = "#131822"
HOVER = "#33405A"
GRID = "#202836"

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
BIGBTN = ("Segoe UI", 12, "bold")
MONO = ("Consolas", 10)
MONO_9 = ("Consolas", 9)
MONO_8 = ("Consolas", 8)
MONO_7 = ("Consolas", 7)
MONO_13 = ("Consolas", 13)
MONO_15B = ("Consolas", 15, "bold")
MONO_20B = ("Consolas", 20, "bold")


def _resolve_fonts(root):
    """Pick the best available family per platform and rebuild font tuples."""
    global SANS, SANS_B, SANS_9, SANS_9B, TINY, TINY_B, MICRO_B, TITLE
    global BRAND, BIGBTN, MONO, MONO_9, MONO_8, MONO_7, MONO_13
    global MONO_15B, MONO_20B
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
    BIGBTN = (sans, 12, "bold")
    MONO = (mono, 10)
    MONO_9 = (mono, 9)
    MONO_8 = (mono, 8)
    MONO_7 = (mono, 7)
    MONO_13 = (mono, 13)
    MONO_15B = (mono, 15, "bold")
    MONO_20B = (mono, 20, "bold")


# ------------------------------------------------------------------ helpers
def port_free(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); return True
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


def fmt_rate(r: float) -> str:
    if r >= 1e9:
        return f"{r / 1e9:,.2f} GH/s"
    if r >= 1e6:
        return f"{r / 1e6:,.2f} MH/s"
    if r >= 10_000:
        return f"{r / 1e3:,.1f} kH/s"
    return f"{r:,.0f} H/s"


def fmt_count(n: float) -> str:
    if n >= 1e12:
        return f"{n / 1e12:,.2f}T"
    if n >= 1e9:
        return f"{n / 1e9:,.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:,.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:,.1f}k"
    return f"{n:,.0f}"


def fmt_span(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    if d < 5:
        return "just now"
    if d < 90:
        return f"{d:.0f}s ago"
    if d < 5400:
        return f"{d / 60:.0f}m ago"
    if d < 129600:
        return f"{d / 3600:.1f}h ago"
    return f"{d / 86400:.1f}d ago"


def mid_ellipsis(s: str, keep: int = 10) -> str:
    if len(s) <= keep * 2 + 1:
        return s
    return s[:keep] + "…" + s[-keep:]


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


def invite_text(url: str) -> str:
    """A ready-to-paste message that gets a friend onto the network."""
    return (
        "Join my Kestrel (KSL) network — takes a minute:\n"
        "\n"
        "1. Get Kestrel Miner (or Kestrel Wallet) and open it —\n"
        "   it runs a node for you automatically.\n"
        f"2. In the app: Network ▸ “Add a node by address” ▸ paste\n"
        f"   {url}\n"
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


# ------------------------------------------------------ connection repair
# Opening the node's port so friends can connect IN has two common
# blockers: the local firewall (Windows Firewall on this PC) and the
# router (needs a port-forward, which UPnP tries automatically). This
# helper can, WITH THE USER'S EXPLICIT CONSENT, add a single inbound-allow
# firewall rule for the node's port. On Windows that needs administrator
# rights, so Windows shows its standard permission prompt (UAC) which the
# user approves. Nothing here disables the firewall or touches any other
# rule; everything is reversible with fw_remove().

def fw_rule_name(port: int) -> str:
    return f"Kestrel node (TCP {port})"


def fw_add_command(port: int) -> str:
    """The exact shell command shown to the user and run elevated."""
    name = fw_rule_name(port)
    return (
        f'netsh advfirewall firewall delete rule name="{name}" '
        f'>nul 2>&1 & '
        f'netsh advfirewall firewall add rule name="{name}" '
        f'dir=in action=allow protocol=TCP localport={port}'
    )


def fw_remove_command(port: int) -> str:
    return (f'netsh advfirewall firewall delete rule '
            f'name="{fw_rule_name(port)}"')


def fw_rule_present(port: int) -> bool:
    """True if our inbound rule already exists. Reading rules needs no
    admin rights, so this is a reliable, side-effect-free verification."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={fw_rule_name(port)}"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.returncode == 0 and "Rule Name" in out.stdout and \
            str(port) in out.stdout
    except Exception:
        return False


def _win_run_elevated(command: str, timeout: int = 120) -> tuple:
    """Run `cmd /c command` with a UAC elevation prompt, wait for it, and
    return (launched, exit_code, note). Never raises."""
    import ctypes
    from ctypes import wintypes

    class SEE(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    sei = SEE()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"           # triggers the UAC consent prompt
    sei.lpFile = "cmd.exe"
    sei.lpParameters = f'/c {command}'
    sei.nShow = SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == 1223:            # ERROR_CANCELLED — user said No at UAC
            return (False, None, "cancelled")
        return (False, None, f"could not start (error {err})")
    h = sei.hProcess
    if not h:
        return (True, None, "started")
    ctypes.windll.kernel32.WaitForSingleObject(h, timeout * 1000)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return (True, int(code.value), "done")


def fw_apply(add: bool, port: int) -> tuple:
    """Add (or remove) the inbound rule with consent/elevation.
    Returns (ok: bool, message: str). Windows only; safe elsewhere."""
    if sys.platform != "win32":
        return (False, "Firewall changes are only automated on Windows.")
    cmd = fw_add_command(port) if add else fw_remove_command(port)
    launched, _code, note = _win_run_elevated(cmd)
    if not launched:
        if note == "cancelled":
            return (False, "You clicked No on the Windows permission "
                           "prompt, so nothing was changed.")
        return (False, f"Windows would not run the change ({note}).")
    present = fw_rule_present(port)
    if add:
        return (present, "Firewall rule added." if present else
                "The rule doesn't seem to have been added.")
    return (not present, "Firewall rule removed." if not present else
            "The rule is still present.")


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
    VIEWS = ("Mine", "Wallet", "Explorer", "Network", "Activity")
    ICONS = {"Mine": "⚒", "Wallet": "◈", "Explorer": "◎", "Network": "⇄",
             "Activity": "≣"}

    def __init__(self):
        super().__init__()
        _resolve_fonts(self)
        self.title(f"Kestrel Miner {KVER}")
        self.configure(bg=DUSK)
        self.minsize(880, 600)
        self.settings = load_settings()
        self._restore_geometry()
        self._make_icon()

        self.chain = Blockchain(data_dir=DATA_DIR)
        # the embedded full node — its lock is THE lock, so the miner and the
        # node never race on the chain. Auto-connects on launch: seeds, saved
        # peers, LAN discovery and the worldwide DHT, no setup needed.
        self.node = Node(self.chain, host="0.0.0.0", port=self._pick_port())
        self.lock = self.node.lock
        self.node.on_log = self._node_log
        self.node_serving = False

        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.mining = threading.Event()
        self.blocks_found = 0
        self.session_feathers = 0
        self.samples = collections.deque(maxlen=120)
        self.session_hashes = 0.0
        self.session_secs = 0.0
        self._last_rate_ts = None
        self._cur_rate = 0.0
        self._block_work = 0

        self.toasts = Toasts(self)
        self._log_recs = collections.deque(maxlen=1500)
        self._log_filter = "All"

        self._init_style()
        self._build_menu()
        self._build_ui()
        self._bind_keys()
        self._ensure_address()
        self._refresh_stats()
        self.after(150, self._drain_queue)
        self.after(400, self.start_node)       # auto-run the node on launch
        self.after(3000, self._tick)           # periodic stats + peer table
        if self.settings.get("autostart"):
            self.after(2500, self._autostart)
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
            # a simple kestrel mark: rufous peak with a buff eye
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

    @staticmethod
    def _pick_port():
        for p in PORTS:
            if port_free(p):
                return p
        return PORTS[0]

    def _bind_keys(self):
        for i, name in enumerate(self.VIEWS, start=1):
            self.bind_all(f"<Control-Key-{i}>",
                          lambda _e, n=name: self.show_view(n))
        self.bind_all("<F5>", lambda _e: self._tick_now())
        self.bind_all("<Control-m>", lambda _e: self.toggle())

    # -------------------------------------------------------------- theming
    def _init_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("KV.Treeview", background=SPOT, fieldbackground=SPOT,
                    foreground=BUFF, bordercolor=DUSK3, borderwidth=0,
                    rowheight=27, font=SANS_9)
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

    # --------------------------------------------------------------- dialogs

    def _modal(self, title, message, kind="info", ok="OK", cancel=None,
               link=None):
        """The app's own dialog, in place of a native OS message box.

        Returns True when the primary button is chosen and False
        otherwise, so it can stand in for both showinfo and askyesno.
        Escape and the window close button both count as False — the safe
        answer for a confirmation, and harmless for a plain notice.

        A `link` is shown as selectable read-only text. Nothing here ever
        opens a browser on the reader's behalf.
        """
        accent = {"info": SLATE, "good": GREEN, "warn": AMBER,
                  "error": RED}.get(kind, SLATE)
        out = {"ok": False}

        top = self._dialog(title)
        tk.Frame(top, bg=accent, height=3).pack(fill="x")
        body = tk.Frame(top, bg=DUSK2, padx=24, pady=20)
        body.pack(fill="both", expand=True)

        tk.Label(body, text=title, bg=DUSK2, fg=BUFF, font=SANS_B,
                 anchor="w", justify="left", wraplength=440).pack(fill="x")
        tk.Label(body, text=message, bg=DUSK2, fg=MUTED, font=SANS,
                 anchor="w", justify="left", wraplength=440
                 ).pack(fill="x", pady=(9, 0))

        if link:
            e = tk.Entry(body, bg=DUSK3, fg=SLATE, font=MONO_9, relief="flat",
                         bd=0, readonlybackground=DUSK3, highlightthickness=0)
            e.insert(0, link)
            e.configure(state="readonly")
            e.pack(fill="x", pady=(13, 0), ipady=7)

        row = tk.Frame(body, bg=DUSK2)
        row.pack(fill="x", pady=(20, 0))

        def done(v):
            out["ok"] = v
            top.destroy()

        primary = self._btn(row, ok, lambda: done(True), primary=True)
        primary.pack(side="right")
        if cancel:
            self._btn(row, cancel, lambda: done(False)).pack(side="right",
                                                             padx=(0, 8))
        top.protocol("WM_DELETE_WINDOW", lambda: done(False))
        top.bind("<Escape>", lambda _e: done(False))
        top.bind("<Return>", lambda _e: done(True))
        self._present_dialog(top)
        primary.focus_set()
        self.wait_window(top)
        return out["ok"]

    def _say(self, title, message, kind="info", link=None):
        """Styled stand-in for the old messagebox.showinfo."""
        return self._modal(title, message, kind=kind, link=link)

    def _error(self, title, message):
        """Styled stand-in for the old messagebox.showerror."""
        return self._modal(title, message, kind="error")

    def _warn(self, title, message):
        """Styled stand-in for the old messagebox.showwarning."""
        return self._modal(title, message, kind="warn")

    def _ask(self, title, message):
        """Styled stand-in for the old messagebox.askyesno."""
        return self._modal(title, message, kind="warn", ok="Continue",
                           cancel="Cancel")

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
        tv.tag_configure("dim", foreground=FAINT)
        tv.tag_configure("mine", foreground=RUFOUS_HI)
        self._style_table(tv)
        return wrap, tv

    # ----------------------------------------------------------------- menu
    def _build_menu(self):
        mk = dict(bg=DUSK2, fg=BUFF, activebackground=DUSK3,
                  activeforeground=BUFF, bd=0, font=SANS)
        bar = tk.Menu(self, **mk)
        fm = tk.Menu(bar, tearoff=0, **mk)
        fm.add_command(label="Backup reward key…", command=self._backup_key)
        fm.add_command(label="New reward address…", command=self.make_address)
        fm.add_separator()
        fm.add_command(label="Open data folder",
                       command=lambda: open_folder(_HERE))
        fm.add_separator()
        fm.add_command(label="Exit", command=self._quit)
        bar.add_cascade(label="File", menu=fm)
        sm = tk.Menu(bar, tearoff=0, **mk)
        self.announce_var = tk.BooleanVar(
            value=bool(self.settings.get("announcements", True)))
        sm.add_checkbutton(label="Show announcements from the project",
                           variable=self.announce_var,
                           onvalue=True, offvalue=False,
                           command=self._save_announcements_pref)
        sm.add_command(label="Forget dismissed announcements",
                       command=self._reset_announcements)
        bar.add_cascade(label="Settings", menu=sm)
        hm = tk.Menu(bar, tearoff=0, **mk)
        hm.add_command(label="About Kestrel Miner", command=self._about)
        hm.add_command(label="Keyboard shortcuts", command=self._shortcuts)
        hm.add_separator()
        hm.add_command(label="Open node API in browser",
                       command=lambda: webbrowser.open(
                           f"http://127.0.0.1:{self.node.port}/"))
        if sys.platform == "win32":
            hm.add_command(label="Remove firewall rule",
                           command=self._remove_firewall_rule)
        bar.add_cascade(label="Help", menu=hm)
        self.config(menu=bar)

    def _about(self):
        self._say(
            "About",
            f"Kestrel Miner {KVER}\n\nScrypt proof-of-work miner for Kestrel "
            "(KSL) with a full node built in.\nEvery block found pays the "
            "block reward to your address.\n\nOpen source, MIT license.")

    def _shortcuts(self):
        self._say(
            "Keyboard shortcuts",
            "Ctrl+1…4   switch view (Mine / Explorer / Network / Activity)\n"
            "Ctrl+M      start / stop mining\n"
            "F5             refresh everything now\n"
            "Ctrl+A        select all in any text box\n"
            "Right-click  cut / copy / paste in any text box")

    def _backup_key(self):
        if not os.path.exists(WALLET_FILE):
            return self._say("No address yet",
                                       "Create a reward address first.")
        path = filedialog.asksaveasfilename(
            title="Backup reward key", defaultextension=".json",
            initialfile="kestrel-wallet-backup.json",
            filetypes=[("Wallet file", "*.json")])
        if path:
            shutil.copyfile(WALLET_FILE, path)
            self.toast("Key file copied — store it safely.", "good")

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        body = self.body_frame = tk.Frame(self, bg=DUSK)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        rail = tk.Frame(body, bg=RAIL, width=182)
        rail.grid(row=0, column=0, sticky="nsw")
        rail.pack_propagate(False)
        brand = tk.Frame(rail, bg=RAIL)
        brand.pack(fill="x", padx=18, pady=(18, 22))
        tk.Label(brand, text="▲ kestrel", bg=RAIL, fg=BUFF,
                 font=BRAND).pack(anchor="w")
        tk.Label(brand, text="MINER", bg=RAIL, fg=RUFOUS_HI,
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
        self.net_pill = tk.StringVar(value="starting node…")
        row = tk.Frame(foot, bg=RAIL); row.pack(anchor="w")
        self.dot_l = tk.Label(row, text="●", bg=RAIL, fg=FAINT,
                              font=SANS)
        self.dot_l.pack(side="left")
        tk.Label(row, textvariable=self.net_pill, bg=RAIL, fg=MUTED,
                 font=SANS_9).pack(side="left", padx=(5, 0))
        self.mine_pill = tk.StringVar(value="")
        tk.Label(foot, textvariable=self.mine_pill, bg=RAIL, fg=RUFOUS_HI,
                 font=TINY).pack(anchor="w", pady=(3, 0))
        tk.Label(foot, text=f"v{KVER} · MIT", bg=RAIL, fg=FAINT,
                 font=MONO_8).pack(anchor="w", pady=(4, 0))

        content = tk.Frame(body, bg=DUSK)
        content.grid(row=0, column=1, sticky="nsew")
        content.rowconfigure(0, weight=1); content.columnconfigure(0, weight=1)
        self._views = {}
        for name, builder in (("Mine", self._view_mine),
                              ("Wallet", self._view_wallet),
                              ("Explorer", self._view_explorer),
                              ("Network", self._view_network),
                              ("Activity", self._view_activity)):
            f = tk.Frame(content, bg=DUSK)
            f.grid(row=0, column=0, sticky="nsew")
            builder(f)
            self._views[name] = f

        # A quiet strip that only ever appears if a newer version exists.
        self.update_bar = tk.Frame(self, bg=DUSK3)
        self.update_msg = tk.StringVar(value="")
        tk.Label(self.update_bar, textvariable=self.update_msg, bg=DUSK3,
                 fg=BUFF, font=SANS_9, anchor="w", padx=14,
                 pady=6).pack(side="left", fill="x", expand=True)
        self._btn(self.update_bar, "Get it", self._open_releases,
                  primary=True, tip="Open the downloads page"
                  ).pack(side="right", padx=(0, 8), pady=4)
        self._btn(self.update_bar, "Later",
                  lambda: self.update_bar.pack_forget(),
                  tip="Hide this until next launch"
                  ).pack(side="right", padx=(0, 6), pady=4)
        # deliberately not packed — shown only by _offer_update

        # Announcements from the project. This sits directly under the
        # title bar rather than at the foot of the window: the bottom edge
        # already carries the update strip and the status line, and a
        # third strip down there reads as chrome and gets ignored.
        self.note_bar = tk.Frame(self, bg=DUSK2)
        self.note_spine = tk.Frame(self.note_bar, bg=SLATE, width=3)
        self.note_spine.pack(side="left", fill="y")

        acts = tk.Frame(self.note_bar, bg=DUSK2)
        acts.pack(side="right", padx=14, pady=9)
        self._btn(acts, "Read", self._read_note, primary=True,
                  tip="Show the full message").pack(side="right")
        self._btn(acts, "Dismiss", self._dismiss_note,
                  tip="Hide this announcement for good"
                  ).pack(side="right", padx=(0, 8))
        self.note_more = tk.Label(acts, text="", bg=DUSK2, fg=FAINT,
                                  font=SANS_9)
        self.note_more.pack(side="right", padx=(0, 12))

        inner = tk.Frame(self.note_bar, bg=DUSK2)
        inner.pack(side="left", fill="both", expand=True, padx=14, pady=9)
        self.note_kind = tk.StringVar(value="ANNOUNCEMENT")
        self.note_kind_lbl = tk.Label(inner, textvariable=self.note_kind,
                                      bg=DUSK2, fg=SLATE, font=SANS_9B,
                                      anchor="w")
        self.note_kind_lbl.pack(fill="x")
        self.note_msg = tk.StringVar(value="")
        tk.Label(inner, textvariable=self.note_msg, bg=DUSK2, fg=BUFF,
                 font=SANS, anchor="w", justify="left"
                 ).pack(fill="x", pady=(2, 0))
        # deliberately not packed — shown only by _render_note

        sb = tk.Frame(self, bg=SPOT)
        sb.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar()
        self.updated_var = tk.StringVar(value="")
        rbtn = tk.Button(sb, text="⟳", command=self._tick_now, bg=SPOT,
                         fg=MUTED, activebackground=DUSK3,
                         activeforeground=BUFF, relief="flat", bd=0,
                         font=SANS_B, cursor="hand2", padx=10)
        rbtn.pack(side="right", fill="y")
        rbtn.bind("<Enter>", lambda _e: rbtn.configure(fg=BUFF))
        rbtn.bind("<Leave>", lambda _e: rbtn.configure(fg=MUTED))
        Tooltip(rbtn, "Refresh everything now (F5)")
        tk.Label(sb, textvariable=self.updated_var, bg=SPOT, fg=FAINT,
                 font=TINY, padx=4).pack(side="right")
        tk.Label(sb, textvariable=self.status_var, bg=SPOT, fg=MUTED,
                 font=SANS_9, anchor="w", padx=14,
                 pady=5).pack(side="left", fill="x", expand=True)
        self.show_view("Mine")
        updates.check(self._offer_update)
        self._check_announcements()

    def _open_releases(self):
        import webbrowser
        webbrowser.open(updates.RELEASES_PAGE)

    def _offer_update(self, latest, url):
        """Called from the update check — only when one really exists."""
        def show():
            self.update_msg.set(
                f"Kestrel {latest} is available — you're on {KVER}. "
                f"Updating keeps you in step with the network.")
            self.update_bar.pack(fill="x", side="bottom")
        self.after(0, show)

    # ------------------------------------------------------ announcements

    def _check_announcements(self):
        """Ask GitHub for project announcements, unless the user said no.

        Runs once at startup and every ANNOUNCE_EVERY_MS after that.

        With the setting off nothing is fetched at all — the choice turns
        the network request off, not merely the display of its result.

        Three places call this: startup, switching the setting back on,
        and un-dismissing. Each would otherwise start its own repeating
        timer, so flipping the setting a few times would leave several
        chains running and multiply the polling rate. Cancelling first
        keeps exactly one alive no matter how it was reached.
        """
        job = getattr(self, "_note_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._note_job = None
        if not self.settings.get("announcements", True):
            return
        seen = self.settings.get("announcements_seen", [])
        announcements.check(self._on_announcements, seen)
        self._note_job = self.after(ANNOUNCE_EVERY_MS,
                                    self._check_announcements)

    def _on_announcements(self, items):
        """Called from a background thread, only when something is new."""
        self.after(0, lambda: self._show_announcements(items))

    def _show_announcements(self, items):
        if not items or not self.settings.get("announcements", True):
            return
        self._notes = list(items)
        self._render_note()

    def _render_note(self):
        """Show the first unread announcement in the strip below the header."""
        item = self._notes[0]
        accent = {"urgent": RED, "important": AMBER}.get(item["level"], SLATE)
        label = {"urgent": "URGENT", "important": "IMPORTANT"}.get(
            item["level"], "ANNOUNCEMENT")
        if item["date"]:
            label += "     " + item["date"]

        self.note_kind.set(label)
        self.note_spine.configure(bg=accent)
        self.note_kind_lbl.configure(fg=accent)

        text = " ".join((item["title"] or item["body"]).split())
        self.note_msg.set(text if len(text) <= 96 else text[:96].rstrip() + "…")

        waiting = len(self._notes) - 1
        self.note_more.configure(text=f"+{waiting} more" if waiting else "")

        # `before` pins it above the main body no matter when it appears
        self.note_bar.pack(fill="x", side="top", before=self.body_frame)

    def _read_note(self):
        """Show the full text of the current announcement.

        The link is printed rather than opened, so the reader sees where
        it goes before deciding. This text comes from a file on the
        internet: it is displayed as a message and can do nothing else.
        """
        if not getattr(self, "_notes", None):
            return
        item = self._notes[0]
        body = item["body"] or item["title"]
        if item["date"]:
            body = item["date"] + "\n\n" + body
        if item["link"]:
            body += "\n\nLink: " + item["link"]
        body += ("\n\nKestrel will never ask for your wallet file, your "
                 "backup key or your password. Any message that does is not "
                 "from the project.")
        self._say(item["title"] or "Announcement", body,
                  kind={"urgent": "error", "important": "warn"}.get(
                      item["level"], "info"),
                  link=item["link"] or None)

    def _dismiss_note(self):
        """Hide this one for good, then show the next if there is one."""
        notes = getattr(self, "_notes", [])
        if notes:
            seen = list(self.settings.get("announcements_seen", []))
            seen.append(notes[0]["id"])
            self.settings["announcements_seen"] = seen[-200:]   # bounded
            save_settings(self.settings)
            self._notes = notes[1:]
        if getattr(self, "_notes", None):
            self._render_note()
        else:
            self.note_bar.pack_forget()

    def _save_announcements_pref(self):
        on = bool(self.announce_var.get())
        self.settings["announcements"] = on
        save_settings(self.settings)
        if on:
            self._check_announcements()
        else:
            self.note_bar.pack_forget()
            self.toast("Announcements turned off.", "info")

    def _reset_announcements(self):
        self.settings["announcements_seen"] = []
        save_settings(self.settings)
        self.toast("Dismissed announcements will appear again.", "info")
        self._check_announcements()

    def _repair_chain(self):
        """Rebuild the ledger from the network, so nobody ever has to go
        and delete their chain file by hand to get unstuck."""
        if self.mining.is_set():
            self._say(
                "Stop mining first",
                "Stop mining before rebuilding, so the repair isn't racing "
                "against new blocks.")
            return
        if not self._ask(
                "Rebuild from the network?",
                "This downloads the chain from the other nodes and switches "
                "to it if theirs is better than yours.\n\n"
                "Your wallet and your coins are not touched — only the "
                "shared ledger is refreshed.\n\nContinue?"):
            return
        self.toast("Rebuilding from the network…")

        def run():
            ok, msg = self.node.resync_from_network()
            self.q.put(("repaired", ok, msg))
        threading.Thread(target=run, daemon=True).start()

    def _tick_now(self):
        self._refresh_stats()
        self._refresh_peers()
        self._painted_h = -1
        self._paint_blocks()
        threading.Thread(target=self.node.sync_once, daemon=True).start()
        self.toast("Refreshing — checking peers and syncing…")

    def show_view(self, name):
        self._active_view = name
        self._views[name].tkraise()
        if name == "Network":               # refresh on demand when opened
            self._refresh_peers()
        elif name == "Explorer":
            self._paint_blocks()
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
            tk.Label(p, text=subtitle, bg=DUSK, fg=FAINT,
                     font=SANS_9).pack(anchor="w", pady=(1, 0))
        tk.Frame(p, bg=DUSK3, height=1).pack(fill="x", pady=(12, 16))
        return p

    def _chip(self, parent, title, sub=False):
        c = tk.Frame(parent, bg=DUSK2, highlightbackground=DUSK3,
                     highlightthickness=1)
        c.pack(side="left", fill="x", expand=True, padx=(0, 10))
        tk.Label(c, text=title, bg=DUSK2, fg=FAINT,
                 font=MICRO_B).pack(anchor="w", padx=12, pady=(9, 0))
        var = tk.StringVar(value="—")
        tk.Label(c, textvariable=var, bg=DUSK2, fg=BUFF,
                 font=MONO_15B).pack(anchor="w", padx=12,
                                     pady=(0, 2 if sub else 9))
        if not sub:
            return var
        svar = tk.StringVar(value="")
        tk.Label(c, textvariable=svar, bg=DUSK2, fg=FAINT,
                 font=TINY).pack(anchor="w", padx=12, pady=(0, 7))
        return var, svar

    def toast(self, text, kind="info"):
        self.toasts.show(text, kind)

    # ----------------------------------------------------------------- mine
    def _view_mine(self, f):
        p = self._page(f, "Mine", "Rewards are paid straight to your address")
        chips = tk.Frame(p, bg=DUSK); chips.pack(fill="x", pady=(0, 14))
        self.chip_bal = self._chip(chips, "YOUR BALANCE")
        self.chip_sess = self._chip(chips, "EARNED THIS SESSION")
        self.chip_found = self._chip(chips, "BLOCKS THIS SESSION")
        self.chip_life, self.chip_life_sub = self._chip(
            chips, "EARNED ALL-TIME", sub=True)
        self.chip_sess.set("0.00000000 KSL")
        self.chip_found.set("0")
        self._paint_lifetime()

        self.netline_var = tk.StringVar(value="")
        tk.Label(p, textvariable=self.netline_var, bg=DUSK, fg=FAINT,
                 font=TINY).pack(anchor="w", pady=(0, 12))

        rowa = tk.Frame(p, bg=DUSK); rowa.pack(fill="x", pady=(0, 2))
        tk.Label(rowa, text="PAY MY REWARDS TO — yours, or paste any address",
                 bg=DUSK, fg=RUFOUS_HI, font=TINY_B).pack(anchor="w")
        r = tk.Frame(rowa, bg=DUSK); r.pack(fill="x", pady=(4, 0))
        self.addr_e = self._entry(r)
        self.addr_e.pack(side="left", fill="x", expand=True, ipady=5)
        self.addr_e.bind("<KeyRelease>", lambda _e: self._addr_changed())
        self._btn(r, "Paste", self.paste_address,
                  tip="Paste an address from the clipboard —\n"
                      "it is used and remembered automatically"
                  ).pack(side="left", padx=(6, 0))
        self._btn(r, "Mine to my wallet", self.use_wallet_address,
                  tip="Switch back to this app's own wallet address"
                  ).pack(side="left", padx=(6, 0))
        self.addr_hint = tk.Label(p, text="", bg=DUSK, fg=FAINT,
                                  font=TINY, anchor="w")
        self.addr_hint.pack(fill="x", pady=(3, 10))

        mid = tk.Frame(p, bg=DUSK); mid.pack(fill="x")
        left = tk.Frame(mid, bg=DUSK); left.pack(side="left", anchor="n")
        self.mine_btn = tk.Button(left, text="▶  Start mining",
                                  command=self.toggle, bg=RUFOUS, fg=DUSK,
                                  activebackground=RUFOUS_HI,
                                  activeforeground=DUSK, relief="flat",
                                  font=BIGBTN, padx=24,
                                  pady=12, cursor="hand2", bd=0)
        self.mine_btn.pack(anchor="w")
        Tooltip(self.mine_btn, "Ctrl+M works too")

        trow = tk.Frame(left, bg=DUSK); trow.pack(anchor="w", pady=(12, 0))
        tk.Label(trow, text="CPU threads", bg=DUSK, fg=MUTED,
                 font=SANS_9).pack(side="left")
        cores = os.cpu_count() or 2
        self.threads_var = tk.IntVar(
            value=int(self.settings.get("threads", default_threads())))
        sp = tk.Spinbox(trow, from_=1, to=cores, width=4,
                        textvariable=self.threads_var, bg=SPOT, fg=BUFF,
                        insertbackground=BUFF, relief="flat", font=MONO,
                        buttonbackground=DUSK3, highlightthickness=1,
                        highlightbackground=DUSK3)
        sp.pack(side="left", padx=(8, 6))
        Tooltip(sp, "More threads = more hashes per second.\n"
                    "Leave one core free so the computer stays smooth.")
        tk.Label(trow, text=f"of {cores} cores", bg=DUSK, fg=FAINT,
                 font=TINY).pack(side="left")

        self.autostart_var = tk.BooleanVar(
            value=bool(self.settings.get("autostart")))
        cb = tk.Checkbutton(left, text="Start mining when the app opens",
                            variable=self.autostart_var, bg=DUSK, fg=MUTED,
                            activebackground=DUSK, activeforeground=BUFF,
                            selectcolor=SPOT, font=SANS_9, bd=0,
                            highlightthickness=0, cursor="hand2",
                            command=self._save_autostart)
        cb.pack(anchor="w", pady=(8, 0))

        self.found_var = tk.StringVar(
            value="No blocks yet this session — press Start")
        tk.Label(left, textvariable=self.found_var, bg=DUSK, fg=MUTED,
                 font=SANS, wraplength=340, justify="left"
                 ).pack(anchor="w", pady=(10, 0))

        right = tk.Frame(mid, bg=DUSK); right.pack(side="right", anchor="n")
        self.rate_var = tk.StringVar(value="0 H/s")
        tk.Label(right, textvariable=self.rate_var, bg=DUSK, fg=SLATE,
                 font=MONO_20B).pack(anchor="e")
        tk.Label(right, text="mining speed", bg=DUSK, fg=FAINT,
                 font=TINY).pack(anchor="e")
        self.spark = tk.Canvas(right, width=320, height=64, bg=SPOT,
                               highlightthickness=1, highlightbackground=DUSK3)
        self.spark.pack(anchor="e", pady=(8, 0))
        self.eta_var = tk.StringVar(value="")
        tk.Label(right, textvariable=self.eta_var, bg=DUSK, fg=FAINT,
                 font=TINY).pack(anchor="e", pady=(4, 0))
        self.sess_var = tk.StringVar(value="")
        tk.Label(right, textvariable=self.sess_var, bg=DUSK, fg=FAINT,
                 font=TINY).pack(anchor="e")

        tk.Frame(p, bg=DUSK3, height=1).pack(fill="x", pady=(14, 10))
        tk.Label(p, text="BLOCKS FOUND THIS SESSION", bg=DUSK, fg=RUFOUS_HI,
                 font=TINY_B).pack(anchor="w", pady=(0, 6))
        wrap, self.found_tv = self._tree(
            p, (("time", 130, "w"), ("height", 90, "w"),
                ("nonce", 130, "w"), ("reward", 150, "e")),
            height=6, stretch="reward")
        wrap.pack(fill="both", expand=True)
        self._hint_if_empty(self.found_tv,
                            "Nothing yet — press Start mining")

    def _paint_lifetime(self):
        lf = int(self.settings.get("lifetime_feathers", 0))
        lb = int(self.settings.get("lifetime_blocks", 0))
        self.chip_life.set(format_ksl(lf))
        self.chip_life_sub.set(f"{lb:,} block(s) mined with this app")

    def _save_autostart(self):
        self.settings["autostart"] = bool(self.autostart_var.get())
        save_settings(self.settings)

    def _autostart(self):
        if not self.mining.is_set() and \
                is_valid_address(self.addr_e.get().strip()):
            self.toggle()
            self.toast("Mining auto-started — turn this off on the "
                       "Mine page if you prefer.", "info")

    @staticmethod
    def _fmt_eta(sec):
        if sec < 90:
            return f"{sec:,.0f} seconds"
        if sec < 5400:
            return f"{sec / 60:,.0f} minutes"
        if sec < 129600:
            return f"{sec / 3600:,.1f} hours"
        return f"{sec / 86400:,.1f} days"

    # ------------------------------------------------------------- explorer
    def _view_explorer(self, f):
        p = self._page(f, "Explorer",
                       "The public ledger, live from your own full node")
        row = tk.Frame(p, bg=DUSK); row.pack(fill="x")
        self.exq_e = self._entry(row)
        self._placeholder(self.exq_e, "block height, address, block id "
                                      "or transaction id…")
        self.exq_e.pack(side="left", fill="x", expand=True, ipady=5)
        self.exq_e.bind("<Return>", lambda _e: self.explorer_search())
        self._btn(row, "Search", self.explorer_search
                  ).pack(side="left", padx=(6, 0))

        qrow = tk.Frame(p, bg=DUSK); qrow.pack(fill="x", pady=(6, 8))
        tk.Label(qrow, text="Quick views:", bg=DUSK, fg=FAINT,
                 font=TINY).pack(side="left")
        self._linkbtn(qrow, "Top holders", self._ex_richlist,
                      tip="The addresses holding the most KSL"
                      ).pack(side="left", padx=(6, 0))
        self.pend_btn = self._linkbtn(qrow, "Pending (0)", self._ex_mempool,
                                      tip="Transactions waiting for the "
                                          "next block")
        self.pend_btn.pack(side="left", padx=(6, 0))
        self._linkbtn(qrow, "Newest block",
                      lambda: self._ex_search_value(str(self.chain.height)),
                      tip="Inspect the newest block on the chain"
                      ).pack(side="left", padx=(6, 0))

        of = tk.Frame(p, bg=SPOT, highlightbackground=DUSK3,
                      highlightthickness=1)
        of.pack(fill="x")
        self.ex_out = tk.Text(of, bg=SPOT, fg=MUTED, height=11,
                              font=MONO_9, relief="flat", wrap="none",
                              state="disabled", padx=10, pady=8,
                              cursor="arrow")
        self.ex_out.pack(fill="x")
        self.ex_out.tag_configure("hi", foreground=BUFF)
        self.ex_out.tag_configure("good", foreground=GREEN)
        self.ex_out.tag_configure("bad", foreground=RED)
        self.ex_out.tag_configure("amber", foreground=AMBER)
        self._ex_links = 0
        self._ex_print([("Search the ledger, or click a quick view above. "
                         "Every underlined id is clickable.", None, None)])

        ehead = tk.Frame(p, bg=DUSK); ehead.pack(fill="x", pady=(14, 6))
        tk.Label(ehead, text="LATEST BLOCKS  —  double-click one to inspect",
                 bg=DUSK, fg=RUFOUS_HI, font=TINY_B).pack(side="left")
        self._btn(ehead, "⟳ Refresh", self._refresh_blocks,
                  tip="Repaint the table with the newest blocks"
                  ).pack(side="right")
        wrap, self.blocks_tv = self._tree(
            p, (("height", 80, "w"), ("age", 90, "w"), ("time", 130, "w"),
                ("txs", 50, "e"), ("size", 80, "e"), ("difficulty", 90, "e"),
                ("reward", 120, "e"), ("miner", 220, "w")),
            height=8, stretch="miner")
        wrap.pack(fill="both", expand=True)
        self.blocks_tv.bind("<Double-1>", self._explorer_open_row)
        self._painted_h = -1

    def _refresh_blocks(self):
        self._painted_h = -1
        self._paint_blocks()

    def _explorer_open_row(self, _e):
        sel = self.blocks_tv.selection()
        if sel:
            v = self.blocks_tv.item(sel[0], "values")
            if v and str(v[0]).replace(",", "").isdigit():
                self._ex_search_value(str(v[0]).replace(",", ""))

    def _ex_search_value(self, value):
        if getattr(self.exq_e, "_ph_on", False):
            self.exq_e._ph_on = False
            self.exq_e.configure(fg=BUFF)
        self.exq_e.delete(0, "end")
        self.exq_e.insert(0, value)
        self.explorer_search()

    def _ex_print(self, lines):
        """lines: list of (text, tag, link_target). Links are clickable."""
        t = self.ex_out
        t.configure(state="normal")
        t.delete("1.0", "end")
        for text, tag, link in lines:
            if link is not None:
                self._ex_links += 1
                lt = f"lk{self._ex_links}"
                t.insert("end", text, (lt, "link"))
                t.tag_configure(lt, foreground=SLATE, underline=True)
                t.tag_bind(lt, "<Button-1>",
                           lambda _e, v=link: self._ex_search_value(v))
                t.tag_bind(lt, "<Enter>",
                           lambda _e, w=t: w.configure(cursor="hand2"))
                t.tag_bind(lt, "<Leave>",
                           lambda _e, w=t: w.configure(cursor="arrow"))
            else:
                t.insert("end", text, tag or ())
        t.configure(state="disabled")

    def explorer_search(self):
        q = self._entry_value(self.exq_e)
        if not q:
            return
        lines = []
        with self.lock:
            try:
                self.node._reindex()
            except Exception:
                pass
            c = self.chain
            block = tx_hit = None
            if q.replace(",", "").isdigit() and \
                    int(q.replace(",", "")) <= c.height:
                block = c.blocks[int(q.replace(",", ""))]
            elif len(q) == 64:
                ql = q.lower()
                for b in c.blocks:
                    if b.block_id == ql:
                        block = b
                        break
                if block is None:
                    hit = None
                    try:
                        hit = self.node._tx_index.get(ql)
                    except Exception:
                        pass
                    if hit:
                        h, t = hit
                        tx_hit = (c.blocks[h] if isinstance(h, int) else None,
                                  t)
                    else:
                        for b in c.blocks:
                            for t in b.transactions:
                                if t.txid == ql:
                                    tx_hit = (b, t)
                                    break
                            if tx_hit:
                                break
                    if tx_hit is None and ql in c.mempool:
                        tx_hit = (None, c.mempool[ql])
            if block is not None:
                lines = self._render_block(block)
            elif tx_hit is not None:
                lines = self._render_tx(*tx_hit)
            elif is_valid_address(q):
                lines = self._render_address(q)
            else:
                lines = [("Nothing on the ledger matches that — "
                          "check for typos.", None, None)]
        self._ex_print(lines)

    def _render_block(self, b):
        c = self.chain
        cbase = b.transactions[0]
        paid_to = cbase.outputs[0].address if cbase.outputs else "—"
        when = time.strftime("%d %b %Y, %H:%M:%S",
                             time.localtime(b.timestamp))
        lines = [
            (f"Block {b.height:,}", "hi", None),
            (f"   ·   {ago(b.timestamp)}\n", None, None),
            ("  id          ", None, None), (b.block_id + "\n", "hi", None),
        ]
        if b.height > 0:
            lines += [("  previous    ", None, None),
                      (b.prev_hash, None, b.prev_hash), ("\n", None, None)]
        if b.height < c.height:
            lines += [("  next        ", None, None),
                      (f"block {b.height + 1:,}", None, str(b.height + 1)),
                      ("\n", None, None)]
        lines += [
            (f"  time        {when}\n", None, None),
            (f"  difficulty  {c.difficulty_of(b.target):,.2f}   ·   "
             f"nonce {b.nonce:,}   ·   size {b.size():,} B\n", None, None),
            (f"  reward      {format_ksl(cbase.total_output)}  →  ",
             "good", None),
            (paid_to, None, paid_to), ("\n", None, None),
            (f"  transactions ({len(b.transactions)}):\n", None, None),
        ]
        for t in b.transactions[:12]:
            kind = "new coins" if t.is_coinbase else \
                f"{len(t.inputs)} in → {len(t.outputs)} out"
            lines += [("    ", None, None),
                      (mid_ellipsis(t.txid, 16), None, t.txid),
                      (f"   {format_ksl(t.total_output)}   ({kind})\n",
                       None, None)]
        if len(b.transactions) > 12:
            lines.append((f"    … and {len(b.transactions) - 12} more\n",
                          None, None))
        return lines

    def _render_tx(self, b, t):
        c = self.chain
        lines = [("Transaction  ", "hi", None), (t.txid + "\n", "hi", None)]
        if b is not None:
            confs = c.height - b.height + 1
            lines += [("  status      ", None, None),
                      (f"confirmed · {confs:,} confirmation(s) · in ",
                       "good", None),
                      (f"block {b.height:,}", None, str(b.height)),
                      ("\n", None, None)]
        else:
            lines += [("  status      waiting for the next block "
                       "(in the mempool)\n", "amber", None)]
        when = time.strftime("%d %b %Y, %H:%M:%S", time.localtime(t.timestamp))
        lines.append((f"  time        {when}   ·   size {t.size():,} B\n",
                      None, None))
        view = None
        try:
            view = self.node._tx_view(t, b.height if b else None)
        except Exception:
            pass
        if view and "fee" in view:
            lines.append((f"  fee         {view['fee_ksl']}\n", None, None))
        lines.append(("  from:\n", None, None))
        if t.is_coinbase:
            lines.append(("    new coins (block reward)\n", "good", None))
        elif view:
            for i in view["inputs"]:
                if i.get("address"):
                    lines += [("    ", None, None),
                              (i["address"], None, i["address"]),
                              (f"   {i.get('amount_ksl', '')}\n", None, None)]
                else:
                    lines.append(("    (unknown input)\n", None, None))
        lines.append(("  to:\n", None, None))
        for o in t.outputs[:8]:
            lines += [("    ", None, None),
                      (o.address, None, o.address),
                      (f"   {format_ksl(o.amount)}\n", "good", None)]
        if len(t.outputs) > 8:
            lines.append((f"    … and {len(t.outputs) - 8} more\n",
                          None, None))
        return lines

    def _render_address(self, q):
        c = self.chain
        view = None
        try:
            view = self.node._address_view(q)
        except Exception:
            pass
        if not view or not view.get("valid"):
            bal = c.balance(q)
            return [
                ("Address  ", "hi", None), (q + "\n", "hi", None),
                (f"  balance     {format_ksl(bal['confirmed'])}   "
                 f"(spendable {format_ksl(bal['spendable'])})\n",
                 "good", None),
            ]
        lines = [
            ("Address  ", "hi", None), (q + "\n", "hi", None),
            (f"  balance     {view['confirmed_ksl']}   "
             f"(spendable {view['spendable_ksl']})\n", "good", None),
            (f"  received    {view['received_ksl']}   ·   "
             f"sent {view['sent_ksl']}   ·   "
             f"{view['tx_count']:,} transaction(s)\n", None, None),
        ]
        hist = view.get("history", [])
        if hist:
            lines.append(("  recent:\n", None, None))
            for h in hist[:8]:
                when = time.strftime("%d %b, %H:%M",
                                     time.localtime(h["timestamp"]))
                sign = "+" if h["delta"] >= 0 else ""
                lines += [
                    (f"    {when}   {sign}{h['delta_ksl']:<18}  ",
                     "good" if h["delta"] >= 0 else "bad", None),
                    (mid_ellipsis(h["txid"], 12), None, h["txid"]),
                    ("\n", None, None)]
        return lines

    def _ex_richlist(self):
        with self.lock:
            try:
                ranked = self.node._richlist(15)
            except Exception:
                totals = {}
                for u in self.chain.utxos.values():
                    totals[u.address] = totals.get(u.address, 0) + u.amount
                circ = self.chain.circulating_supply() or 1
                ranked = [{"address": a, "amount_ksl": format_ksl(v),
                           "pct": round(v / circ * 100, 2)}
                          for a, v in sorted(totals.items(),
                                             key=lambda kv: -kv[1])[:15]]
        lines = [("Top holders — by balance right now\n", "hi", None)]
        if not ranked:
            lines.append(("  Nothing mined yet.\n", None, None))
        for i, r in enumerate(ranked, 1):
            lines += [(f"  {i:>2}. ", None, None),
                      (r["address"], None, r["address"]),
                      (f"   {r['amount_ksl']:>22}   {r['pct']:.2f}%\n",
                       "good", None)]
        self._ex_print(lines)

    def _ex_mempool(self):
        with self.lock:
            try:
                self.node._reindex()
            except Exception:
                pass
            txs = list(self.chain.mempool.values())
            views = []
            for t in txs[:15]:
                v = None
                try:
                    v = self.node._tx_view(t)
                except Exception:
                    pass
                views.append((t, v))
        if not txs:
            self._ex_print([("No pending transactions — everything is "
                             "confirmed.", None, None)])
            return
        lines = [(f"Pending transactions ({len(txs)}) — waiting for the "
                  "next block\n", "hi", None)]
        for t, v in views:
            fee = f"   fee {v['fee_ksl']}" if v and "fee" in v else ""
            lines += [("  ", None, None),
                      (mid_ellipsis(t.txid, 16), None, t.txid),
                      (f"   {format_ksl(t.total_output)}{fee}\n",
                       "amber", None)]
        self._ex_print(lines)

    def _paint_blocks(self):
        if not hasattr(self, "blocks_tv"):
            return
        my_addr = self.addr_e.get().strip() if hasattr(self, "addr_e") else ""
        with self.lock:
            h = self.chain.height
            if h == self._painted_h:
                return
            rows = []
            for b in self.chain.blocks[-100:][::-1]:
                cb = b.transactions[0]
                miner = cb.outputs[0].address if cb.outputs else "—"
                rows.append((f"{b.height:,}",
                             ago(b.timestamp),
                             time.strftime("%d %b, %H:%M:%S",
                                           time.localtime(b.timestamp)),
                             len(b.transactions),
                             f"{b.size():,} B",
                             f"{self.chain.difficulty_of(b.target):,.2f}",
                             format_ksl(cb.total_output),
                             mid_ellipsis(miner, 11),
                             miner == my_addr))
        self._painted_h = h
        self.blocks_tv.delete(*self.blocks_tv.get_children())
        for r in rows:
            tags = ("mine",) if r[-1] else ()
            self.blocks_tv.insert("", "end", values=r[:-1], tags=tags)
        self._zebra(self.blocks_tv)

    # -------------------------------------------------------------- network
    def _view_network(self, f):
        p = self._page(f, "Network",
                       "Everything connects automatically — seeds, saved "
                       "peers, your own network and the worldwide DHT")
        chips = tk.Frame(p, bg=DUSK); chips.pack(fill="x", pady=(0, 14))
        self.chip_online = self._chip(chips, "NODES ONLINE")
        self.chip_known = self._chip(chips, "KNOWN NODES")
        self.chip_mem = self._chip(chips, "PENDING TXS")
        self.chip_height = self._chip(chips, "BLOCK HEIGHT")

        # Getting unstuck should be a button, not a folder full of files the
        # user has to find and delete with the app closed.
        fix = tk.Frame(p, bg=DUSK2); fix.pack(fill="x", pady=(0, 12))
        fc = tk.Frame(fix, bg=DUSK2); fc.pack(fill="x", padx=18, pady=12)
        tk.Label(fc, text="OUT OF STEP WITH EVERYONE ELSE?", bg=DUSK2,
                 fg=MUTED, font=TINY_B).pack(anchor="w")
        tk.Label(fc, text="If your block height is stuck or looks nothing "
                          "like the rest of the network, rebuild the ledger "
                          "from the other nodes. Your wallet and coins are "
                          "not touched.",
                 bg=DUSK2, fg=FAINT, font=SANS_9, wraplength=620,
                 justify="left").pack(anchor="w", pady=(4, 9))
        self._btn(fc, "Rebuild from the network", self._repair_chain,
                  tip="Download the chain from other nodes and switch to "
                      "it if theirs is better").pack(anchor="w")

        share = tk.Frame(p, bg=DUSK2, highlightbackground=RUFOUS,
                         highlightthickness=1)
        share.pack(fill="x", pady=(0, 12))
        sc = tk.Frame(share, bg=DUSK2)
        sc.pack(fill="x", padx=18, pady=12)
        tk.Label(sc, text="SHARE YOUR NODE — send this to friends and the "
                 "network grows by itself", bg=DUSK2, fg=RUFOUS_HI,
                 font=TINY_B).pack(anchor="w")
        srow = tk.Frame(sc, bg=DUSK2)
        srow.pack(fill="x", pady=(6, 0))
        self.share_url = ""
        self.share_big_var = tk.StringVar(value="starting…")
        sl = tk.Label(srow, textvariable=self.share_big_var, bg=SPOT,
                      fg=BUFF, font=MONO_13, anchor="w", padx=12, pady=9,
                      cursor="hand2")
        sl.pack(side="left", fill="x", expand=True)
        sl.bind("<Button-1>", lambda _e: self._copy_share())
        Tooltip(sl, "Click to copy")
        self._btn(srow, "Copy", self._copy_share, primary=True,
                  tip="Copy your node address"
                  ).pack(side="left", padx=(8, 0))
        self._btn(srow, "Copy invite", self._copy_invite,
                  tip="Copy a ready-to-send message with your address\n"
                      "and the three steps to join — paste it into any chat"
                  ).pack(side="left", padx=(6, 0))
        self.share_sub_var = tk.StringVar(value="")
        tk.Label(sc, textvariable=self.share_sub_var, bg=DUSK2, fg=FAINT,
                 font=TINY, wraplength=660, justify="left"
                 ).pack(anchor="w", pady=(5, 0))

        card = tk.Frame(p, bg=DUSK2, highlightbackground=DUSK3,
                        highlightthickness=1)
        card.pack(fill="x")
        c = tk.Frame(card, bg=DUSK2); c.pack(fill="x", padx=18, pady=14)
        self.net_big = tk.StringVar(value="Node starting…")
        tk.Label(c, text="YOUR NODE", bg=DUSK2, fg=RUFOUS_HI,
                 font=TINY_B).pack(anchor="w")
        tk.Label(c, textvariable=self.net_big, bg=DUSK2, fg=BUFF,
                 font=MONO_13).pack(anchor="w", pady=(4, 2))
        arow = tk.Frame(c, bg=DUSK2); arow.pack(fill="x")
        self.api_var = tk.StringVar(value="")
        tk.Label(arow, textvariable=self.api_var, bg=DUSK2, fg=FAINT,
                 font=MONO_9).pack(side="left")
        self._linkbtn(arow, "Copy",
                      lambda: self._copy_text(
                          f"http://127.0.0.1:{self.node.port}/",
                          "API address copied."),
                      tip="Copy the local JSON API address"
                      ).pack(side="left", padx=(8, 0))
        self._linkbtn(arow, "Open dashboard",
                      lambda: webbrowser.open(
                          f"http://127.0.0.1:{self.node.port}/"),
                      tip="Open your node's live web dashboard in a browser"
                      ).pack(side="left")
        self.reach_var = tk.StringVar(value="")
        self.reach_lbl = tk.Label(c, textvariable=self.reach_var, bg=DUSK2,
                                  fg=MUTED, font=SANS_9,
                                  wraplength=640, justify="left")
        self.reach_lbl.pack(anchor="w", pady=(8, 0))
        self.fix_row = tk.Frame(c, bg=DUSK2)
        self.fix_btn = self._btn(self.fix_row, "Fix my connection",
                                 self._fix_connection, primary=True,
                                 tip="Let friends connect IN to you —\n"
                                     "opens this app's port with your "
                                     "permission")
        self.fix_btn.pack(side="left")
        self._fixing = False

        nhead = tk.Frame(p, bg=DUSK); nhead.pack(fill="x", pady=(16, 6))
        tk.Label(nhead, text="CONNECTED NODES", bg=DUSK, fg=RUFOUS_HI,
                 font=TINY_B).pack(side="left")
        self._btn(nhead, "⟳ Refresh", self.refresh_network,
                  tip="Re-check every node and sync right now"
                  ).pack(side="right")
        wrap, self.peers_tv = self._tree(
            p, (("peer", 280, "w"), ("status", 90, "w"),
                ("height", 90, "e"), ("seen", 130, "e")),
            height=6, stretch="peer")
        wrap.pack(fill="both", expand=True)
        pm = tk.Menu(self.peers_tv, tearoff=0, bg=DUSK2, fg=BUFF,
                     activebackground=DUSK3, activeforeground=BUFF, bd=0,
                     font=SANS)
        pm.add_command(label="Copy node address", command=self._copy_peer)
        pm.add_command(label="Sync with this node now",
                       command=self._sync_selected_peer)

        def peer_pop(e):
            iid = self.peers_tv.identify_row(e.y)
            if iid:
                self.peers_tv.selection_set(iid)
                try:
                    pm.tk_popup(e.x_root, e.y_root)
                finally:
                    pm.grab_release()
        self.peers_tv.bind("<Button-3>", peer_pop)

        tk.Label(p, text="ADD A NODE BY ADDRESS  —  optional",
                 bg=DUSK, fg=RUFOUS_HI,
                 font=TINY_B).pack(anchor="w", pady=(14, 4))
        row = tk.Frame(p, bg=DUSK); row.pack(fill="x")
        self.node_e = self._entry(row)
        self._placeholder(self.node_e,
                          f"http://their-ip:{params.DEFAULT_PORT}")
        self.node_e.pack(side="left", fill="x", expand=True, ipady=5)
        self.node_e.bind("<Return>", lambda _e: self.sync_now())
        self._btn(row, "Connect", self.sync_now).pack(side="left", padx=(6, 0))
        tk.Label(p, text="Nodes on the same network find each other by "
                 "themselves, and the worldwide DHT finds nodes anywhere on "
                 "Earth. To reach a friend directly, paste their address "
                 "once — after that the mesh remembers itself. Everything "
                 "runs locally; no accounts, no servers.",
                 bg=DUSK, fg=MUTED, font=SANS_9,
                 wraplength=680, justify="left").pack(anchor="w", pady=(10, 0))

    def _copy_text(self, text, note="Copied."):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.toast(note, "good")

    def _copy_peer(self):
        sel = self.peers_tv.selection()
        if sel:
            vals = self.peers_tv.item(sel[0], "values")
            if vals and vals[0] and not str(vals[0]).startswith("No nodes"):
                self._copy_text(vals[0], "Node address copied.")

    def refresh_network(self):
        self.log("Checking every known node…", cat="network")
        self._refresh_peers()

        def work():
            self.node.sync_once()
            self.q.put(("stats",))
        threading.Thread(target=work, daemon=True).start()

    def _copy_share(self):
        if not self.share_url:
            return self.toast("The node is still starting — one moment.",
                              "warn")
        self._copy_text(self.share_url,
                        "Your node address is on the clipboard — "
                        "send it to anyone.")

    def _copy_invite(self):
        if not self.share_url:
            return self.toast("The node is still starting — one moment.",
                              "warn")
        self._copy_text(invite_text(self.share_url),
                        "Invite copied — paste it into any chat.")

    def _sync_selected_peer(self):
        sel = self.peers_tv.selection()
        if not sel:
            return
        vals = self.peers_tv.item(sel[0], "values")
        url = str(vals[0]) if vals else ""
        if not url.startswith("http"):
            return
        self.toast(f"Syncing with {url}…")
        threading.Thread(target=self._sync_worker, args=(url,),
                         daemon=True).start()

    # ------------------------------------------------- connection repair
    def _fix_connection(self):
        port = self.node.port
        win = (sys.platform == "win32")
        top = self._dialog("Fix my connection")
        wrap = tk.Frame(top, bg=DUSK2)
        wrap.pack(padx=20, pady=16, fill="both")
        tk.Label(wrap, text="Let friends connect IN to you", bg=DUSK2,
                 fg=BUFF, font=SANS_B).pack(anchor="w")
        if win:
            steps = (
                "With your permission this will:\n\n"
                f"1.  Add ONE Windows Firewall rule that allows incoming\n"
                f"     connections on TCP port {port} (this app's node).\n"
                "2.  Ask your router to forward that port (UPnP).\n"
                "3.  Re-check whether people can now reach you.\n\n"
                "Windows will pop up asking for permission — that is the\n"
                "normal Administrator prompt; click Yes. This does NOT turn\n"
                "off your firewall or open anything else, and you can undo\n"
                "it any time from Help ▸ Remove firewall rule.")
        else:
            steps = (
                "This will ask your router to forward this app's port "
                f"(TCP {port}) using UPnP, then re-check whether people can "
                "reach you.\n\nOn this system the app can't change the "
                "firewall for you. If a friend still can't connect, allow "
                "incoming connections for this app in your system's "
                "firewall settings, or forward the port on your router.")
        tk.Label(wrap, text=steps, bg=DUSK2, fg=MUTED, font=SANS_9,
                 justify="left").pack(anchor="w", pady=(8, 6))
        if win:
            tk.Label(wrap, text="Exact command Windows will run:", bg=DUSK2,
                     fg=FAINT, font=TINY_B).pack(anchor="w", pady=(4, 2))
            box = tk.Text(wrap, bg=SPOT, fg=SLATE, font=MONO_8, height=3,
                          relief="flat", wrap="word", padx=8, pady=6)
            box.insert("1.0", fw_add_command(port))
            box.configure(state="disabled")
            box.pack(fill="x")
        row = tk.Frame(top, bg=DUSK2)
        row.pack(pady=(4, 16))
        self._btn(row, "Fix it", lambda: (top.destroy(),
                                          self._run_fix()), primary=True,
                  tip="You'll approve a Windows permission prompt next"
                  if win else None).pack(side="left", padx=4)
        self._btn(row, "Cancel", top.destroy).pack(side="left", padx=4)
        self._present_dialog(top)

    def _run_fix(self):
        if self._fixing:
            return
        self._fixing = True
        self.fix_btn.configure(state="disabled", text="Fixing…")
        self.fix_row.pack_forget()
        self.toast("Opening your node's port — approve the Windows prompt "
                   "if it appears…")
        threading.Thread(target=self._fix_worker, daemon=True).start()

    def _fix_worker(self):
        port = self.node.port
        steps = []
        if sys.platform == "win32":
            ok, msg = fw_apply(True, port)
            steps.append(("good" if ok else "bad", msg))
            self.q.put(("log", "Firewall: " + msg,
                        "good" if ok else "bad", "network"))
        # retry the router port-forward
        try:
            self.node._setup_reachability()
            if self.node.upnp_mapped:
                steps.append(("good", "Router forwarded the port (UPnP)."))
            else:
                steps.append(("info", "Router did not auto-forward (UPnP "
                                      "off or unsupported)."))
        except Exception:
            steps.append(("info", "Could not talk to the router."))
        # re-check reachability from a peer
        try:
            self.node.reachable = None
            self.node.check_reachability()
        except Exception:
            pass
        self.q.put(("fixdone", steps))

    def _finish_fix(self, steps):
        self._fixing = False
        self.fix_btn.configure(state="normal", text="Fix my connection")
        reach = self.node.reachable
        if reach is True:
            self.toast("✓ Fixed — people can now connect to you. Share your "
                       "node address!", "good")
        elif any(t == "good" for t, _m in steps):
            self.toast("Applied. Reachability can take a minute to confirm — "
                       "watch the status above. If it stays blocked, your "
                       "router likely needs a manual port-forward.", "warn")
        else:
            worst = next((m for t, m in steps if t == "bad"), None)
            self.toast(worst or "Nothing was changed.", "bad")
        self._refresh_stats()

    def _remove_firewall_rule(self):
        if sys.platform != "win32":
            return self.toast("Automatic firewall changes are Windows-only.",
                              "warn")
        if not fw_rule_present(self.node.port):
            return self.toast("There's no Kestrel firewall rule to remove.",
                              "info")
        if not self._ask(
                "Remove firewall rule?",
                "Remove the Windows Firewall rule that lets friends connect "
                f"to this app on TCP port {self.node.port}?\n\n"
                "You'll approve a Windows permission prompt. Your node keeps "
                "working; people just won't be able to connect IN anymore."):
            return

        def work():
            ok, msg = fw_apply(False, self.node.port)
            self.q.put(("toast", msg, "good" if ok else "bad"))
            self.q.put(("stats",))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- activity
    def _view_activity(self, f):
        p = self._page(f, "Activity",
                       "A plain-language log of everything happening")
        ahead = tk.Frame(p, bg=DUSK); ahead.pack(fill="x", pady=(0, 6))
        tk.Label(ahead, text="LIVE LOG", bg=DUSK, fg=RUFOUS_HI,
                 font=TINY_B).pack(side="left")
        self._filter_btns = {}
        fr = tk.Frame(ahead, bg=DUSK); fr.pack(side="left", padx=(14, 0))
        for name in ("All", "Blocks", "Network", "Problems"):
            b = tk.Button(fr, text=name, bd=0, relief="flat", bg=DUSK,
                          fg=MUTED, activebackground=DUSK,
                          activeforeground=BUFF, font=TINY_B, padx=8,
                          pady=2, cursor="hand2",
                          command=lambda n=name: self._set_log_filter(n))
            b.pack(side="left")
            self._filter_btns[name] = b
        self._btn(ahead, "Clear", self._clear_log,
                  tip="Empty the log (nothing else is affected)"
                  ).pack(side="right")
        self._linkbtn(ahead, "Copy all",
                      lambda: self._copy_text(
                          "\n".join(f"{ts}  {m}" for ts, m, _t, _c
                                    in self._log_recs),
                          "Log copied to the clipboard."),
                      tip="Copy the whole log as text"
                      ).pack(side="right", padx=(0, 8))
        lf = tk.Frame(p, bg=SPOT, highlightbackground=DUSK3,
                      highlightthickness=1)
        lf.pack(fill="both", expand=True)
        self.log_t = tk.Text(lf, bg=SPOT, fg=MUTED, font=MONO_9,
                             relief="flat", wrap="word", state="disabled",
                             padx=8, pady=6)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.log_t.yview,
                           style="KV.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.log_t.configure(yscrollcommand=sb.set)
        self.log_t.pack(fill="both", expand=True)
        self.log_t.tag_configure("good", foreground=GREEN)
        self.log_t.tag_configure("bad", foreground=RED)
        self.log_t.tag_configure("hi", foreground=BUFF)
        self._set_log_filter("All")

    def _set_log_filter(self, name):
        self._log_filter = name
        for nm, b in self._filter_btns.items():
            b.configure(fg=BUFF if nm == name else MUTED,
                        bg=DUSK3 if nm == name else DUSK)
        self._repaint_log()

    @staticmethod
    def _cat_of(msg, tag):
        if tag == "bad":
            return "Problems"
        low = msg.lower()
        if "block" in low or "★" in msg or "mining" in low:
            return "Blocks"
        return "Network"

    def _match_filter(self, cat):
        return self._log_filter == "All" or cat == self._log_filter

    def _repaint_log(self):
        t = self.log_t
        t.configure(state="normal")
        t.delete("1.0", "end")
        for ts, msg, tag, cat in self._log_recs:
            if self._match_filter(cat):
                t.insert("end", f"{ts}  {msg}\n", tag or ())
        t.see("end")
        t.configure(state="disabled")

    def _append_log(self, ts, msg, tag, cat):
        self._log_recs.append((ts, msg, tag, cat))
        if not self._match_filter(cat):
            return
        t = self.log_t
        at_bottom = t.yview()[1] > 0.95
        t.configure(state="normal")
        if int(t.index("end-1c").split(".")[0]) > 1600:
            t.delete("1.0", "200.0")
        t.insert("end", f"{ts}  {msg}\n", tag or ())
        if at_bottom:
            t.see("end")
        t.configure(state="disabled")

    def _clear_log(self):
        self._log_recs.clear()
        self._repaint_log()

    # ------------------------------------------------------------- plumbing
    def log(self, msg, tag=None, cat=None):
        self.q.put(("log", msg, tag, cat))

    def _node_log(self, msg, level="info"):
        tag = {"good": "good", "bad": "bad", "info": None}.get(level)
        self.q.put(("log", msg, tag, None))
        self.q.put(("stats",))

    def _drain_queue(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "log":
                    msg, tag, cat = rest
                    self._append_log(time.strftime("%H:%M:%S"), msg, tag,
                                     cat or self._cat_of(msg, tag))
                elif kind == "rate":
                    self._on_rate(rest[0])
                elif kind == "found":
                    n, feathers = rest
                    s = "" if n == 1 else "s"
                    self.found_var.set(
                        f"You have mined {n} block{s} this session — "
                        f"{format_ksl(feathers)} earned")
                    self.chip_sess.set(format_ksl(feathers))
                    self.chip_found.set(f"{n:,}")
                elif kind == "foundrow":
                    height, nonce, reward = rest
                    for iid in self.found_tv.get_children():
                        if "hint" in self.found_tv.item(iid, "tags"):
                            self.found_tv.delete(iid)
                    self.found_tv.insert(
                        "", 0, tags=("pos",),
                        values=(time.strftime("%H:%M:%S"), f"{height:,}",
                                f"{nonce:,}", "+" + format_ksl(reward)))
                    self._zebra(self.found_tv)
                    self.toast(f"★ Block {height:,} mined — "
                               f"+{format_ksl(reward)} to your address",
                               "good")
                elif kind == "lifetime":
                    reward = rest[0]
                    self.settings["lifetime_feathers"] = \
                        int(self.settings.get("lifetime_feathers", 0)) + reward
                    self.settings["lifetime_blocks"] = \
                        int(self.settings.get("lifetime_blocks", 0)) + 1
                    save_settings(self.settings)
                    self._paint_lifetime()
                elif kind == "wsent":
                    self.w_send_btn.configure(state="normal")
                    self._w_say(f"✓ Sent {rest[0]} — the next block "
                                f"confirms it, about 2 minutes.", GREEN)
                    self.toast(f"✓ Sent {rest[0]}.", "good")
                    self.w_to.delete(0, "end"); self.w_amt.delete(0, "end")
                    self._refresh_wallet()
                elif kind == "wfail":
                    self.w_send_btn.configure(state="normal")
                    self._w_say(f"Could not send: {rest[0]}", RED)
                    self.toast("Could not send — see the Wallet tab.", "bad")
                elif kind == "repaired":
                    ok, msg = rest
                    self.toast(msg, "good" if ok else "info")
                    self.log(msg, "good" if ok else None)
                    self._tick_now()
                elif kind == "status":
                    self.status_var.set(rest[0])
                elif kind == "toast":
                    self.toast(*rest)
                elif kind == "connfail":
                    title, msg = rest
                    self.toast("Couldn't connect — see the details.", "bad")
                    self._warn(title, msg)
                elif kind == "fixdone":
                    self._finish_fix(rest[0])
                elif kind == "stats":
                    self._refresh_stats()
        except queue.Empty:
            pass
        self.after(150, self._drain_queue)

    def _on_rate(self, r):
        now = time.time()
        if r > 0 and self._last_rate_ts is not None:
            dt = min(max(now - self._last_rate_ts, 0.0), 3.0)
            self.session_hashes += r * dt
            self.session_secs += dt
        self._last_rate_ts = now if r > 0 else None
        self._cur_rate = r
        self.rate_var.set(fmt_rate(r))
        self.samples.append(r)
        self._draw_spark()
        work = self._block_work
        if r > 0 and work:
            self.eta_var.set("≈ one block every "
                             + self._fmt_eta(work / r) + " at this speed")
        elif not self.mining.is_set():
            self.eta_var.set("")
        if self.session_secs > 1:
            avg = self.session_hashes / self.session_secs
            self.sess_var.set(f"session {fmt_count(self.session_hashes)} "
                              f"hashes · avg {fmt_rate(avg)} · "
                              f"mining for {fmt_span(self.session_secs)}")
        if self.mining.is_set():
            self.mine_pill.set(f"⚒ mining · {fmt_rate(r)}")
        else:
            self.mine_pill.set("")

    def _tick(self):
        if self.state() == "iconic":        # minimized: poll lazily, cheaply
            self._refresh_stats()
            self.after(8000, self._tick)
            return
        self._refresh_stats()
        view = getattr(self, "_active_view", None)
        if view == "Network":               # repaint a table only while shown
            self._refresh_peers()
        elif view == "Explorer":
            self._paint_blocks()
        self.after(3000, self._tick)

    def _draw_spark(self):
        c = self.spark
        if getattr(self, "_active_view", "Mine") != "Mine" or self.state() == "iconic":
            return  # Mine view not on top (or minimized) — skip redraw, save CPU
        c.delete("all")
        w = int(c.cget("width")); h = int(c.cget("height"))
        if len(self.samples) < 2:
            c.create_text(w // 2, h // 2, fill=FAINT, font=MONO_8,
                          text="speed graph — warming up…")
            return
        top = max(self.samples) or 1
        for frac in (0.33, 0.66):
            y = h - 5 - frac * (h - 14)
            c.create_line(4, y, w - 4, y, fill=GRID)
        n = len(self.samples)
        pts = []
        for i, v in enumerate(self.samples):
            x = 4 + i * (w - 8) / max(n - 1, 1)
            y = h - 5 - (v / top) * (h - 14)
            pts.extend((x, y))
        c.create_polygon(*(pts + [pts[-2], h - 2, pts[0], h - 2]),
                         fill="#2A2028", outline="")
        avg = sum(self.samples) / n
        ya = h - 5 - (avg / top) * (h - 14)
        c.create_line(4, ya, w - 4, ya, fill=SLATE, dash=(2, 3))
        c.create_line(*pts, fill=RUFOUS_HI, width=2, smooth=True)
        c.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3,
                      fill=RUFOUS_HI, outline="")
        c.create_text(6, 8, anchor="w", fill=FAINT, font=MONO_7,
                      text=f"peak {fmt_rate(top)} · avg {fmt_rate(avg)}")

    # -------------------------------------------------------------- address
    def _addr_changed(self):
        addr = self.addr_e.get().strip()
        if not addr:
            self.addr_hint.configure(text="", fg=FAINT)
        elif is_valid_address(addr):
            saved = "" if addr != self.settings.get("payout_address") \
                else "  (remembered)"
            self.addr_hint.configure(text="✓ valid Kestrel address" + saved,
                                     fg=GREEN)
        else:
            self.addr_hint.configure(
                text="✗ not a Kestrel address yet — they start with K",
                fg=RED)

    def _set_address(self, addr, remember=True):
        self.addr_e.delete(0, "end")
        self.addr_e.insert(0, addr)
        if remember and is_valid_address(addr):
            self.settings["payout_address"] = addr
            save_settings(self.settings)
        self._addr_changed()

    def paste_address(self):
        try:
            pasted = self.clipboard_get().strip()
        except Exception:
            return self.toast("Clipboard is empty.", "bad")
        self._set_address(pasted, remember=is_valid_address(pasted))
        if is_valid_address(pasted):
            self.toast(f"Rewards will now go to {pasted[:14]}… "
                       "(remembered)", "good")
        else:
            self.toast("That doesn't look like a Kestrel address — "
                       "they start with K.", "bad")

    def use_wallet_address(self):
        if os.path.exists(WALLET_FILE):
            try:
                w = Wallet.load(WALLET_FILE)
                self._set_address(w.address)
                self.toast("Rewards will go to this app's own wallet "
                           "address.", "good")
                return
            except Exception:
                pass
        self.make_address()

    def _ensure_address(self):
        saved = self.settings.get("payout_address", "")
        if is_valid_address(saved):
            self._set_address(saved, remember=False)
            self.log("Welcome back — rewards go to your saved address.", "hi")
            return
        if os.path.exists(WALLET_FILE):
            try:
                w = Wallet.load(WALLET_FILE)
                self._set_address(w.address)
                self.log("Welcome back — rewards go to your saved address.",
                         "hi")
                return
            except Exception:
                pass
        w = Wallet.create()
        w.save(WALLET_FILE)
        self._set_address(w.address)
        self.log("Created your reward address automatically.", "good")
        self._say(
            "Your reward address is ready",
            "Address:\n" + w.address +
            "\n\nBackup key (keep secret):\n" + private_to_wif(w.private_key) +
            "\n\nWrite the backup key down — it is the only way to restore "
            "your coins, and anyone who has it can spend them. Import it into "
            "Kestrel Wallet to spend what you mine.\n\nPrefer mining to a "
            "different address? Just paste it in the address box.")

    def make_address(self):
        if os.path.exists(WALLET_FILE) and not self._ask(
                "Replace address?",
                "An address already exists here and will be replaced. "
                "If it holds KSL, use File ▸ Backup first.\n\nContinue?"):
            return
        w = Wallet.create()
        w.save(WALLET_FILE)
        self._set_address(w.address)
        self._say(
            "Back up your key",
            "Your new address:\n" + w.address +
            "\n\nYour backup key:\n" + private_to_wif(w.private_key) +
            "\n\nWrite the backup key down now — it is the only way to "
            "restore your coins.")
        self.log("New reward address created and saved.", "good")

    # ----------------------------------------------------- built-in node
    def start_node(self):
        if self.node_serving:
            return
        self.node_serving = True
        threading.Thread(target=self._node_thread, daemon=True).start()

    def _node_thread(self):
        try:
            self.log(f"Full node running on port {self.node.port} — "
                     "connecting to the Kestrel network…", "hi")
            self.log(f"Dashboard + JSON API at "
                     f"http://127.0.0.1:{self.node.port}/ "
                     f"(open in a browser)", "good")
            self.q.put(("stats",))
            self.node.serve_forever()
        except OSError as e:
            self.node_serving = False
            self.log(f"The node could not start: {e}", "bad")
            self.q.put(("toast", f"The node could not start: {e}", "bad"))

    # ---------------------------------------------------------------- sync
    def sync_now(self):
        raw = self._entry_value(self.node_e)
        if not raw:
            self.toast("Type another node's address like "
                       f"http://192.168.1.20:{params.DEFAULT_PORT} "
                       "and press Connect.", "warn")
            return
        # loose input welcome: "1.2.3.4", "1.2.3.4:4444" and full URLs
        # all work — parse_node_url fills in the scheme and default port
        url, _host, _port = parse_node_url(raw)
        if not url:
            self.toast("That doesn't look like a node address — use "
                       f"something like http://12.34.56.78:"
                       f"{params.DEFAULT_PORT}.", "warn")
            return
        self.node.add_peers([url])
        self.toast(f"Connecting to {url}…")
        threading.Thread(target=self._sync_worker, args=(url,),
                         daemon=True).start()

    def _sync_worker(self, url):
        try:
            self.node.announce_to(url)
            msg = self.node.sync_peer(url)
            self.log("Connected: " + msg, "hi")
            self.q.put(("toast", "Connected — " + msg, "good"))
            self.q.put(("stats",))
        except Exception as e:
            reason = diagnose_node(url) or f"Could not reach that node: {e}"
            self.log(reason, "bad")
            self.q.put(("connfail", "Couldn't connect to that node", reason))

    # --------------------------------------------------------------- mining
    def toggle(self):
        if self.mining.is_set():
            self.mining.clear()
            self.mine_btn.configure(text="▶  Start mining", bg=RUFOUS,
                                    fg=DUSK, activebackground=RUFOUS_HI,
                                    activeforeground=DUSK)
            return
        address = self.addr_e.get().strip()
        if not is_valid_address(address):
            self.show_view("Mine")
            return self._error(
                "Where should rewards go?",
                "Paste a Kestrel address (starts with K) or press "
                "“Mine to my wallet” — your rewards need "
                "somewhere to land.")
        if address != self.settings.get("payout_address"):
            self.settings["payout_address"] = address
            save_settings(self.settings)
            self._addr_changed()
        try:
            threads = int(self.threads_var.get() or 1)
        except (tk.TclError, ValueError):
            threads = default_threads()   # non-numeric text in the spinbox
            self.threads_var.set(threads)
        threads = max(1, min(threads, os.cpu_count() or 1))
        if threads != self.settings.get("threads"):
            self.settings["threads"] = threads
            save_settings(self.settings)
        self.mining.set()
        self.mine_btn.configure(text="■  Stop mining", bg=DUSK3, fg=BUFF,
                                activebackground=HOVER,
                                activeforeground=BUFF)
        threading.Thread(target=self._mine_loop, args=(address, threads),
                         daemon=True).start()

    def _mine_loop(self, address, threads):
        # Never start building blocks before we know whether anyone else is
        # out there. At the starting difficulty a few seconds of solo mining
        # can outweigh the real network, and once that happens this node
        # correctly refuses to switch — leaving it mining a private chain
        # forever with no way back except deleting the ledger by hand. So
        # wait for the answer first.
        if self.node.network_state() == "looking":
            self.q.put(("status", "Finding the network before mining…"))
            self.log("Checking for other nodes before mining, so this "
                     "computer can't start a chain of its own by mistake…")
            state = self.node.wait_until_known(timeout=45)
            if not self.mining.is_set():
                return
            if state == "joined":
                self.log("Connected. Mining on the shared chain.", "good")
            else:
                self.log("No other nodes answered — mining a brand-new "
                         "network on this machine. It will merge with the "
                         "others automatically when one appears.", "warn")

        self.log(f"Mining on {threads} CPU thread(s). Every block found "
                 f"pays the reward to {address[:14]}…", "hi")
        while self.mining.is_set():
            with self.lock:
                block = assemble_candidate(self.chain, address,
                                           message="kestrel-miner-app")
                tip_id = block.prev_hash

            round_stop = threading.Event()

            def watch():
                # stop this round if the user stops, or the chain moves on
                while not round_stop.is_set():
                    if not self.mining.is_set():
                        round_stop.set(); return
                    with self.lock:
                        moved = self.chain.tip.block_id != tip_id
                    if moved:
                        round_stop.set(); return
                    round_stop.wait(0.5)

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            ok = find_pow(block, threads=threads, stop=round_stop,
                          max_seconds=25,
                          on_progress=lambda r: self.q.put(("rate", r)))
            round_stop.set()

            if not self.mining.is_set():
                break
            if not ok:
                continue  # stale tip or round over — fresh candidate
            try:
                with self.lock:
                    self.chain.add_block(block)
                reward = block.transactions[0].total_output
                self.blocks_found += 1
                self.session_feathers += reward
                self.q.put(("found", self.blocks_found, self.session_feathers))
                self.q.put(("foundrow", block.height, block.nonce, reward))
                self.q.put(("lifetime", reward))
                self.log(f"★ Block {block.height:,} found!  "
                         f"+{format_ksl(reward)} to your address  "
                         f"(nonce {block.nonce:,})", "good")
                self.q.put(("stats",))
                self.node.gossip_block(block)
            except ValidationError:
                self.log("That block arrived a moment too late — continuing.",
                         "bad")
        self.q.put(("rate", 0.0))
        self.log("Mining stopped.")

    # ---------------------------------------------------------------- wallet
    def _view_wallet(self, f):
        p = self._page(f, "Wallet",
                       "Spend what you mine — your keys stay on this "
                       "machine and every transaction is signed here")

        chips = tk.Frame(p, bg=DUSK); chips.pack(fill="x", pady=(0, 14))
        self.chip_w_spend = self._chip(chips, "SPENDABLE NOW")
        self.chip_w_conf = self._chip(chips, "CONFIRMED")
        self.chip_w_imm = self._chip(chips, "MATURING")

        # ---- receive -----------------------------------------------------
        recv = tk.Frame(p, bg=DUSK2, highlightbackground=RUFOUS,
                        highlightthickness=1)
        recv.pack(fill="x", pady=(0, 12))
        rc = tk.Frame(recv, bg=DUSK2); rc.pack(fill="x", padx=18, pady=12)
        tk.Label(rc, text="YOUR ADDRESS — share it to get paid",
                 bg=DUSK2, fg=RUFOUS_HI, font=TINY_B).pack(anchor="w")
        rrow = tk.Frame(rc, bg=DUSK2); rrow.pack(fill="x", pady=(6, 0))
        self.w_addr_var = tk.StringVar(value="…")
        al = tk.Label(rrow, textvariable=self.w_addr_var, bg=SPOT, fg=BUFF,
                      font=MONO_13, anchor="w", padx=12, pady=9,
                      cursor="hand2")
        al.pack(side="left", fill="x", expand=True)
        al.bind("<Button-1>", lambda _e: self._w_copy_addr())
        Tooltip(al, "Click to copy")
        self._btn(rrow, "Copy", self._w_copy_addr, primary=True,
                  tip="Copy your address").pack(side="left", padx=(8, 0))
        self._btn(rrow, "Back up key", self._w_backup,
                  tip="Show the secret key that restores this wallet.\n"
                      "Anyone who has it can spend your coins."
                  ).pack(side="left", padx=(6, 0))

        # ---- send --------------------------------------------------------
        send = tk.Frame(p, bg=DUSK2); send.pack(fill="x", pady=(0, 12))
        sc = tk.Frame(send, bg=DUSK2); sc.pack(fill="x", padx=18, pady=14)
        tk.Label(sc, text="SEND KSL", bg=DUSK2, fg=MUTED,
                 font=TINY_B).pack(anchor="w")

        r1 = tk.Frame(sc, bg=DUSK2); r1.pack(fill="x", pady=(8, 0))
        tk.Label(r1, text="To", bg=DUSK2, fg=MUTED, font=SANS_9,
                 width=7, anchor="w").pack(side="left")
        self.w_to = tk.Entry(r1, bg=SPOT, fg=BUFF, insertbackground=BUFF,
                             relief="flat", font=MONO_9)
        self.w_to.pack(side="left", fill="x", expand=True, ipady=6, ipadx=8)

        r2 = tk.Frame(sc, bg=DUSK2); r2.pack(fill="x", pady=(7, 0))
        tk.Label(r2, text="Amount", bg=DUSK2, fg=MUTED, font=SANS_9,
                 width=7, anchor="w").pack(side="left")
        self.w_amt = tk.Entry(r2, bg=SPOT, fg=BUFF, insertbackground=BUFF,
                              relief="flat", font=MONO_9, width=18)
        self.w_amt.pack(side="left", ipady=6, ipadx=8)
        tk.Label(r2, text="KSL", bg=DUSK2, fg=FAINT,
                 font=SANS_9).pack(side="left", padx=(7, 0))
        self._btn(r2, "Send all", self._w_send_all,
                  tip="Fill in everything you can spend, minus the fee"
                  ).pack(side="left", padx=(10, 0))

        r3 = tk.Frame(sc, bg=DUSK2); r3.pack(fill="x", pady=(7, 0))
        tk.Label(r3, text="Fee", bg=DUSK2, fg=MUTED, font=SANS_9,
                 width=7, anchor="w").pack(side="left")
        self.w_fee = tk.Entry(r3, bg=SPOT, fg=BUFF, insertbackground=BUFF,
                              relief="flat", font=MONO_9, width=18)
        self.w_fee.pack(side="left", ipady=6, ipadx=8)
        self.w_fee.insert(0, format_ksl(params.MIN_RELAY_FEE).split()[0])
        tk.Label(r3, text="KSL  ·  paid to whoever mines your transaction",
                 bg=DUSK2, fg=FAINT, font=TINY).pack(side="left", padx=(7, 0))

        r4 = tk.Frame(sc, bg=DUSK2); r4.pack(fill="x", pady=(12, 0))
        self.w_send_btn = self._btn(r4, "Review and send", self._w_send,
                                    primary=True,
                                    tip="You'll get a confirmation prompt "
                                        "before anything is sent")
        self.w_send_btn.pack(side="left")
        self.w_note = tk.StringVar(value="")
        self.w_note_l = tk.Label(sc, textvariable=self.w_note, bg=DUSK2,
                                 fg=MUTED, font=SANS_9, wraplength=640,
                                 justify="left", anchor="w")
        self.w_note_l.pack(anchor="w", pady=(9, 0))

        # ---- history -----------------------------------------------------
        tk.Label(p, text="YOUR TRANSACTIONS", bg=DUSK, fg=MUTED,
                 font=TINY_B).pack(anchor="w", pady=(6, 6))
        wrap, self.w_tx = self._tree(
            p, (("When", 150, "w"), ("Kind", 96, "w"),
                ("Amount", 150, "e"), ("Status", 170, "w")),
            height=9, stretch="Status")
        wrap.pack(fill="both", expand=True)

    def _w_wallet(self):
        """The wallet this app owns the keys for, if any."""
        try:
            if os.path.exists(WALLET_FILE):
                return Wallet.load(WALLET_FILE)
        except Exception:
            pass
        return None

    def _w_copy_addr(self):
        a = self.w_addr_var.get()
        if a and a != "…":
            self._copy_text(a, "Address copied.")

    def _w_backup(self):
        w = self._w_wallet()
        if not w:
            self._say(
                "No key here",
                "This app is mining to an address you pasted in, so it "
                "doesn't hold that key — whichever wallet created the "
                "address has it.")
            return
        self._say(
            "Your backup key",
            "Address:\n" + w.address +
            "\n\nBackup key (keep secret):\n" +
            private_to_wif(w.private_key) +
            "\n\nWrite this down and keep it offline. It is the only way "
            "to restore your coins, and anyone who has it can spend them.")

    def _w_say(self, msg, colour=None):
        self.w_note.set(msg)
        self.w_note_l.configure(fg=colour or MUTED)

    def _w_send_all(self):
        w = self._w_wallet()
        if not w:
            return
        with self.lock:
            bal = self.chain.balance(w.address)
        try:
            fee = parse_ksl(self.w_fee.get())
        except ValueError:
            fee = params.MIN_RELAY_FEE
        amount = bal["spendable"] - fee
        if amount <= 0:
            self._w_say("There isn't enough spendable KSL to cover the fee "
                        "yet. Freshly mined coins need 10 confirmations.",
                        RED)
            return
        self.w_amt.delete(0, "end")
        self.w_amt.insert(0, format_ksl(amount).split()[0])

    def _w_send(self):
        w = self._w_wallet()
        if not w:
            self._say(
                "This app can't spend that address",
                "You're mining to an address that was made elsewhere, so "
                "the key isn't here. Open Kestrel Wallet to spend from it.")
            return
        to = self.w_to.get().strip()
        if not is_valid_address(to):
            self._w_say("That doesn't look like a Kestrel address — they "
                        "start with K.", RED)
            return
        if to == w.address:
            self._w_say("That's your own address.", RED)
            return
        try:
            amount = parse_ksl(self.w_amt.get())
        except ValueError as e:
            self._w_say(f"Amount: {e}", RED); return
        try:
            fee = parse_ksl(self.w_fee.get())
        except ValueError as e:
            self._w_say(f"Fee: {e}", RED); return
        if amount <= 0:
            self._w_say("Enter an amount greater than zero.", RED); return
        if fee < params.MIN_RELAY_FEE:
            self._w_say(f"The fee must be at least "
                        f"{format_ksl(params.MIN_RELAY_FEE)}.", RED)
            return
        with self.lock:
            bal = self.chain.balance(w.address)
        if amount + fee > bal["spendable"]:
            self._w_say(
                f"You can spend {format_ksl(bal['spendable'])} right now. "
                f"Newly mined blocks need 10 confirmations before they can "
                f"be spent.", RED)
            return
        if not self._ask(
                "Send KSL?",
                f"Send {format_ksl(amount)}\n"
                f"to {to}\n\n"
                f"Fee: {format_ksl(fee)}\n"
                f"Total: {format_ksl(amount + fee)}\n\n"
                "This cannot be undone. Check the address carefully."):
            return
        self.w_send_btn.configure(state="disabled")
        self._w_say("Signing and broadcasting…")
        threading.Thread(target=self._w_send_worker,
                         args=(w, to, amount, fee), daemon=True).start()

    def _w_send_worker(self, w, to, amount, fee):
        try:
            with self.lock:
                utxos = self.chain.utxos_for(w.address, spendable_only=True)
                tx = w.build_transaction(utxos, to, amount, fee)
                self.chain.add_transaction(tx)
                self.chain.save()
            self.node.broadcast("/tx", {"tx": tx.to_dict()})
            self.q.put(("wsent", format_ksl(amount)))
        except ValidationError as e:
            self.q.put(("wfail", str(e)))
        except Exception as e:
            self.q.put(("wfail", str(e)))

    # --------------------------------------------------------------- stats
    def _refresh_wallet(self):
        """Balances, address and history for the Wallet tab."""
        if not hasattr(self, "w_addr_var"):
            return
        # Show the address this app holds the key for — that is the one it
        # can actually spend from. Mining rewards may be pointed somewhere
        # else entirely (a pasted address), so say so rather than showing a
        # balance for one address and spending from another.
        own = self._w_wallet()
        payout = self.addr_e.get().strip()
        addr = own.address if own else payout
        if not is_valid_address(addr):
            return
        self.w_addr_var.set(addr)
        if own and is_valid_address(payout) and payout != own.address:
            self._w_say("Note: mining rewards are going to a different "
                        "address you pasted in, not this one.")
        with self.lock:
            bal = self.chain.balance(addr)
            height = self.chain.height
            rows, mem_rows = [], []
            for b in reversed(self.chain.blocks[-400:]):
                for tx in b.transactions:
                    got = sum(o.amount for o in tx.outputs
                              if o.address == addr)
                    if not got:
                        continue
                    confs = height - b.height + 1
                    if tx.is_coinbase:
                        kind = "Mined"
                        status = ("spendable" if confs >= params.COINBASE_MATURITY
                                  else f"matures in "
                                       f"{params.COINBASE_MATURITY - confs} blocks")
                    else:
                        kind = "Received"
                        status = f"{confs} confirmation(s)"
                    rows.append((time.strftime("%d %b, %H:%M",
                                               time.localtime(b.timestamp)),
                                 kind,
                                 format_ksl(got), status))
                    if len(rows) >= 60:
                        break
                if len(rows) >= 60:
                    break
            for tx in self.chain.mempool.values():
                got = sum(o.amount for o in tx.outputs if o.address == addr)
                if got:
                    mem_rows.append(("pending", "Incoming",
                                     format_ksl(got), "waiting for a block"))
        self.chip_w_spend.set(format_ksl(bal["spendable"]))
        self.chip_w_conf.set(format_ksl(bal["confirmed"]))
        self.chip_w_imm.set(format_ksl(bal["confirmed"] - bal["spendable"]))
        tv = self.w_tx
        tv.delete(*tv.get_children())
        for r in mem_rows + rows:
            tv.insert("", "end", values=r,
                      tags=("dim",) if r[0] == "pending" else ())
        self._hint_if_empty(tv, "Nothing yet — mine a block to get paid.")

    def _refresh_stats(self):
        try:
            self._refresh_wallet()
        except Exception:
            pass          # wallet tab is cosmetic; never break the miner
        addr = self.addr_e.get().strip()
        with self.lock:
            h = self.chain.height
            supply = self.chain.circulating_supply()
            reward = self.chain.block_subsidy(h + 1)
            bal = (self.chain.balance(addr)["confirmed"]
                   if is_valid_address(addr) else None)
            next_target = self.chain.next_target()
            self._block_work = (1 << 256) // (next_target + 1)
            diff = self.chain.difficulty_of(next_target)
            mem = len(self.chain.mempool)
        alive = len(self.node.alive_peers())
        known = len(self.node.peers)
        mined = format_ksl(supply).replace(" KSL", "")
        _h, target = self.node.sync_status()
        if target > h:
            line = (f"⬇ Downloading the public ledger…  block {h:,} "
                    f"of {target:,}   ·   {alive} node(s) online")
        else:
            line = (f"Block {h:,}   ·   {mined} of 44,000,000 KSL mined"
                    f"   ·   next block pays {format_ksl(reward)}"
                    f"   ·   {alive} node(s) online")
        if bal is not None:
            line += f"   ·   Your balance: {format_ksl(bal)}"
        self.status_var.set(line)
        self.updated_var.set("updated " + time.strftime("%H:%M:%S"))
        self.chip_bal.set(format_ksl(bal) if bal is not None else "—")

        # network context line on the Mine page
        net_rate = self._block_work / max(params.TARGET_BLOCK_TIME, 1)
        to_halving = params.HALVING_INTERVAL - (h % params.HALVING_INTERVAL)
        years = to_halving * params.TARGET_BLOCK_TIME / 31_557_600
        parts = [f"difficulty {diff:,.2f}",
                 f"whole network ≈ {fmt_rate(net_rate)}"]
        if self._cur_rate > 0 and net_rate > 0:
            share = self._cur_rate / net_rate * 100
            parts.append("your share ≈ "
                         + (f"{share:,.1f}%" if share >= 0.1 else "<0.1%"))
        parts.append(f"next halving in {to_halving:,} blocks"
                     f" (~{years:,.1f} years)")
        if mem:
            parts.append(f"{mem} pending tx(s)")
        self.netline_var.set("   ·   ".join(parts))
        if hasattr(self, "pend_btn"):
            self.pend_btn.configure(text=f"Pending ({mem})")
        if hasattr(self, "chip_online"):
            self.chip_online.set(f"{alive:,}")
            self.chip_known.set(f"{known:,}")
            self.chip_mem.set(f"{mem:,}")
            self.chip_height.set(f"{h:,}")

        if self.node_serving:
            self.dot_l.configure(fg=GREEN if alive else SLATE)
            self.net_pill.set(f"node on :{self.node.port} · {alive} peers")
            self.net_big.set(f"Running on port {self.node.port}  ·  "
                             f"{alive} of {known} peer(s) online  ·  "
                             f"block {h:,}")
            self.api_var.set(f"Dashboard + JSON API  ·  http://127.0.0.1:{self.node.port}/"
                             + ("   ·   LAN discovery on"
                                if self.node.discovery.active else "")
                             + ("   ·   worldwide discovery on"
                                if self.node.rendezvous.active else ""))
            pub = self.node.public_url()
            lan = f"http://{get_lan_ip()}:{self.node.port}"
            self.share_url = pub or lan
            self.share_big_var.set(self.share_url)
            if pub:
                sub = ("Reachable from anywhere. One connection is enough — "
                       "nodes pass peers to each other, so the mesh spreads "
                       "on its own.")
                if lan != pub:
                    sub += f"   ·   On your own Wi-Fi: {lan}"
            else:
                sub = ("Works for anyone on your network right now. For "
                       "friends across the internet, forward TCP "
                       f"{self.node.port} on your router — or simply "
                       "connect OUT to any reachable node once and you "
                       "are all on the same mesh.")
            self.share_sub_var.set(sub)
            reach = self.node.reachable
            if reach is True:
                self.reach_lbl.configure(fg=GREEN)
                self.reach_var.set("✓ Reachable from the internet — other "
                                   "people can connect directly to you.")
                self.fix_row.pack_forget()
            elif reach is False:
                self.reach_lbl.configure(fg=RED)
                self.reach_var.set(
                    "⚠ Others can't connect IN to you (your router or "
                    "firewall blocks incoming). You still sync and mine "
                    "fine. Want friends to connect to you? Press Fix my "
                    "connection — it opens just this app's port, with your "
                    "permission.")
                if not self._fixing:
                    self.fix_row.pack(anchor="w", pady=(10, 0))
            else:
                self.reach_lbl.configure(fg=FAINT)
                self.reach_var.set("Checking whether others can reach you…")
                self.fix_row.pack_forget()
        else:
            self.dot_l.configure(fg=FAINT)
            self.net_pill.set("node starting…")

    def _refresh_peers(self):
        if not hasattr(self, "peers_tv"):
            return
        self.peers_tv.delete(*self.peers_tv.get_children())
        info = dict(self.node.peer_info)
        for url in sorted(self.node.peers):
            i = info.get(url, {})
            alive = i.get("alive", False)
            height = i.get("height")
            last = i.get("last", 0)
            if alive:
                status, tag = "online", "pos"
            elif last:
                status, tag = "offline", "dim"
            else:
                status, tag = "trying…", "dim"
            seen = "—"
            if last:
                seen = ago(last)
            self.peers_tv.insert(
                "", "end", tags=(tag,),
                values=(url, status,
                        f"{height:,}" if height is not None else "—", seen))
        self._hint_if_empty(self.peers_tv,
                            "No nodes yet — they appear here automatically")
        self._zebra(self.peers_tv)

    def _quit(self):
        if self.mining.is_set() and not self._ask(
                "Stop mining and quit?",
                "Mining is running. Quit anyway?\n\n"
                "Blocks already found are safe — they are on the chain."):
            return
        self.mining.clear()
        try:
            self.settings["geometry"] = self.geometry()
            save_settings(self.settings)
        except Exception:
            pass
        try:
            self.node.stop()
        except Exception:
            pass
        time.sleep(0.1)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
