"""
Kestrel updates — find a newer release, and install it if the user says so.

Nothing here happens on its own. The check is quiet and optional; the
download only starts after someone clicks a button; and the swap only
runs after the download has been checked — against the SHA-256 GitHub
publishes for every release file, and in every case against the zip's
own contents and version.

What the update is allowed to touch is deliberately narrow. Code and docs
are replaced. The wallet file, the address book, settings and the local
ledger are never read, never copied and never overwritten — see KEEP.
The previous version is kept next to the app until the new one has
started, so a failed update can be put back.

The honest summary of the trust model: you are already running code from
this repository, and an update runs more of it. A published checksum
protects the download from being tampered with in transit or served
corrupt; it does not protect you from the repository itself, and it is
optional. Nothing signs releases yet.
That is why the default is to ask first, why the dialog says what it is
about to do, and why "never update automatically" is one click away.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile

from . import __version__ as CURRENT

REPO = "inrune/kestrel"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
# every release, newest first — the only way to see pre-releases, which
# /releases/latest leaves out by design
RELEASES_LIST = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
TIMEOUT = 12
MAX_DOWNLOAD = 200 * 1024 * 1024        # a Kestrel app is a few MB; cap it

# Files and folders an update must never touch. Everything here either IS
# the user's money or is local state that a new version can rebuild.
KEEP = {
    "kestrel-wallet.json",              # the private key. The money.
    "kestrel-address-book.json",
    "wallet-settings.json",
    "miner-settings.json",
    "seeds.txt",
    "kestrel-data",                     # ledger, peers, discovery caches
    ".kestrel-update",                  # our own staging area
    "kestrel-log.txt",                  # this machine's diagnostics
    "kestrel-log.txt.1",
    ".venv", "venv",                    # a launcher-made Python environment
}

# Anything whose name starts like this is key material too — the wallet's
# own temp file, and the copies it sets aside before it will ever replace
# a wallet ("kestrel-wallet.json.replaced-<time>.bak"). None of it is
# read, copied or backed up by an update.
KEEP_PREFIXES = ("kestrel-wallet",)

# What a valid extracted app must contain before we are willing to swap.
REQUIRED = ("app.py", os.path.join("kestrel", "__init__.py"))


# --------------------------------------------------------------- versions

_PRE_RANK = {"a": 0, "alpha": 0, "dev": 0, "b": 1, "beta": 1, "pre": 1,
             "preview": 1, "c": 2, "rc": 2}


def _parse(v: str) -> tuple:
    """A version as something that sorts the way people expect.

    'v1.4.5' -> (1, 4, 5, 0, (3, 0)). A pre-release sorts BEFORE the
    release it leads up to and after the one before it, so
    1.4.9 < 1.5.0-alpha < 1.5.0-beta.1 < 1.5.0-beta.2 < 1.5.0-rc1 < 1.5.0.
    Accepts "1.5.0-beta.1", "1.5.0b1", "v1.5.0 beta 1" and the like.
    Nothing parseable gives (0,), which is never newer than anything.
    """
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+){0,3})(.*)$", v or "")
    if not m:
        return (0,)
    nums = [int(n) for n in m.group(1).split(".")]
    nums += [0] * (4 - len(nums))
    rest = m.group(2).split("+", 1)[0].strip(" -_.").lower()
    if not rest:
        pre = (3, 0)                                   # a final release
    else:
        word = re.match(r"[a-z]*", rest).group(0)
        num = re.search(r"\d+", rest)
        pre = (_PRE_RANK.get(word, 0), int(num.group(0)) if num else 0)
    return tuple(nums) + (pre,)


def is_prerelease(v: str) -> bool:
    key = _parse(v)
    return len(key) > 1 and key[-1][0] < 3


def is_newer(latest: str, current: str = CURRENT) -> bool:
    return _parse(latest) > _parse(current)


def label(rel: dict) -> str:
    """How to name a release to a person: '1.5.0-beta.1 (beta)'."""
    v = str(rel.get("version", "")).lstrip("vV")
    return v + (" (beta)" if rel.get("prerelease") else "")


# ------------------------------------------------------------- the check

def _get(url: str, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": f"kestrel/{CURRENT}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _release(data) -> dict | None:
    """One release from GitHub's API, in the shape the apps use."""
    if not isinstance(data, dict) or data.get("draft"):
        return None
    tag = (data.get("tag_name") or data.get("name") or "").strip()
    if not tag:
        return None
    assets = [{"name": a.get("name", ""),
               "url": a.get("browser_download_url", ""),
               "size": int(a.get("size") or 0),
               "sha256": asset_sha256(a)}
              for a in (data.get("assets") or [])
              if isinstance(a, dict) and a.get("browser_download_url")]
    return {
        "version": tag,
        "prerelease": bool(data.get("prerelease")) or is_prerelease(tag),
        "notes": (data.get("body") or "").strip(),
        "url": data.get("html_url") or RELEASES_PAGE,
        "assets": assets,
    }


