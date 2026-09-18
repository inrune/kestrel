"""
Kestrel desktop UI kit — the look, shared by the Miner and the Wallet.

Both apps used to carry their own copy of the palette, the fonts and a
dozen small widget helpers, which is how they drifted apart: the same
button was three different sizes depending on which window you were in.
They now build from this.

Tkinter gives you rectangles, text and a canvas. No rounded corners, no
shadows, no transparency. So the work goes where it can actually land:
spacing that keeps a rhythm, a type scale with real steps in it, colour
used sparingly enough to mean something, and the few things worth drawing
by hand (progress, sparklines, dials) done properly on a canvas.

Nothing here knows about blocks, coins or nodes — it is only the surface.
"""

import sys
import time
import traceback
import tkinter as tk

from . import logfile
from tkinter import ttk
from tkinter import font as tkfont


# ============================================================== palette
#
# A near-black base so the rufous accent carries, three surface steps for
# depth, and warm off-white text — a kestrel is a warm brown bird and the
# interface should not feel like a blue server rack.

INK = "#0C0F14"        # window background, deepest
RAIL = "#090B0F"       # sidebar, deeper still
SURFACE = "#11151C"    # page background
CARD = "#171C25"       # raised panel
CARD_HI = "#1E2531"    # hover / active panel
SUNK = "#0A0D12"       # inputs, tables — below the page
LINE = "#242C3A"       # visible border
LINE_SOFT = "#1A202A"  # a divider you should barely notice

TEXT = "#F2ECDE"       # primary
TEXT_DIM = "#A8A294"   # secondary
TEXT_FAINT = "#6C685D"  # tertiary, hints, placeholders

RUFOUS = "#D25E2E"     # the accent. A kestrel's back.
RUFOUS_HI = "#EA7038"
RUFOUS_LO = "#7E3819"

GREEN = "#4EA96A"
GREEN_LO = "#1B2A20"
RED = "#D6523F"
RED_LO = "#2C1A17"
AMBER = "#E0A63C"
AMBER_LO = "#2E2517"
SLATE = "#7E97B8"

ZEBRA = "#0D1117"      # alternate table row
FOCUS = "#3A4658"      # focus ring

# severity -> (spine colour, tinted background)
LEVELS = {
    "info": (SLATE, CARD),
    "good": (GREEN, GREEN_LO),
    "warn": (AMBER, AMBER_LO),
    "bad": (RED, RED_LO),
    "accent": (RUFOUS, CARD),
}

# ================================================================ scale
#
# One spacing unit, used everywhere. Layouts that pick their own padding
# per widget are what make an interface look assembled rather than built.
U = 4
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = U, U * 2, U * 3, U * 5, U * 8


# ================================================================ fonts
#
# Filled in by resolve_fonts() once a Tk root exists; the defaults are
# what Windows gets, and are only placeholders until then.
F = {
    "micro": ("Segoe UI", 7, "bold"),
    "tiny": ("Segoe UI", 8),
    "tiny_b": ("Segoe UI", 8, "bold"),
    "small": ("Segoe UI", 9),
    "small_b": ("Segoe UI", 9, "bold"),
    "body": ("Segoe UI", 10),
    "body_b": ("Segoe UI", 10, "bold"),
    "lead": ("Segoe UI", 11),
    "h3": ("Segoe UI", 12, "bold"),
    "h2": ("Segoe UI", 15, "bold"),
    "h1": ("Segoe UI", 19, "bold"),
    "brand": ("Segoe UI", 14, "bold"),
    "mono_micro": ("Consolas", 7),
    "mono_tiny": ("Consolas", 8),
    "mono_small": ("Consolas", 9),
    "mono": ("Consolas", 10),
    "mono_b": ("Consolas", 10, "bold"),
    "mono_lead": ("Consolas", 12),
    "mono_lead_b": ("Consolas", 12, "bold"),
    "mono_big": ("Consolas", 15, "bold"),
    "mono_huge": ("Consolas", 26, "bold"),
    "mono_display": ("Consolas", 30, "bold"),
}

SANS_STACK = ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
              "Helvetica Neue", "Inter", "DejaVu Sans", "Arial")
MONO_STACK = ("Cascadia Mono", "JetBrains Mono", "SF Mono", "Consolas",
              "Menlo", "DejaVu Sans Mono", "Courier New")


def resolve_fonts(root, scale: float = 1.0):
    """Pick the best families this machine actually has, and size them.

    `scale` nudges everything at once for people who want it bigger.
    """
    try:
        fams = set(tkfont.families(root))
    except Exception:
        return F
    sans = next((f for f in SANS_STACK if f in fams), "TkDefaultFont")
    mono = next((f for f in MONO_STACK if f in fams), "TkFixedFont")

    def sz(n):
        return max(6, int(round(n * scale)))

    spec = {
        "micro": (sans, 7, "bold"), "tiny": (sans, 8),
        "tiny_b": (sans, 8, "bold"), "small": (sans, 9),
        "small_b": (sans, 9, "bold"), "body": (sans, 10),
        "body_b": (sans, 10, "bold"), "lead": (sans, 11),
        "h3": (sans, 12, "bold"), "h2": (sans, 15, "bold"),
        "h1": (sans, 19, "bold"), "brand": (sans, 14, "bold"),
        "mono_micro": (mono, 7), "mono_tiny": (mono, 8),
        "mono_small": (mono, 9), "mono": (mono, 10),
        "mono_b": (mono, 10, "bold"), "mono_lead": (mono, 12),
        "mono_lead_b": (mono, 12, "bold"), "mono_big": (mono, 15, "bold"),
        "mono_huge": (mono, 26, "bold"), "mono_display": (mono, 30, "bold"),
    }
    for k, v in spec.items():
        F[k] = (v[0], sz(v[1])) + tuple(v[2:])
    return F


