#!/usr/bin/env bash
# Kestrel Core — the node, on the command line. Its output is the point,
# so unlike the desktop apps this one stays attached to your terminal.
set -u
cd "$(dirname "$0")"

PY=""
for c in python3 python python3.13 python3.12 python3.11 python3.10; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null
  then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo ""
  echo "  Kestrel Core needs Python 3.10 or newer."
  case "$(uname -s)" in
    Darwin) echo "      brew install python   # or python.org/downloads" ;;
    Linux)
      if   command -v apt-get >/dev/null 2>&1; then echo "      sudo apt install python3 python3-pip"
      elif command -v dnf     >/dev/null 2>&1; then echo "      sudo dnf install python3 python3-pip"
      elif command -v pacman  >/dev/null 2>&1; then echo "      sudo pacman -S python python-pip"
      fi ;;
  esac
  echo ""
  exit 1
fi

if ! "$PY" -c 'import ecdsa' >/dev/null 2>&1; then
  echo "Installing the one dependency (ecdsa)…"
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || "$PY" -m pip install --quiet --user -r requirements.txt \
    || { echo "Could not install dependencies."; exit 1; }
fi

exec "$PY" -m kestrel.cli start "$@"
