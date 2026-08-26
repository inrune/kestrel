"""
Tell the user when a newer Kestrel is available.

Deliberately quiet and optional: it asks GitHub once, in the background,
and if anything at all goes wrong it says nothing rather than nagging.
Nothing is ever downloaded or installed automatically — the user is only
shown that a newer version exists and where to get it.
"""

import json
import re
import threading
import urllib.request

from . import __version__ as CURRENT

RELEASES_API = "https://api.github.com/repos/inrune/kestrel/releases/latest"
RELEASES_PAGE = "https://github.com/inrune/kestrel/releases"
TIMEOUT = 8


def _parse(v: str) -> tuple:
    """'v1.4.5' -> (1, 4, 5). Unparseable pieces become 0."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest: str, current: str = CURRENT) -> bool:
    a, b = _parse(latest), _parse(current)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def fetch_latest() -> str | None:
    """The newest published version string, or None if we can't tell."""
    try:
        req = urllib.request.Request(
            RELEASES_API,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"kestrel/{CURRENT}"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode())
        tag = (data.get("tag_name") or data.get("name") or "").strip()
        return tag or None
    except Exception:
        return None      # offline, rate-limited, no releases yet — stay quiet


def check(callback):
    """Run the check in the background.

    ``callback(latest_version, url)`` is called only when there really is
    a newer version. It is never called on failure, so a flaky connection
    can't produce a misleading 'you're up to date' either way.
    """
    def run():
        latest = fetch_latest()
        if latest and is_newer(latest):
            try:
                callback(latest, RELEASES_PAGE)
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()