def font(name: str):
    return F.get(name, F["body"])


# ========================================================== ttk styling

def init_style(root):
    """Theme the ttk widgets we use: tables and scrollbars."""
    s = ttk.Style(root)
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass
    s.configure("K.Treeview", background=SUNK, fieldbackground=SUNK,
                foreground=TEXT, bordercolor=LINE, borderwidth=0,
                rowheight=32, font=F["small"])
    s.configure("K.Treeview.Heading", background=SURFACE, foreground=TEXT_FAINT,
                font=F["micro"], relief="flat", padding=(8, 7))
    s.map("K.Treeview", background=[("selected", CARD_HI)],
          foreground=[("selected", TEXT)])
    s.map("K.Treeview.Heading",
          background=[("active", CARD_HI)], foreground=[("active", TEXT_DIM)])
    s.layout("K.Treeview", [("K.Treeview.treearea", {"sticky": "nswe"})])
    s.configure("K.Vertical.TScrollbar", background=LINE, troughcolor=SUNK,
                bordercolor=SUNK, arrowcolor=TEXT_FAINT, relief="flat",
                gripcount=0, width=10)
    s.map("K.Vertical.TScrollbar", background=[("active", FOCUS)])
    return s


# ====================================================== staying alive
#
# Everything an app repeats — polling the node, redrawing a balance,
# draining a worker queue — is an `after` chain: a callback that ends by
# booking the next one. Which means a single unhandled exception anywhere
# in that callback doesn't just skip one update; the booking never
# happens and the chain is gone for the rest of the session. The window
# keeps working, so nothing looks broken — it simply stops finding
# anything out, and sits there showing whatever it last knew until the
# app is closed and opened again.
#
# That is the shape of "it said the payment was on its way until I
# restarted it". The loops below cannot die: a failing tick is reported
# once and the next one is booked regardless.

class Resilient:
    """Mixin for a tk.Tk app: self-healing timers and queue draining."""

    _err_seen: set = None

    def report(self, where: str, exc: BaseException):
        """Log a failure once per site. Never raises, never blocks."""
        if self._err_seen is None:
            self._err_seen = set()
        key = f"{where}:{type(exc).__name__}:{exc}"
        first = key not in self._err_seen
        self._err_seen.add(key)
        if first:
            # To the log file, with the traceback. NOT to the console: a
            # screenful of Python appearing over the window someone
            # launched the app from is alarming, useless to them, and —
            # launched without a console at all — a crash of its own,
            # because sys.stderr is then None.
            logfile.exception(where, exc, tb=traceback.format_exc())
        try:
            hook = getattr(self, "on_internal_error", None)
            if hook and first:
                hook(where, exc)
        except Exception:
            pass

    def report_callback_exception(self, exc, val, tb):
        """Tk's last-resort handler for anything a callback throws.

        Left alone, this prints the whole traceback to stderr — which is
        the wall of Python that lands in the console window the app was
        started from, and which is a crash in itself when there is no
        console (pythonw leaves sys.stderr as None). It goes to the log
        file instead, and the person gets one plain sentence.
        """
        import traceback as _tb
        try:
            logfile.exception("tk callback", val,
                              tb="".join(_tb.format_exception(exc, val, tb)))
        except Exception:
            pass
        try:
            key = f"tk:{getattr(exc, '__name__', exc)}:{val}"
            if self._err_seen is None:
                self._err_seen = set()
            if key not in self._err_seen:
                self._err_seen.add(key)
                hook = getattr(self, "on_internal_error", None)
                if hook:
                    hook("tk callback", val)
        except Exception:
            pass

    def _alive(self) -> bool:
        """Is this window still real? Used to tell teardown from a bug."""
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def safe(self, fn, *a, where=None, **kw):
        """Call something and swallow — but report — anything it throws."""
        try:
            return fn(*a, **kw)
        except tk.TclError:
            return None          # window going away mid-update; not news
        except Exception as e:
            self.report(where or getattr(fn, "__name__", "callback"), e)
            return None

    def every(self, ms, fn, *, where=None, jitter=0):
        """Run fn now, then every `ms`, forever — whatever fn does.

        Returns a callable that stops the loop.
        """
        state = {"job": None, "stop": False}
        name = where or getattr(fn, "__name__", "loop")

        def tick():
            if state["stop"]:
                return
            try:
                delay = fn()
            except tk.TclError as e:
                # TclError is not "the window went away" — it is Tk's
                # general-purpose error, and a bad index or a stale widget
                # raises it just as readily. Treating it as a shutdown
                # signal ended the loop for the rest of the session, with
                # nothing logged, which is the exact failure this class
                # exists to make impossible. Only stop when the window has
                # genuinely gone; otherwise report it and carry on.
                if not self._alive():
                    return
                self.report(name, e)
                delay = None
            except Exception as e:
                self.report(name, e)
                delay = None
            if state["stop"]:
                return
            wait = int(delay if isinstance(delay, (int, float)) and delay
                       else ms)
            try:
                state["job"] = self.after(max(wait + jitter, 1), tick)
            except Exception:
                # booking failed, so the chain is about to end. If the
                # window is still there that is a real problem and has to
                # be said out loud, not swallowed.
                state["stop"] = True
                if self._alive():
                    self.report(f"{name} (could not reschedule)",
                                RuntimeError("after() failed"))

        def cancel():
            state["stop"] = True
            if state["job"]:
                try:
                    self.after_cancel(state["job"])
                except Exception:
                    pass
        tick()
        return cancel

    def drain(self, q, handler, *, ms=120, where="queue"):
        """Drain a queue into `handler(kind, rest)` forever.

        One bad message is dropped and reported; the rest of the batch
        and every batch after it still get through.
        """
        import queue as _q

        def pump():
            for _ in range(500):            # never hog the event loop
                try:
                    kind, *rest = q.get_nowait()
                except _q.Empty:
                    break
                except Exception as e:
                    self.report(where, e)
                    break
                try:
                    handler(kind, rest)
                except tk.TclError:
                    pass
                except Exception as e:
                    self.report(f"{where}:{kind}", e)
            return ms
        return self.every(ms, pump, where=where)


