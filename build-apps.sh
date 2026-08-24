#!/usr/bin/env bash
#
# Kestrel — release packager.
#
# Produces the three downloadable zips next to the website:
#     kestrel-core.zip    kestrel-miner.zip    kestrel-wallet.zip
#
# Each zip is a self-contained copy of one folder, with local junk left
# out: Python caches, private keys, local chain/peer state, settings,
# editor/OS clutter and any previously-built zips. What ships is exactly
# what a new user should download — code, README, LICENSE, launch scripts
# and (for core) the whitepaper.
#
# Usage:
#     ./build-apps.sh            # build all three zips
#     ./build-apps.sh --check    # list what WOULD ship, build nothing
#
set -euo pipefail
cd "$(dirname "$0")"

FOLDERS=(kestrel-core kestrel-miner kestrel-wallet)

# Paths excluded from every zip (never ship these publicly).
EXCLUDES=(
  '*/__pycache__/*' '*__pycache__*' '*.pyc' '*.pyo'
  '*/kestrel-data/*' '*kestrel-data*'      # local ledger + peer book
  '*/node[0-9]*/*'                          # stray multi-node test dirs
  'kestrel-wallet.json'                     # private keys — never publish
  '*/kestrel-wallet.json'
  '*-settings.json' '*/*-settings.json'     # miner/wallet local settings
  'seeds.txt' '*/seeds.txt'                 # operator's live seed list
  '*seeds-cache.txt' '*dht-nodes.json'      # discovery caches
  '.DS_Store' '*/.DS_Store' '*/.git/*' '*.zip'
)

zip_excludes=()
for e in "${EXCLUDES[@]}"; do zip_excludes+=(-x "$e"); done

if [[ "${1:-}" == "--check" ]]; then
  for f in "${FOLDERS[@]}"; do
    echo "### $f.zip would contain:"
    ( cd "$f" && find . -type f \
        ! -path '*/__pycache__/*' ! -name '*.pyc' \
        ! -path '*/kestrel-data/*' ! -name 'kestrel-wallet.json' \
        ! -name '*-settings.json' ! -name 'seeds.txt' \
        ! -name '*seeds-cache.txt' ! -name 'dht-nodes.json' \
        ! -name '.DS_Store' | sort | sed 's/^/    /' )
    echo
  done
  exit 0
fi

command -v zip >/dev/null 2>&1 || {
  echo "error: 'zip' is not installed."
  echo "  Debian/Ubuntu:  sudo apt-get install -y zip"
  echo "  macOS:          zip ships with the system"
  exit 1
}

for f in "${FOLDERS[@]}"; do
  [[ -d "$f" ]] || { echo "skip: $f/ not found"; continue; }
  out="$f.zip"
  rm -f "$out"
  # zip the folder itself (so it extracts into "$f/"), minus the excludes
  zip -r -q "$out" "$f" "${zip_excludes[@]}"
  printf '  built %-20s %s\n' "$out" "$(du -h "$out" | cut -f1)"
done

echo
echo "Done. Upload these three zips wherever your download links point"
echo "(GitHub Releases, or next to kestrel-website.html on your host)."
