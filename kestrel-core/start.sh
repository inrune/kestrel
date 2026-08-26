#!/usr/bin/env bash
# Kestrel — one-command launcher (node + dashboard + explorer + wallet).
set -e
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
python3 -m kestrel.cli start "$@"