# ============================================================== helpers

def fmt_age(seconds: float) -> str:
    """36 -> '36s', 240 -> '4 min', 7500 -> '2h 5m'."""
    s = max(int(seconds), 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def mid_ellipsis(s: str, keep: int = 10) -> str:
    s = str(s)
    return s if len(s) <= keep * 2 + 1 else f"{s[:keep]}…{s[-keep:]}"


def mix(a: str, b: str, t: float) -> str:
    """Blend two #rrggbb colours. t=0 -> a, t=1 -> b."""
    t = max(0.0, min(1.0, t))
    av = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    bv = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(
        int(round(x + (y - x) * t)) for x, y in zip(av, bv))


def hover(widget, normal: str, over: str, attr: str = "bg"):
    """Light up on pointer-over, respecting a disabled state.

    Both halves have to check. `on` alone was not enough: a disabled
    button was correctly left alone on the way in, then repainted to its
    ENABLED colour on the way out, because `off` restored the colour it
    was given at birth. Passing the pointer over a greyed-out Send button
    turned it solid orange and it stayed that way — looking like the one
    thing on screen you are meant to press, and doing nothing at all.
    """
    def on(_e=None):
        if str(widget.cget("state")) != "disabled":
            widget.configure(**{attr: over})

    def off(_e=None):
        if str(widget.cget("state")) != "disabled":
            widget.configure(**{attr: normal})
    widget.bind("<Enter>", on, add="+")
    widget.bind("<Leave>", off, add="+")


# ============================================================== widgets

def card(parent, bg=CARD, pad=PAD_M, border=LINE):
    """A panel with a hairline border. Returns (outer, inner)."""
    outer = tk.Frame(parent, bg=bg, highlightbackground=border,
                     highlightcolor=border, highlightthickness=1, bd=0)
    inner = tk.Frame(outer, bg=bg)
    inner.pack(fill="both", expand=True, padx=pad + PAD_S, pady=pad)
    return outer, inner


def spined_card(parent, level="info", pad=PAD_M):
    """A card with a coloured spine down its left edge. (outer, inner)."""
    spine_c, bg = LEVELS.get(level, LEVELS["info"])
    outer = tk.Frame(parent, bg=bg, highlightbackground=LINE,
                     highlightcolor=LINE, highlightthickness=1, bd=0)
    tk.Frame(outer, bg=spine_c, width=3).pack(side="left", fill="y")
    inner = tk.Frame(outer, bg=bg)
    inner.pack(side="left", fill="both", expand=True,
               padx=(pad + PAD_S, pad + PAD_S), pady=pad)
    return outer, inner


def section(parent, text, bg=SURFACE, right=None):
    """A small capitalised section heading with a rule running off it."""
    row = tk.Frame(parent, bg=bg)
    tk.Label(row, text=text.upper(), bg=bg, fg=RUFOUS_HI,
             font=F["micro"]).pack(side="left")
    if right is not None:
        right(row)
    tk.Frame(row, bg=LINE_SOFT, height=1).pack(
        side="left", fill="x", expand=True, padx=(PAD_M, 0), pady=(6, 0))
    return row


def label(parent, text="", bg=CARD, fg=TEXT, f="body", **kw):
    return tk.Label(parent, text=text, bg=bg, fg=fg, font=F[f], **kw)


def button(parent, text, cmd, *, kind="normal", tip=None, bg_of=None, **kw):
    """kind: primary | normal | quiet | danger."""
    palette = {
        "primary": (RUFOUS, RUFOUS_HI, INK),
        "normal": (CARD_HI, FOCUS, TEXT),
        "quiet": (bg_of or SURFACE, CARD_HI, TEXT_DIM),
        "danger": (RED_LO, RED, TEXT),
    }
    base, over, fg = palette.get(kind, palette["normal"])
    b = tk.Button(parent, text=text, command=cmd, bg=base, fg=fg,
                  activebackground=over, activeforeground=fg,
                  disabledforeground=TEXT_FAINT,
                  relief="flat", bd=0, cursor="hand2", highlightthickness=0,
                  font=F["body_b"] if kind == "primary" else F["small_b"],
                  padx=PAD_M + PAD_XS, pady=PAD_S + 1, **kw)
    hover(b, base, over)
    b._kestrel_colors = (base, over, fg)
    if tip:
        Tooltip(b, tip)
    return b


def link(parent, text, cmd, bg=SURFACE, tip=None):
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=SLATE,
                  activebackground=bg, activeforeground=TEXT, relief="flat",
                  bd=0, cursor="hand2", font=F["small_b"], highlightthickness=0,
                  padx=PAD_S, pady=2)
    hover(b, SLATE, TEXT, attr="fg")
    if tip:
        Tooltip(b, tip)
    return b


