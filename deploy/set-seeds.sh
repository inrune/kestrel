#!/usr/bin/env bash
# =====================================================================
# Kestrel — wire the network's entry points into the apps.
#
# Sets SEED_NODES and/or SEED_LIST_URLS in ALL THREE kestrel/params.py
# files (core, miner, wallet) at once, so every app ships pointing at the
# same network. Run this on your own machine, in your repo, after your
# anchor node is up.
#
#   • --seed  = a node URL baked directly into the app (always works,
#               even if GitHub is down). Repeatable.
#   • --list  = the Raw URL of a seeds.txt you host on GitHub. Apps
#               re-read it every launch, so you can add/remove public
#               nodes later WITHOUT re-shipping. Repeatable.
#
# Example:
#   ./deploy/set-seeds.sh \
#       --seed http://203.0.113.5:4444 \
#       --list https://raw.githubusercontent.com/you/kestrel/main/seeds.txt
#
# Re-running replaces the previous values (idempotent). Pass --show to
# print the current values without changing anything.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

SEEDS=()
LISTS=()
SHOW=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --seed) SEEDS+=("${2:?}"); shift 2;;
    --list) LISTS+=("${2:?}"); shift 2;;
    --show) SHOW=1; shift;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "unknown option: $1"; exit 1;;
  esac
done

FILES=()
for d in kestrel-core kestrel-miner kestrel-wallet; do
  f="$d/kestrel/params.py"
  [[ -f "$f" ]] && FILES+=("$f")
done
[[ ${#FILES[@]} -gt 0 ]] || { echo "no kestrel/params.py found — run me from the repo root"; exit 1; }

SEEDS_JOINED="$(IFS=,; echo "${SEEDS[*]:-}")"
LISTS_JOINED="$(IFS=,; echo "${LISTS[*]:-}")"

SHOW="$SHOW" SEEDS="$SEEDS_JOINED" LISTS="$LISTS_JOINED" \
python3 - "${FILES[@]}" <<'PY'
import os, re, sys

show  = os.environ.get("SHOW") == "1"
seeds = [s for s in os.environ.get("SEEDS","").split(",") if s]
lists = [s for s in os.environ.get("LISTS","").split(",") if s]
files = sys.argv[1:]

def py_list(items):
    inner = ", ".join('"%s"' % i.rstrip("/") for i in items)
    return "[%s]" % inner

def sub_assignment(text, name, replacement):
    # replace: NAME: list[str] = [ ... ]   (single line, as shipped)
    pat = re.compile(r'^(%s\s*:\s*list\[str\]\s*=\s*).*$' % re.escape(name), re.M)
    if not pat.search(text):
        raise SystemExit("!! could not find '%s' assignment — file changed?" % name)
    return pat.sub(lambda m: m.group(1) + replacement, text, count=1)

def current(text, name):
    m = re.search(r'^%s\s*:\s*list\[str\]\s*=\s*(.*)$' % re.escape(name), text, re.M)
    return m.group(1) if m else "<not found>"

if show:
    for f in files:
        t = open(f, encoding="utf-8").read()
        print(f"{f}")
        print(f"    SEED_NODES     = {current(t,'SEED_NODES')}")
        print(f"    SEED_LIST_URLS = {current(t,'SEED_LIST_URLS')}")
    raise SystemExit(0)

if not seeds and not lists:
    raise SystemExit("nothing to do — pass --seed and/or --list (or --show)")

for f in files:
    t = open(f, encoding="utf-8").read()
    if seeds:
        t = sub_assignment(t, "SEED_NODES", py_list(seeds))
    if lists:
        t = sub_assignment(t, "SEED_LIST_URLS", py_list(lists))
    open(f, "w", encoding="utf-8").write(t)
    print(f"  patched {f}")

print()
if seeds: print("SEED_NODES     ->", py_list(seeds))
if lists: print("SEED_LIST_URLS ->", py_list(lists))
PY

echo
echo "Done. Now, from the repo root:"
echo "    ./build-apps.sh          # rebuild the app zips with the new seeds"
echo "    git add -A && git commit -m 'Point apps at the live network' && git push"