def fetch_latest(beta: bool = False) -> dict | None:
    """The newest published release, or None if we can't tell.

    With `beta`, pre-releases count too: whichever is newer, the newest
    beta or the newest full release, wins — so someone on a beta moves on
    to the final version when it comes out. Without it, only full
    releases are ever offered.

    Returns {version, prerelease, notes, url, assets: [{name, url, size,
    sha256}]}. Never raises: offline, rate-limited or no releases at all
    all mean "say nothing", which is better than nagging about a failure
    the person can do nothing about.
    """
    try:
        if not beta:
            return _release(json.loads(_get(RELEASES_API).decode()))
        listed = json.loads(_get(RELEASES_LIST).decode())
        best = None
        for data in listed if isinstance(listed, list) else []:
            rel = _release(data)
            if rel and (best is None
                        or _parse(rel["version"]) > _parse(best["version"])):
                best = rel
        return best
    except Exception:
        return None


def asset_sha256(asset: dict) -> str | None:
    """The SHA-256 GitHub computed for an uploaded release file.

    Since June 2025 GitHub publishes one for every release asset, as
    "digest": "sha256:<hex>". It is worked out by GitHub from the file it
    actually stores, so nobody has to maintain a checksum file by hand.
    """
    d = asset.get("digest") if isinstance(asset, dict) else None
    if isinstance(d, str) and d.lower().startswith("sha256:"):
        h = d.split(":", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", h):
            return h
    return None


def check(callback, beta: bool = False):
    """Run the check in the background.

    ``callback(release)`` is called only when there really is a newer
    version. It is never called on failure, so a flaky connection can't
    produce a misleading 'you're up to date' either way.
    """
    def run():
        rel = fetch_latest(beta)
        if rel and is_newer(rel["version"]):
            try:
                callback(rel)
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


def summarize(notes: str, limit: int = 8) -> list[str]:
    """Release notes -> a few plain bullet lines for the dialog."""
    out = []
    for raw in (notes or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "---", "<!--", "|")):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)   # [text](url)
        line = re.sub(r"[`*_]", "", line)
        line = re.sub(r"https?://\S+", "", line).strip()
        if len(line) < 3:
            continue
        out.append(line if len(line) <= 120 else line[:117] + "…")
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------- downloads

def pick_asset(assets: list[dict], app: str) -> dict | None:
    """The zip belonging to this app ('kestrel-miner' / 'kestrel-wallet')."""
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    for a in zips:
        if a["name"].lower().startswith(app.lower()):
            return a
    for a in zips:
        if app.lower() in a["name"].lower():
            return a
    return None


def published_checksums(assets: list[dict]) -> dict:
    """{filename: sha256} from a SHA256SUMS file published with the release."""
    for a in assets:
        if a["name"].lower() in ("sha256sums.txt", "sha256sums",
                                 "checksums.txt"):
            try:
                text = _get(a["url"], timeout=TIMEOUT).decode("utf-8", "replace")
            except Exception:
                return {}
            sums = {}
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}",
                                                    parts[0]):
                    sums[os.path.basename(parts[-1].lstrip("*"))] = \
                        parts[0].lower()
            return sums
    return {}