def pill(parent, textvar=None, text="", level="info", bg=CARD):
    """A small status chip. Pass a StringVar to keep it live."""
    fg, _ = LEVELS.get(level, LEVELS["info"])
    kw = {"textvariable": textvar} if textvar is not None else {"text": text}
    return tk.Label(parent, bg=mix(bg, fg, 0.12), fg=fg, font=F["tiny_b"],
                    padx=PAD_S, pady=2, **kw)


def entry(parent, app=None, width=None, f="body", **kw):
    e = tk.Entry(parent, bg=SUNK, fg=TEXT, insertbackground=RUFOUS_HI,
                 relief="flat", bd=0, font=F[f],
                 highlightbackground=LINE, highlightcolor=RUFOUS,
                 highlightthickness=1, selectbackground=RUFOUS_LO,
                 selectforeground=TEXT, disabledbackground=SUNK,
                 disabledforeground=TEXT_FAINT, **kw)
    if width:
        e.configure(width=width)
    return e


def divider(parent, bg=SURFACE, pad=PAD_M):
    f = tk.Frame(parent, bg=LINE_SOFT, height=1)
    f.pack(fill="x", pady=pad)
    return f


class StatTile:
    """A label, a big value and an optional footnote, in a bordered box."""

    def __init__(self, parent, title, *, value="—", note="", accent=TEXT,
                 f="mono_lead_b", bg=CARD, width=None, tip=None):
        self.var = tk.StringVar(value=value)
        self.note_var = tk.StringVar(value=note)
        self.frame = tk.Frame(parent, bg=bg, highlightbackground=LINE,
                              highlightcolor=LINE, highlightthickness=1, bd=0)
        inner = tk.Frame(self.frame, bg=bg)
        inner.pack(fill="both", expand=True, padx=PAD_M, pady=PAD_S + 2)
        tk.Label(inner, text=title.upper(), bg=bg, fg=TEXT_FAINT,
                 font=F["micro"], anchor="w").pack(fill="x")
        self.value_lbl = tk.Label(inner, textvariable=self.var, bg=bg,
                                  fg=accent, font=F[f], anchor="w")
        self.value_lbl.pack(fill="x", pady=(3, 0))
        self.note_lbl = tk.Label(inner, textvariable=self.note_var, bg=bg,
                                 fg=TEXT_FAINT, font=F["tiny"], anchor="w")
        if note:
            self.note_lbl.pack(fill="x")
        if width:
            self.frame.configure(width=width)
            self.frame.pack_propagate(False)
        if tip:
            Tooltip(self.frame, tip)
            Tooltip(self.value_lbl, tip)

    def set(self, value, note=None):
        self.var.set(value)
        if note is not None:
            self.note_var.set(note)
            if note and not self.note_lbl.winfo_ismapped():
                self.note_lbl.pack(fill="x")
            elif not note and self.note_lbl.winfo_ismapped():
                self.note_lbl.pack_forget()

    def color(self, fg):
        self.value_lbl.configure(fg=fg)

    def pack(self, **kw):
        self.frame.pack(**kw)
        return self

    def grid(self, **kw):
        self.frame.grid(**kw)
        return self


class Odometer:
    """A number that counts to its new value instead of jumping.

    Only for the headline balance. Applied to every figure on screen it
    would be noise; on the one number someone is actually watching, it
    makes an arrival feel like an arrival.
    """

    def __init__(self, widget, var, *, fmt=lambda v: f"{v:,.8f}", ms=420):
        self.w, self.var, self.fmt, self.ms = widget, var, fmt, ms
        self._from = self._to = 0.0
        self._t0 = None
        self._job = None

    def set(self, value: float, *, animate=True):
        value = float(value)
        if not animate or abs(value - self._to) < 1e-12:
            self._stop()
            self._from = self._to = value
            self.var.set(self.fmt(value))
            return
        self._from, self._to, self._t0 = self._current(), value, time.time()
        if self._job is None:
            self._tick()

    def _current(self):
        if self._t0 is None:
            return self._to
        p = min((time.time() - self._t0) / (self.ms / 1000), 1.0)
        return self._from + (self._to - self._from) * p

    def _stop(self):
        if self._job is not None:
            try:
                self.w.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _tick(self):
        p = min((time.time() - self._t0) / (self.ms / 1000), 1.0)
        eased = 1 - (1 - p) ** 3                     # ease-out cubic
        self.var.set(self.fmt(self._from + (self._to - self._from) * eased))
        if p >= 1.0:
            self._job = None
            self._from = self._to
            return
        try:
            self._job = self.w.after(16, self._tick)
        except Exception:
            self._job = None


