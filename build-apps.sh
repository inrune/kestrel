#!/usr/bin/env bash
#
# Kestrel — release packager.
#
# Produces the three downloadable zips next to the website:
#     kestrel-core.zip    kestrel-miner.zip    kestrel-wallet.zip
# and prints their SHA-256. Attach the three zips to the GitHub Release.
# GitHub works out and publishes the SHA-256 of every file attached to a
# release; the apps' updater and the download page both read it from
# there, so there is no checksum file to keep up to date.
#
# The zips are REPRODUCIBLE. They are built from the committed tree (git
# HEAD), never from whatever else is lying in the folders — so a private
# key, a ledger or a log cannot end up in a release. Nothing about the
# build machine or the commit leaks into the bytes: every file carries
# the same fixed date (the genesis block's), permissions follow from the
# file name (launchers 755, everything else 644), entries are in a fixed
# order, and nothing is compressed (deflate output varies between zlib
# builds; stored bytes do not). The same files therefore give the same
# SHA-256 anywhere — whoever checks out the release, however it was
# committed or uploaded, and nobody has to trust the person who built it.
#
# Usage:
#     ./build-apps.sh                # build from HEAD (refuses a dirty tree)
#     ./build-apps.sh --allow-dirty  # build from HEAD even so (says so)
#     ./build-apps.sh --check        # list what would ship, build nothing
#
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null
  then PY="$c"; break; fi
done
[[ -n "$PY" ]] || { echo "error: needs python 3.8+"; exit 1; }

# The apps each ship a copy of kestrel-core/kestrel/. Refuse to build if
# any copy has drifted from the core.
bash sync-packages.sh --check >/dev/null || {
  bash sync-packages.sh --check
  echo "error: run ./sync-packages.sh first"
  exit 1
}

exec "$PY" - "$@" <<'PYEOF'
import hashlib, io, os, subprocess, sys, tarfile, time, zipfile

FOLDERS = ("kestrel-core", "kestrel-miner", "kestrel-wallet")
args = set(sys.argv[1:])

# Never shipped, even if someone commits one by mistake.
def excluded(path):
    parts = path.split("/")
    name = parts[-1]
    return (
        "__pycache__" in parts or name.endswith((".pyc", ".pyo", ".tmp"))
        or "kestrel-data" in parts or ".kestrel-update" in parts
        or ".venv" in parts or "venv" in parts or ".git" in parts
        or name.startswith("kestrel-wallet.json")          # keys, .bak copies
        or name.endswith("-settings.json") or name == "seeds.txt"
        or name in ("seeds-cache.txt", "dht-nodes.json", "peers.json",
                    "kestrel-address-book.json", ".DS_Store")
        or name.startswith("kestrel-log.txt") or name.endswith(".zip")
        or any(p.startswith("node") and p[4:].isdigit() for p in parts)
    )

def git(*a):
    return subprocess.run(("git",) + a, capture_output=True, check=True).stdout

try:
    in_git = git("rev-parse", "--is-inside-work-tree").strip() == b"true"
except (OSError, subprocess.CalledProcessError):
    in_git = False

files = {}          # folder -> [(path, mode, bytes)]
if in_git:
    dirty = git("status", "--porcelain", "--", *FOLDERS).decode().strip()
    if dirty and "--allow-dirty" not in args and "--check" not in args:
        print("error: uncommitted changes in the app folders — the release "
              "is built from the\ncommitted tree, so these would NOT be in "
              "it:\n" + "\n".join("  " + l for l in dirty.splitlines()))
        print("commit them first, or pass --allow-dirty to build HEAD anyway")
        sys.exit(1)
    if dirty:
        print("note: building from HEAD; uncommitted changes are not included")
    commit = git("rev-parse", "--short=12", "HEAD").decode().strip()
    tar = tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar",
                                              "HEAD", "--", *FOLDERS)))
    for m in tar.getmembers():
        if not m.isfile() or excluded(m.name):
            continue
        top = m.name.split("/", 1)[0]
        files.setdefault(top, []).append(
            (m.name, 0o755 if m.name.endswith(".sh") else 0o644,
             tar.extractfile(m).read()))
else:
    commit = "working tree"
    for top in FOLDERS:
        for base, dirs, names in os.walk(top):
            dirs.sort()
            for n in names:
                p = os.path.join(base, n).replace(os.sep, "/")
                if excluded(p):
                    continue
                mode = 0o755 if n.endswith(".sh") else 0o644
                with open(p, "rb") as f:
                    files.setdefault(top, []).append((p, mode, f.read()))

version = None
for path, _m, data in files.get("kestrel-core", []):
    if path == "kestrel-core/kestrel/__init__.py":
        for line in data.decode().splitlines():
            if line.startswith("__version__"):
                version = line.split("=", 1)[1].strip().strip("\"'")

if "--check" in args:
    for top in FOLDERS:
        print(f"### {top}.zip would contain:")
        for path, mode, data in sorted(files.get(top, [])):
            print(f"    {oct(mode)[2:]}  {len(data):>8,}  {path}")
        print()
    sys.exit(0)

# One fixed date for every file: 2026-07-03 00:00 UTC, the genesis block.
# (Using the commit's time would make the bytes depend on HOW the files
# were committed — a web upload makes a new commit with a new time.)
stamp = time.gmtime(1783036800)[:6]
built = []
for top in FOLDERS:
    entries = sorted(files.get(top, []))
    if not entries:
        print(f"skip: nothing to ship for {top}")
        continue
    out = f"{top}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for path, mode, data in entries:
            zi = zipfile.ZipInfo(path, date_time=stamp)
            zi.create_system = 3                          # unix: keep modes
            zi.external_attr = (0o100000 | mode) << 16
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, data)
    with open(out, "wb") as f:
        f.write(buf.getvalue())
    built.append((out, hashlib.sha256(buf.getvalue()).hexdigest(),
                  len(buf.getvalue()), len(entries)))
    print(f"  built {out:<20} {len(buf.getvalue()) / 1024:>7.0f} KB  "
          f"{len(entries)} files")

print(f"\nKestrel {version} from {commit} — SHA-256:")
for out, digest, _size, _n in built:
    print(f"  {digest}  {out}")
print(f"\nDone. Attach the three zips to the GitHub Release (tag v{version})."
      "\nGitHub shows the same SHA-256 next to each file, and anyone can "
      "rebuild\nthem from that tag with this script and compare.")
PYEOF