def download(url: str, dest: str, *, expect_sha256: str = None,
             max_bytes: int = MAX_DOWNLOAD, on_progress=None,
             cancel: threading.Event = None) -> str:
    """Download to `dest`, checking the size cap and the checksum.

    Returns the sha256 it actually got. Raises on any mismatch — a wrong
    checksum means the file is not what the release says it is, and the
    only safe thing to do with it is delete it.
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": f"kestrel/{CURRENT}",
                      "Accept": "application/octet-stream"})
    h = hashlib.sha256()
    got = 0
    tmp = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            if total and total > max_bytes:
                raise ValueError(f"download too large ({total:,} bytes)")
            with open(tmp, "wb") as f:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("cancelled")
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > max_bytes:
                        raise ValueError("download exceeded the size limit")
                    h.update(chunk)
                    f.write(chunk)
                    if on_progress:
                        on_progress(got, total)
        digest = h.hexdigest()
        if expect_sha256 and digest != expect_sha256.lower():
            raise ValueError(
                "checksum does not match the one published with the "
                "release — the download was corrupted or tampered with")
        os.replace(tmp, dest)
        return digest
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------- staging

def _safe_extract(zf: zipfile.ZipFile, into: str):
    """Extract, refusing any entry that would escape `into`.

    A zip can name its entries '../../somewhere/else'. Python's extractall
    has guarded against that since 3.6.2, but this is the step that
    overwrites an application directory, so it checks for itself.
    """
    root = os.path.realpath(into)
    for member in zf.infolist():
        name = member.filename
        if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
            raise ValueError(f"unsafe path in the update archive: {name}")
        target = os.path.realpath(os.path.join(into, name))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError(f"unsafe path in the update archive: {name}")
    zf.extractall(into)
    _restore_modes(zf, into)


def _restore_modes(zf: zipfile.ZipFile, into: str):
    """Put back the permission bits the archive recorded.

    zipfile.extractall ignores them, so every file comes out as plain
    read/write — which on macOS and Linux turned run.sh into a file the
    launcher could no longer run, straight after an update that otherwise
    worked. Only the ordinary rwx bits are restored, never setuid and
    friends; and whatever the archive says, a .sh launcher is executable.
    """
    if os.name == "nt":
        return
    for member in zf.infolist():
        if member.is_dir():
            continue
        path = os.path.join(into, member.filename)
        mode = (member.external_attr >> 16) & 0o777
        if member.filename.endswith(".sh"):
            mode |= 0o755
        if mode:
            try:
                os.chmod(path, mode | 0o600)
            except OSError:
                pass


def _find_app_root(base: str) -> str | None:
    """The folder inside the extract that actually holds the app."""
    candidates = [base] + [os.path.join(base, d) for d in
                           sorted(os.listdir(base))
                           if os.path.isdir(os.path.join(base, d))]
    for c in candidates:
        if all(os.path.exists(os.path.join(c, r)) for r in REQUIRED):
            return c
    return None


def stage(zip_path: str, staging: str, *, expect_version: str = None) -> str:
    """Unpack the download and check it really is a Kestrel app.

    Returns the folder to install from. Raises if the archive is not what
    it claims to be — better to refuse than to half-replace a working
    installation with something unknown.
    """
    extract = os.path.join(staging, "extract")
    shutil.rmtree(extract, ignore_errors=True)
    os.makedirs(extract, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise ValueError(f"the downloaded archive is damaged ({bad})")
        _safe_extract(zf, extract)

    root = _find_app_root(extract)
    if not root:
        raise ValueError("the download does not look like a Kestrel app "
                         "(no app.py and kestrel/ inside)")

    init = os.path.join(root, "kestrel", "__init__.py")
    try:
        with open(init, encoding="utf-8") as f:
            found = re.search(r'__version__\s*=\s*["\']([^"\']+)', f.read())
        shipped = found.group(1) if found else None
    except Exception:
        shipped = None
    if shipped and not is_newer(shipped, CURRENT):
        raise ValueError(f"the download is version {shipped}, which is not "
                         f"newer than the {CURRENT} already installed")
    if expect_version and shipped and _parse(shipped) != _parse(expect_version):
        raise ValueError(f"the download says version {shipped} but the "
                         f"release says {expect_version}")
    return root


# -------------------------------------------------------------- applying

# Run as its own process after the app exits. It cannot import kestrel —
# the package it would import is the one being replaced — so it is written
# out whole and stands alone.
APPLY_SCRIPT = r'''
"""Swap a staged Kestrel update into place, then restart the app.