class Meter:
    """A flat progress bar drawn on a canvas. Determinate or indeterminate."""

    def __init__(self, parent, *, width=280, height=6, bg=CARD,
                 fill=RUFOUS, track=None):
        self.fill, self.h, self.w = fill, height, width
        self.c = tk.Canvas(parent, width=width, height=height, bg=bg,
                           highlightthickness=0, bd=0)
        self._track = track or mix(bg, TEXT, 0.10)
        self._value = 0.0
        self._spin = None
        self._pos = 0.0
        self.c.bind("<Configure>", self._resize)
        self._draw()

    def _resize(self, e):
        w = max(e.width, 1)
        if w == self.w:
            return                      # Tk sends these freely; most change nothing
        self.w = w
        self._draw()

    def set(self, fraction: float):
        self.stop()
        self._value = max(0.0, min(1.0, fraction))
        self._draw()

    def _draw(self):
        self.c.delete("all")
        self.c.create_rectangle(0, 0, self.w, self.h, fill=self._track,
                                outline="")
        w = int(self.w * self._value)
        if w > 0:
            self.c.create_rectangle(0, 0, w, self.h, fill=self.fill,
                                    outline="")

    def start(self):
        """Indeterminate: a band sliding along, for work of unknown length."""
        if self._spin is not None:
            return

        def step():
            self._pos = (self._pos + 0.018) % 1.0
            self.c.delete("all")
            self.c.create_rectangle(0, 0, self.w, self.h,
                                    fill=self._track, outline="")
            band = max(int(self.w * 0.28), 40)
            x = int((self.w + band) * self._pos) - band
            self.c.create_rectangle(max(x, 0), 0, min(x + band, self.w),
                                    self.h, fill=self.fill, outline="")
            self._spin = self.c.after(16, step)
        step()

    def stop(self):
        if self._spin is not None:
            try:
                self.c.after_cancel(self._spin)
            except Exception:
                pass
            self._spin = None

    def pack(self, **kw):
        self.c.pack(**kw)
        return self

    def grid(self, **kw):
        self.c.grid(**kw)
        return self


class Sparkline:
    """A filled line chart of recent samples, with a soft baseline."""

    def __init__(self, parent, *, width=460, height=64, bg=SUNK,
                 color=RUFOUS_HI):
        self.color, self.bg = color, bg
        self.c = tk.Canvas(parent, width=width, height=height, bg=bg,
                           highlightthickness=1, highlightbackground=LINE,
                           bd=0)
        self._last = []
        self._size = (0, 0)
        self._redraw = None
        self._last_evt = 0.0
        self._opts = {}
        self.c.bind("<Configure>", self._resized)

    SETTLE = 60          # ms of quiet before the chart is redrawn

    def _resized(self, e):
        """Redraw once the drag settles, not on every pixel.

        Dragging a window edge delivers a Configure per step, and redrawing
        a couple of hundred points each time is what makes a resize feel
        like treacle on a slow machine. One redraw at the end looks the
        same and costs a fraction as much.
        """
        if (e.width, e.height) == self._size:
            return
        self._size = (e.width, e.height)
        self._last_evt = time.time()
        if self._redraw is not None:
            return                  # already waiting; _do_redraw re-checks
        try:
            self._redraw = self.c.after(self.SETTLE, self._do_redraw)
        except Exception:
            self._redraw = None

    def _do_redraw(self):
        """Only draw once the resizing has genuinely stopped.

        Rebooking a timer per event is not enough on its own: Tk delivers
        Configure in bursts with idle gaps between them, and every gap
        longer than the delay lets the timer through. Checking when the
        last event actually arrived does not care how they were delivered.
        """
        self._redraw = None
        quiet = (time.time() - self._last_evt) * 1000
        if quiet < self.SETTLE:
            try:
                self._redraw = self.c.after(int(self.SETTLE - quiet) + 5,
                                            self._do_redraw)
                return
            except Exception:
                self._redraw = None
        try:
            self.draw(self._last, **self._opts)
        except Exception:
            pass

    def draw(self, samples, *, label_fn=None, empty="warming up…"):
        self._last = list(samples or [])
        # remembered so a resize redraws the same chart, labels and all
        self._opts = {"label_fn": label_fn, "empty": empty}
        c = self.c
        c.delete("all")
        w = max(int(c.winfo_width() or c["width"]), 2)
        h = max(int(c.winfo_height() or c["height"]), 2)
        for frac in (0.25, 0.5, 0.75):
            y = h * frac
            c.create_line(0, y, w, y, fill=mix(self.bg, TEXT, 0.07))
        if len(self._last) < 2:
            c.create_text(w // 2, h // 2, fill=TEXT_FAINT,
                          font=F["mono_tiny"], text=empty)
            return
        top = max(self._last) or 1.0
        n = len(self._last)
        pts = [(i * (w - 2) / (n - 1) + 1,
                h - 3 - (v / top) * (h - 10)) for i, v in enumerate(self._last)]
        c.create_polygon(*[coord for p in pts for coord in p], w, h, 1, h,
                         fill=mix(self.bg, self.color, 0.16), outline="")
        c.create_line(*[coord for p in pts for coord in p],
                      fill=self.color, width=2, smooth=True)
        x, y = pts[-1]
        c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=self.color, outline="")
        if label_fn:
            c.create_text(w - 8, 10, anchor="e", fill=TEXT_DIM,
                          font=F["mono_tiny"], text=label_fn(top))

    def pack(self, **kw):
        self.c.pack(**kw)
        return self


