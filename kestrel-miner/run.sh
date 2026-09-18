#!/usr/bin/env bash
#
# Kestrel Miner launcher.
#
# Finds a Python 3.10+, says exactly how to get one if there isn't one,
# installs the single dependency, and starts the app detached from this
# terminal — so closing the terminal doesn't take the app with it, and
# nothing the app prints lands in your shell. Everything worth reading
# goes to kestrel-log.txt next to this file.

set -u
cd "$(dirname "$0")"

APP="Kestrel Miner"

say()  { printf '%s\n' "$*"; }
die()  { printf '\n  %s\n\n' "$*" >&2; exit 1; }

# ---- 1. find a Python that is new enough ----------------------------
PY=""
for c in python3 python python3.13 python3.12 python3.11 python3.10; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null
  then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  say ""
  say "  $APP needs Python 3.10 or newer, and I can't find one."
  say ""
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        say "  You have Homebrew, so this will do it:"
        say ""
        say "      brew install python-tk"
        say ""
        printf '  Run that now? [y/N] '
        read -r a || a=""
        case "$a" in
          [yY]*) brew install python-tk && exec "$0" "$@" ;;
        esac
      else
        say "  I can download the official installer from python.org and"
        say "  open it for you. It asks for your password — macOS installers"
        say "  do, and you should be the one typing it."
        say ""
        printf '  Download it now? [Y/n] '
        read -r a || a=""
        case "$a" in
          [nN]*) ;;
          *)
            pkg="$TMPDIR/python-kestrel.pkg"
            for v in 3.12.7 3.11.9 3.10.11; do
              url="https://www.python.org/ftp/python/$v/python-$v-macos11.pkg"
              say "  Downloading python-$v ..."
              if curl -fSL --connect-timeout 20 -o "$pkg" "$url" 2>/dev/null; then
                say ""
                say "  Opening the installer. Run this file again when it's done."
                open "$pkg"
                exit 0
              fi
            done
            say "  Could not download it. Check your internet connection."
            ;;
        esac
        say ""
        say "  Or install it yourself from"
        say "  https://www.python.org/downloads/macos/"
      fi
      ;;
    Linux)
      if   command -v apt-get >/dev/null 2>&1; then
        say "  On Debian/Ubuntu:    sudo apt install python3 python3-tk python3-pip"
      elif command -v dnf >/dev/null 2>&1; then
        say "  On Fedora:           sudo dnf install python3 python3-tkinter python3-pip"
      elif command -v pacman >/dev/null 2>&1; then
        say "  On Arch:             sudo pacman -S python tk python-pip"
      else
        say "  Install python3 (3.10+), its tkinter package, and pip."
      fi
      say ""
      say "  Kestrel won't install system packages for you — that needs"
      say "  your password, and you should be the one typing it."
      ;;
    *)
      say "  Install Python 3.10+ from https://www.python.org/downloads/"
      ;;
  esac
  say ""
  exit 1
fi

# ---- 2. tkinter, which slim builds and some distros leave out --------
if ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
  say ""
  say "  This Python has no tkinter, which $APP needs for its window."
  case "$(uname -s)" in
    Darwin) say "      brew install python-tk" ;;
    Linux)
      if   command -v apt-get >/dev/null 2>&1; then say "      sudo apt install python3-tk"
      elif command -v dnf     >/dev/null 2>&1; then say "      sudo dnf install python3-tkinter"
      elif command -v pacman  >/dev/null 2>&1; then say "      sudo pacman -S tk"
      fi ;;
  esac
  say ""
  exit 1
fi

# ---- 3. the one dependency ------------------------------------------
if ! "$PY" -c 'import ecdsa' >/dev/null 2>&1; then
  say "Installing the one dependency (ecdsa)…"
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || "$PY" -m pip install --quiet --user -r requirements.txt \
    || die "Could not install dependencies. Check your internet connection."
fi

# ---- 4. start it, off this terminal ---------------------------------
if [ -t 1 ]; then
  nohup "$PY" app.py >/dev/null 2>&1 &
  disown 2>/dev/null || true
  say "$APP started. Diagnostics: $(pwd)/kestrel-log.txt"
else
  exec "$PY" app.py
fi