Started by the app just before it quits. Waits for that process to go,
copies the new files in (never touching the wallet, settings or ledger),
and relaunches. If anything fails the old files are put back, so the
worst case is the version you already had.
"""
import json, os, shutil, subprocess, sys, time

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
APP, NEW, BACKUP = cfg["app_dir"], cfg["staged"], cfg["backup"]
KEEP, PID, RELAUNCH = set(cfg["keep"]), cfg["pid"], cfg["relaunch"]
KEEP_PREFIXES = tuple(cfg.get("keep_prefixes") or ("kestrel-wallet",))
LOG = os.path.join(os.path.dirname(BACKUP), "update.log")


def say(msg):
    line = time.strftime("%H:%M:%S ") + msg
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def running(pid):
    """Is the app still alive? A process that has exited but not yet been
    reaped still answers signal 0, so ask for its state too — otherwise we
    wait out the whole timeout on an app that quit immediately."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True).stdout
            return str(pid) in out
        os.kill(pid, 0)
    except Exception:
        return False
    try:                                    # Linux
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        pass
    try:                                    # macOS / BSD
        st = subprocess.run(["ps", "-o", "state=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=5).stdout
        return not st.strip().startswith("Z")
    except Exception:
        return True


def kept(name):
    return name in KEEP or name.startswith(KEEP_PREFIXES)


def tree(root):
    """Every relative path under root, minus the keep-list."""
    found = []
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root)
        parts = [] if rel_base == "." else rel_base.split(os.sep)
        if parts and kept(parts[0]):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs
                   if not (not parts and kept(d)) and d != "__pycache__"]
        for f in files:
            rel = f if not parts else os.path.join(*parts, f)
            if kept(rel.split(os.sep)[0]) or f.endswith((".pyc", ".pyo")):
                continue
            found.append(rel)
    return found


def shipped_code(rel):
    """Is this a file an older release shipped, that is safe to delete if
    the new release no longer has it? Only the package's own Python.

    Anything else in the folder may be the person's: notes, exports, a
    backup key saved as a .txt. The old rule deleted every .txt, .md, .py,
    .bat and .sh the new version didn't ship — which included exactly
    those. An unused top-level file left behind costs nothing.
    """
    parts = rel.split(os.sep)
    return len(parts) >= 2 and parts[0] == "kestrel" and rel.endswith(".py")