class Tooltip:
    """A small dark tooltip after a short hover.

    This one stays a separate window on purpose: a tooltip on a control
    near the bottom edge has to be allowed to hang past it, and an in-app
    widget would be clipped instead. The price is that it has to be
    dismissed by hand on anything that would otherwise strand it —
    including the app losing focus, which is what alt-tabbing does and
    what used to leave a tooltip sitting on top of the program you had
    just switched to.
    """

    def __init__(self, widget, text, delay=550):
        self.w, self.text, self.delay = widget, text, delay
        self.tip = self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")
        try:    # moving, minimising, or switching away all strand it
            top = widget.winfo_toplevel()
            top.bind("<Configure>", self._window_changed, add="+")
            top.bind("<Unmap>", self._window_changed, add="+")
            top.bind("<FocusOut>", self._window_changed, add="+")
        except Exception:
            pass

    def _window_changed(self, _e=None):
        """An app has dozens of tooltips and a drag fires hundreds of
        events, so this runs thousands of times for one gesture. It has to
        do nothing, quickly, unless this particular tooltip is involved."""
        if self.tip is None and self.job is None:
            return
        self._hide()

    def _schedule(self, _e=None):
        self._cancel()
        try:
            self.job = self.w.after(self.delay, self._show)
        except Exception:
            self.job = None

    def _cancel(self):
        if self.job:
            try:
                self.w.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _show(self):
        if self.tip or not self.text:
            return
        try:
            if not self.w.winfo_exists():
                return
            x = self.w.winfo_rootx() + 14
            y = self.w.winfo_rooty() + self.w.winfo_height() + 7
            sw = self.w.winfo_screenwidth()
            sh = self.w.winfo_screenheight()
        except Exception:
            return
        try:
            self.tip = tk.Toplevel(self.w)
        except Exception:
            self.tip = None
            return
        self.tip.wm_overrideredirect(True)
        self.tip.configure(bg=LINE)
        tk.Label(self.tip, text=self.text, bg=CARD_HI, fg=TEXT,
                 font=F["small"], padx=PAD_S + 2, pady=PAD_XS + 2,
                 justify="left", wraplength=320).pack(padx=1, pady=1)
        try:    # keep it on the screen rather than half off the edge
            self.tip.update_idletasks()
            w, h = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
            x = max(0, min(x, sw - w - 4))
            if y + h > sh:
                y = self.w.winfo_rooty() - h - 7
            self.tip.wm_geometry(f"+{x}+{max(0, y)}")
            self.tip.lift()
        except Exception:
            pass

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class Toasts:
    """Stacked transient messages in the corner of the app window.

    These used to be separate top-level windows — the usual trick for
    floating something over a Tk layout, and the wrong one. A separate
    window is not part of the app: it does not move when the app moves,
    it does not go behind the app when you switch to another program, and
    it does not disappear when the app is minimised. Every one of those
    has to be chased manually, by watching the window for <Configure>,
    <Map> and <Unmap> and re-deriving screen coordinates each time. That
    is what made them drift after the window, flicker when alt-tabbing,
    and hang over other applications.

    They are ordinary widgets now, placed inside the window itself. All
    of that behaviour comes free and correct: they are clipped by the
    window, travel with it because they are *in* it, vanish and return
    with it, and can never be left stranded on the desktop. The stacking
    is Tk's packer rather than arithmetic on screen coordinates, so there
    is nothing to recompute when the window moves or resizes — and no
    per-event work during a drag at all.
    """

    MARGIN = PAD_L
    GAP = PAD_S
    WIDTH = 330

    def __init__(self, root):
        self.root = root
        self.area = root        # the region messages are pinned inside
        self.items = []
        self._host = None
        self._reaping = False

    def in_area(self, widget):
        """Pin messages inside `widget` rather than the whole window.

        An app with a status bar along the bottom wants them above it, not
        over it — a message covering the buttons on a strip you are meant
        to click is worse than no message, because clicking it dismisses
        the message instead of pressing the button underneath.
        """
        self.area = widget
        if self._host is not None:
            for t in list(self.items):
                self.close(t)
            try:
                self._host.destroy()
            except Exception:
                pass
            self._host = None

    # -- the overlay the messages live in ------------------------------
    def _ensure_host(self):
        """A container pinned to the window's bottom-right corner.

        Placed with relative coordinates, so Tk keeps it in the corner
        through every move and resize without being told.
        """
        if self._host is not None and self._host.winfo_exists():
            return self._host
        try:
            self._host = tk.Frame(self.area, bg=SURFACE)
        except Exception:
            self._host = None
            return None
        return self._host

    def _show_host(self):
        h = self._ensure_host()
        if h is None:
            return
        try:
            # width is fixed so every card matches; height is whatever the
            # stack currently needs, which Tk works out for us
            h.place(relx=1.0, rely=1.0, anchor="se",
                    x=-self.MARGIN, y=-self.MARGIN, width=self.WIDTH)
            h.lift()            # above the page, whichever view is showing
        except Exception:
            pass

    def _hide_host(self):
        """Taken out of the layout entirely when empty.

        A leftover 1x1 frame in the corner would still swallow the clicks
        that landed on it.
        """
        if self._host is None:
            return
        try:
            self._host.place_forget()
        except Exception:
            pass

    # -- showing -------------------------------------------------------
    def show(self, text, kind="info", ms=4800):
        accent, _ = LEVELS.get(kind, LEVELS["info"])
        host = self._ensure_host()
        if host is None:
            return None
        try:
            t = tk.Frame(host, bg=LINE)
        except Exception:
            return None
        # newest at the bottom, nearest the corner; the packer reflows the
        # stack on its own when one is removed
        t.pack(side="bottom", fill="x", pady=(self.GAP, 0))

        shell = tk.Frame(t, bg=CARD_HI)
        shell.pack(padx=1, pady=1, fill="both", expand=True)
        tk.Frame(shell, bg=accent, width=3).pack(side="left", fill="y")
        body = tk.Frame(shell, bg=CARD_HI)
        body.pack(side="left", fill="both", expand=True)
        msg = tk.Label(body, text=text, bg=CARD_HI, fg=TEXT, font=F["small"],
                       padx=PAD_M, pady=PAD_S + 1, justify="left",
                       wraplength=self.WIDTH - 52, anchor="w")
        msg.pack(side="left", fill="both", expand=True)
        shut = tk.Label(body, text="✕", bg=CARD_HI, fg=TEXT_FAINT,
                        font=F["tiny_b"], padx=PAD_S, cursor="hand2")
        shut.pack(side="right", fill="y")
        hover(shut, TEXT_FAINT, TEXT, attr="fg")

        t._kestrel_deadline = time.time() + ms / 1000.0
        t._kestrel_held = False

        # Whether the pointer is over the card is one question, but Tk asks
        # it once per widget: moving from a frame onto its own child sends
        # the frame a <Leave>. Counting enters and leaves instead of
        # storing a flag means crossing an internal boundary nets out to
        # zero, so resting on a long message holds it for as long as you
        # are reading rather than the 1.2s an "I left" would have given.
        t._kestrel_over = 0

        def hold(_e=None):
            t._kestrel_over += 1
            t._kestrel_held = True

        def release(_e=None):
            t._kestrel_over = max(0, t._kestrel_over - 1)
            if t._kestrel_over:
                return                    # still on the card, just a child
            t._kestrel_held = False
            t._kestrel_deadline = max(t._kestrel_deadline,
                                      time.time() + 1.2)

        # Tk has no event bubbling, so every part of the card needs its own
        # binding. The message label was missing, which is most of the card
        # — clicking the text you are trying to dismiss did nothing.
        for w in (t, shell, body, msg, shut):
            w.bind("<Enter>", hold, add="+")
            w.bind("<Leave>", release, add="+")
            w.bind("<Button-1>", lambda _e, tt=t: self.close(tt), add="+")
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

        self.items.append(t)
        self._show_host()
        self._reap()
        return t

    def _reap(self):
        """One timer for all of them, so a held toast can outstay its slot."""
        if self._reaping:
            return
        now = time.time()
        for t in list(self.items):
            try:
                gone = not t.winfo_exists()
            except Exception:
                gone = True
            if gone:
                if t in self.items:
                    self.items.remove(t)
                continue
            if not getattr(t, "_kestrel_held", False) and \
                    now >= getattr(t, "_kestrel_deadline", 0):
                self.close(t)
        if self.items:
            # Switching view packs a new page into the window, which can
            # land on top of the overlay. Staying lifted while anything is
            # showing costs one call every quarter second and only while a
            # message is actually up.
            try:
                if self._host is not None and self._host.winfo_exists():
                    self._host.lift()
            except Exception:
                pass
            self._reaping = True
            try:
                self.root.after(250, self._tick)
            except Exception:
                self._reaping = False
        else:
            self._hide_host()

    def _tick(self):
        self._reaping = False
        self._reap()

    def close(self, t):
        if t in self.items:
            self.items.remove(t)
        try:
            t.destroy()          # the packer closes the gap by itself
        except Exception:
            pass
        if not self.items:
            self._hide_host()


