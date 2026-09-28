#!/usr/bin/env bash
# Kestrel Core — the node, on the command line. Its output is the point,
# so unlike the desktop apps this one stays attached to your terminal.
set -u
cd "$(dirname "$0")" || exit 1

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

# a private environment made by an earlier run takes precedence
if [ -x ".venv/bin/python" ] && ".venv/bin/python" -c 'import ecdsa' >/dev/null 2>&1; then
  PY=".venv/bin/python"
fi

if ! "$PY" -c 'import ecdsa' >/dev/null 2>&1; then
  echo "Installing the one dependency (ecdsa)…"
  if "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt >/dev/null 2>&1 \
     || "$PY" -m pip install --quiet --disable-pip-version-check --user -r requirements.txt >/dev/null 2>&1; then
    :
  # "externally managed" Pythons (Debian 12+, Ubuntu 23.04+, Homebrew)
  # refuse pip entirely; a private .venv beside this file does not
  # (--clear replaces one left from an older Python; the marker keeps a
  # .venv someone made themselves out of harm's way)
  elif { [ ! -d .venv ] || [ -f .venv/.made-by-kestrel ]; } && \
       "$PY" -m venv --clear .venv >/dev/null 2>&1 && \
       : > .venv/.made-by-kestrel && \
       ".venv/bin/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt >/dev/null 2>&1; then
    PY=".venv/bin/python"
  else
    if [ -f .venv/.made-by-kestrel ]; then rm -rf .venv; fi
    echo ""
    echo "  Could not install the one package Kestrel needs (ecdsa)."
    if command -v apt-get >/dev/null 2>&1; then
      echo "  The simplest fix:   sudo apt install python3-ecdsa"
    else
      echo "  Install it yourself:   $PY -m pip install --user ecdsa"
    fi
    echo ""
    exit 1
  fi
fi

exec "$PY" -m kestrel.cli start "$@"