def restart():
    try:
        kw = {"cwd": APP, "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(RELAUNCH, **kw)
        say("restarted")
    except Exception as e:
        say(f"could not restart automatically ({e}) — start Kestrel yourself")


def give_up(msg):
    """Leave the installed version as it is — and start it again, since
    the person asked for an update, not for their app to vanish."""
    say(msg)
    restart()
    sys.exit(1)


WAIT = float(cfg.get("wait_seconds") or 60)
say(f"waiting for Kestrel (pid {PID}) to exit")
for _ in range(max(int(WAIT * 2), 1)):
    if not running(PID):
        break
    time.sleep(0.5)
else:
    say("it is still running — leaving everything alone")
    sys.exit(1)
time.sleep(1.0)                            # let file handles close

old = tree(APP)
new = tree(NEW)
say(f"{len(old)} existing file(s), {len(new)} in the update")

# Refuse to go anywhere near a working installation with an update that
# isn't one. An empty or half-extracted staging folder would otherwise
# delete the app and put nothing back in its place.
missing = [r for r in cfg["required"] if not os.path.exists(os.path.join(NEW, r))]
if not new or missing:
    give_up(f"the staged update is not a complete app (missing {missing or 'everything'})"
            " — leaving the installed version alone")

new_set = set(new)
stale = [r for r in old if r not in new_set and shipped_code(r)]
# Only what the update will overwrite or remove is backed up: that is all a
# rollback needs, and it keeps the person's own files out of the copy.
affected = [r for r in old if r in new_set] + stale

# 1. keep a copy of what we are about to replace
shutil.rmtree(BACKUP, ignore_errors=True)
os.makedirs(BACKUP, exist_ok=True)
try:
    for rel in affected:
        dst = os.path.join(BACKUP, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(APP, rel), dst)
except Exception as e:
    give_up(f"could not back up ({e}) — refusing to update")

# 2. copy the new files in, then remove code the new version dropped
try:
    for rel in new:
        dst = os.path.join(APP, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(NEW, rel), dst)
        if os.name != "nt" and rel.endswith(".sh"):
            # a launcher that has lost its execute bit is a launcher
            # that no longer launches
            os.chmod(dst, os.stat(dst).st_mode | 0o755)
    short = [r for r in cfg["required"] if not os.path.exists(os.path.join(APP, r))]
    if short:
        raise RuntimeError(f"{short} did not make it across")
    # Drop compiled bytecode from the version we just replaced. Python
    # decides a .pyc is still good by comparing the source's size and
    # modification time against what the .pyc recorded — and a file copied
    # in the same second whose size did not change matches on both. The
    # new source is then never compiled, the old code keeps running, and
    # the update looks like it installed while doing nothing at all.
    for base, dirs, _files in os.walk(APP):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
                dirs.remove(d)
    for rel in stale:
        try:
            os.remove(os.path.join(APP, rel))
        except OSError:
            pass
    say("update applied")
except Exception as e:
    say(f"update failed ({e}) — putting the old files back")
    for rel in affected:
        src = os.path.join(BACKUP, rel)
        if os.path.exists(src):
            dst = os.path.join(APP, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass
    # and take out what the half-applied update added, so the old code
    # does not start up next to modules from a version it never knew
    old_set = set(old)
    for rel in new:
        if rel not in old_set and shipped_code(rel):
            try:
                os.remove(os.path.join(APP, rel))
            except OSError:
                pass
    say("rolled back")

# 3. start the new version (or the old one, if it was rolled back)
restart()
'''


def app_dir_of(module_file: str) -> str:
    return os.path.dirname(os.path.abspath(module_file))


def relaunch_command(app_file: str) -> list:
    """How to start this app again after the swap."""
    exe = sys.executable or "python3"
    if getattr(sys, "frozen", False):       # packaged build: run the exe
        return [sys.executable]
    return [exe, os.path.abspath(app_file)]


def can_install(app_dir: str) -> tuple[bool, str]:
    """Is this installation one we can actually replace in place?"""
    if getattr(sys, "frozen", False):
        return False, ("this build is packaged as a single executable — "
                       "download the new version from the releases page")
    if not os.access(app_dir, os.W_OK):
        return False, ("this folder is read-only for you, so the update "
                       "cannot be written — download it yourself, or move "
                       "Kestrel somewhere you can write to")
    probe = os.path.join(app_dir, ".kestrel-write-test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        return False, f"this folder cannot be written to ({e})"
    return True, ""


def apply_update(app_dir: str, staged: str, relaunch: list) -> subprocess.Popen:
    """Hand the swap to a separate process and return it.

    The caller quits immediately afterwards: the files being replaced are
    the ones it is running from, so it must not be running when they are.
    """
    staging = os.path.join(app_dir, ".kestrel-update")
    os.makedirs(staging, exist_ok=True)
    script = os.path.join(staging, "apply_update.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(APPLY_SCRIPT)
    cfg_path = os.path.join(staging, "apply.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"app_dir": app_dir, "staged": staged,
                   "backup": os.path.join(staging, "previous-version"),
                   "keep": sorted(KEEP), "keep_prefixes": list(KEEP_PREFIXES),
                   "required": list(REQUIRED),
                   "pid": os.getpid(), "relaunch": relaunch,
                   "wait_seconds": 60}, f)
    kw = {"cwd": staging, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    return subprocess.Popen([sys.executable or "python3", script, cfg_path],
                            **kw)


def install(release: dict, app: str, app_dir: str, app_file: str, *,
            on_progress=None, on_step=None,
            cancel: threading.Event = None) -> list:
    """Download, verify, stage and schedule the swap.

    Returns the relaunch command. The caller shuts the app down straight
    after — everything that can fail has already failed by then.
    """
    def step(msg):
        if on_step:
            on_step(msg)

    ok, why = can_install(app_dir)
    if not ok:
        raise RuntimeError(why)

    asset = pick_asset(release.get("assets") or [], app)
    if not asset:
        raise RuntimeError(
            f"this release has no {app} download attached to it — "
            f"get it from the releases page instead")

    staging = os.path.join(app_dir, ".kestrel-update")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    # The download is checked against the SHA-256 GitHub publishes for the
    # file, or failing that a SHA256SUMS file if a release carries one,
    # and it still has to be a well-formed Kestrel zip of the expected
    # version. GitHub publishes a digest for every release file, so one
    # with neither is not something to install blind.
    expect = asset.get("sha256")
    if not expect:
        expect = published_checksums(release.get("assets") or []).get(
            asset["name"])
    if not expect:
        raise ValueError(
            "GitHub did not list a checksum for this download, so it "
            "can't be verified. Try again later, or download it from the "
            "releases page yourself.")
    step("Checking the published checksum…")

    step(f"Downloading {asset['name']}…")
    zip_path = os.path.join(staging, asset["name"])
    download(asset["url"], zip_path, expect_sha256=expect,
             on_progress=on_progress, cancel=cancel)

    step("Checking the download…")
    staged = stage(zip_path, staging, expect_version=release.get("version"))

    step("Installing…")
    relaunch = relaunch_command(app_file)
    apply_update(app_dir, staged, relaunch)
    return relaunch


def after_update(app_dir: str) -> list:
    """Repair what older updaters got wrong. Run at every start; cheap.

    An update is applied by the script of the version being *replaced*,
    so the fixes in this file only protect the update after this one.
    Coming from 1.4.8, the old script has already run, and it:

      * deleted every .txt, .md, .py, .bat and .sh in the app folder that
        the new release didn't ship — people's own notes and exports, and
        possibly a backup key saved as a text file. It did copy them into
        .kestrel-update/previous-version first, and that copy is erased
        by the NEXT update, so this is the moment to put them back;
      * extracted the new release without its permission bits, so on
        macOS and Linux run.sh could no longer be run.

    Returns the relative paths put back (empty almost always).
    """
    restored = []
    if os.name != "nt":
        for name in os.listdir(app_dir) if os.path.isdir(app_dir) else []:
            if name.endswith(".sh"):
                path = os.path.join(app_dir, name)
                try:
                    mode = os.stat(path).st_mode
                    if not mode & 0o100:
                        os.chmod(path, mode | 0o755)
                except OSError:
                    pass

    staging = os.path.join(app_dir, ".kestrel-update")
    prev = os.path.join(staging, "previous-version")
    marker = os.path.join(staging, "recovered")
    if not os.path.isdir(prev):
        return restored
    try:
        with open(marker, encoding="utf-8") as f:
            if f.read().strip() == CURRENT:
                return restored        # already done for this version
    except OSError:
        pass
    for base, dirs, files in os.walk(prev):
        rel_base = os.path.relpath(base, prev)
        parts = [] if rel_base == "." else rel_base.split(os.sep)
        # the package itself is code: anything missing there was removed
        # on purpose. Everything else was the person's.
        dirs[:] = [d for d in dirs if d != "__pycache__"
                   and not (not parts and (d == "kestrel" or d in KEEP))]
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            rel = os.path.join(*parts, f) if parts else f
            # No name-based skipping here: 1.4.8 had no prefix rule, so a
            # "kestrel-wallet-backup.txt" is exactly the kind of file it
            # deleted. Only missing paths are filled, so nothing that is
            # there now — a wallet above all — is ever overwritten.
            dst = os.path.join(app_dir, rel)
            if os.path.exists(dst):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(base, f), dst)
                restored.append(rel)
            except OSError:
                pass
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(CURRENT)
    except OSError:
        pass
    return restored


def cleanup(app_dir: str):
    """Remove staging left behind by a previous update. Best effort."""
    staging = os.path.join(app_dir, ".kestrel-update")
    for name in ("extract", "apply.json", "apply_update.py"):
        p = os.path.join(staging, name)
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass
    try:
        for f in os.listdir(staging):
            if f.endswith((".zip", ".part")):
                os.remove(os.path.join(staging, f))
    except OSError:
        pass
