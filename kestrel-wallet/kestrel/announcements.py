"""
Project announcements, pulled from a plain text file on GitHub.

Lets the project tell people running the apps about releases, network
events or security notices, without shipping a new build.

Design rules, in order of importance:

*   **Quiet on failure.** Offline, rate-limited, file missing, malformed
    — every one of those ends in "show nothing". An announcement channel
    that produces error popups is worse than no channel at all.
*   **Never trusted.** Whatever this returns is remote text that a repo
    compromise could rewrite. It is displayed as a message and nothing
    more: it can't run, click, install or configure anything. Callers
    must treat the text as untrusted and the accompanying link as
    something the reader chooses to open, never something opened for
    them.
*   **Optional.** The caller checks the user's setting before ever
    calling ``check``. No setting, no request.

The feed is a text file so it can be edited straight in GitHub's web
editor by someone who does not want to touch JSON. Format:

    # comments start with a hash
    id: welcome
    date: 2026-09-01
    level: info
    title: The network is live
    body: Back up your wallet file somewhere offline.
      Losing it means losing your coins.
    link: https://github.com/inrune/kestrel
    ---
    id: v147
    level: important
    title: Please update to v1.4.7
    body: This release fixes a sync bug.

``---`` on its own line separates entries. Keys may repeat onto the next
line by indenting it. Unknown keys are ignored, so the format can grow
later without breaking older apps.
"""

import hashlib
import re
import threading
import urllib.request

from . import __version__ as CURRENT

FEED_URL = ("https://raw.githubusercontent.com/inrune/kestrel/"
            "main/announcements.txt")
TIMEOUT = 8

MAX_BYTES = 64 * 1024      # a feed bigger than this is not a feed
MAX_ITEMS = 20             # ...and nobody needs to read more than this
MAX_TITLE = 120
MAX_BODY = 1200

LEVELS = ("info", "important", "urgent")

# Control characters have no business in a message box, and are the
# usual way to smuggle line-noise or fake UI into displayed text.
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str, limit: int) -> str:
    text = _CTRL.sub("", text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def parse(raw: str) -> list[dict]:
    """Parse the feed. Never raises — bad entries are dropped."""
    items = []
    for chunk in re.split(r"(?m)^-{3,}\s*$", raw or ""):
        fields, key = {}, None
        for line in chunk.splitlines():
            if line.lstrip().startswith("#"):
                continue
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
            if m and not line[:1].isspace():
                key = m.group(1).lower()
                fields[key] = m.group(2)
            elif key and line.strip():
                fields[key] += "\n" + line.strip()

        title = _clean(fields.get("title", ""), MAX_TITLE)
        body = _clean(fields.get("body", ""), MAX_BODY)
        if not title and not body:
            continue

        level = _clean(fields.get("level", "info"), 16).lower()
        if level not in LEVELS:
            level = "info"

        # Only http(s) links are ever shown. file:, javascript: and the
        # like must never reach a webbrowser.open call.
        link = _clean(fields.get("link", ""), 300)
        if link and not re.match(r"https?://", link, re.I):
            link = ""

        # A stable id lets the app remember what has already been shown.
        # Falling back to a content hash means an entry without one is
        # still only shown once, and editing it re-shows it — which is
        # the behaviour you want from a corrected notice.
        ident = _clean(fields.get("id", ""), 64)
        if not ident:
            ident = hashlib.sha256(
                (title + body).encode("utf-8", "replace")).hexdigest()[:16]

        items.append({
            "id": ident,
            "date": _clean(fields.get("date", ""), 24),
            "level": level,
            "title": title,
            "body": body,
            "link": link,
        })
        if len(items) >= MAX_ITEMS:
            break
    return items


def fetch() -> list[dict]:
    """Download and parse the feed. Returns [] on any failure."""
    try:
        req = urllib.request.Request(
            FEED_URL, headers={"User-Agent": f"kestrel/{CURRENT}",
                               "Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return []
        return parse(raw.decode("utf-8", "replace"))
    except Exception:
        return []          # offline, 404, DNS, TLS — all the same: stay quiet


def unseen(items: list[dict], seen) -> list[dict]:
    """Entries whose id isn't in ``seen``, newest-first order preserved."""
    done = set(seen or ())
    return [i for i in items if i["id"] not in done]


def check(callback, seen=()):
    """Fetch in the background; call ``callback(new_items)`` if there are any.

    The callback is only invoked when there is something new to show, so
    a failed or empty fetch is indistinguishable from "nothing to say" —
    which is exactly right for a feature that must never nag.
    """
    def run():
        fresh = unseen(fetch(), seen)
        if fresh:
            try:
                callback(fresh)
            except Exception:
                pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