def tree(parent, spec, *, height=8, stretch=None, bg=SURFACE, app=None):
    """Treeview with a self-hiding scrollbar. spec: [(name, width, anchor)]."""
    wrap = tk.Frame(parent, bg=bg, highlightbackground=LINE,
                    highlightcolor=LINE, highlightthickness=1, bd=0)
    cols = [n for n, _w, _a in spec]
    tv = ttk.Treeview(wrap, columns=cols, show="headings", style="K.Treeview",
                      height=height, selectmode="browse")
    for name, wdt, anc in spec:
        # A heading defaults to centred whatever its column does, so a
        # left-aligned column of addresses sat under a heading floating in
        # the middle of it, and an amount column right-aligned under a
        # heading that wasn't. Match them.
        tv.heading(name, text=name.upper(), anchor=anc)
        tv.column(name, width=wdt, anchor=anc, stretch=(name == stretch),
                  minwidth=max(48, wdt // 2))
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview,
                       style="K.Vertical.TScrollbar")
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
    for tag, fg in (("pos", GREEN), ("neg", RED), ("pend", AMBER),
                    ("dim", TEXT_FAINT), ("hint", TEXT_FAINT),
                    ("accent", RUFOUS_HI), ("stuck", RED),
                    # rows that are yours — the Miner tags its own blocks
                    # with this and nothing ever gave it a colour, so the
                    # comparison behind it was doing nothing at all
                    ("mine", RUFOUS_HI)):
        tv.tag_configure(tag, foreground=fg)
    tv.tag_configure("even", background=SUNK)
    tv.tag_configure("odd", background=ZEBRA)
    _sortable(tv)
    return wrap, tv


def zebra(tv):
    for i, iid in enumerate(tv.get_children()):
        tags = [t for t in tv.item(iid, "tags") if t not in ("even", "odd")]
        tags.append("even" if i % 2 == 0 else "odd")
        tv.item(iid, tags=tags)


def hint_if_empty(tv, text):
    """Put an empty-state line in the widest column, so it isn't clipped."""
    if tv.get_children():
        return
    cols = list(tv["columns"])
    # prefer the widest LEFT-aligned column: a hint right-aligned in an
    # amount column reads as data, which is the opposite of the point
    def score(i):
        w = int(tv.column(cols[i], "width") or 0)
        return (tv.column(cols[i], "anchor") in ("w", "west"), w)
    widest = max(range(len(cols)), key=score)
    vals = [""] * len(cols)
    vals[widest] = text
    tv.insert("", "end", values=vals, tags=("hint",))


def _sortable(tv):
    def key(v):
        s = (str(v).replace(",", "").replace("+", "").replace("−", "-")
             .replace(" KSL", "").replace("…", "").strip())
        try:
            return (0, float(s), "")
        except ValueError:
            return (1, 0.0, s.lower())

    def sort(col, rev):
        rows = [(tv.set(i, col), i) for i in tv.get_children()]
        rows.sort(key=lambda t: key(t[0]), reverse=rev)
        for pos, (_v, i) in enumerate(rows):
            tv.move(i, "", pos)
        tv.heading(col, command=lambda c=col: sort(c, not rev))
        zebra(tv)

    for c in tv["columns"]:
        tv.heading(c, command=lambda c=c: sort(c, False))

# ============================================================== the mark
#
# The Kestrel icon, as PNG bytes, so the apps put the real bird in the
# window title bar, the taskbar and the alt-tab switcher instead of the
# rough shape they used to draw by hand. Tk reads PNG from base64
# directly (8.6+), and if anything about that fails the caller falls back
# to drawing, so a missing feature can never stop an app opening.

ICON_PNG = {
    64: (
        "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"
    ),
    32: (
        "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"
        "YII="
    ),
}


_MARK_CACHE = {}


def mark(root, size=64):
    """The Kestrel mark as a Tk image, cached. None if Tk won't take it.

    Only the sizes embedded above are exact; anything else is reduced from
    the nearest larger one, since Tk can only subsample by whole numbers.
    """
    key = (id(root.winfo_toplevel()), size)
    if key in _MARK_CACHE:
        return _MARK_CACHE[key]
    try:
        exact = ICON_PNG.get(size)
        if exact:
            img = tk.PhotoImage(data=exact, master=root)
        else:
            bigger = min((k for k in ICON_PNG if k >= size), default=None)
            if bigger is None:
                return None
            img = tk.PhotoImage(data=ICON_PNG[bigger], master=root)
            factor = max(1, round(bigger / size))
            if factor > 1:
                img = img.subsample(factor, factor)
    except Exception:
        return None
    _MARK_CACHE[key] = img
    return img


def brand(parent, text="kestrel", sub=None, bg=RAIL, size=26):
    """The mark and the wordmark, as one block. Returns the frame."""
    f = tk.Frame(parent, bg=bg)
    row = tk.Frame(f, bg=bg)
    row.pack(anchor="w")
    img = mark(parent, size)
    if img is not None:
        lbl = tk.Label(row, image=img, bg=bg, bd=0)
        lbl._kestrel_img = img          # Tk drops un-referenced images
        lbl.pack(side="left", padx=(0, 9))
    else:
        tk.Label(row, text="▲", bg=bg, fg=RUFOUS,
                 font=F["brand"]).pack(side="left", padx=(0, 7))
    tk.Label(row, text=text, bg=bg, fg=TEXT,
             font=F["brand"]).pack(side="left")
    if sub:
        tk.Label(f, text=sub.upper(), bg=bg, fg=RUFOUS_HI,
                 font=F["micro"]).pack(anchor="w", pady=(2, 0))
    return f


def app_icon(root):
    """Set the window icon. Returns the images, which must be kept alive."""
    kept = []
    try:
        for size in (64, 32):
            kept.append(tk.PhotoImage(data=ICON_PNG[size]))
        root.iconphoto(True, *kept)
    except Exception:
        return None
    return kept
