#!/usr/bin/env bash
# Run the Kestrel test suite.
#
#   ./run-tests.sh            # all tests
#   ./run-tests.sh -q         # quieter
#   ./run-tests.sh fast       # skip the slower mining/networking tests
#
# Needs Python 3.10+ and the one dependency (ecdsa); installs it if missing.
set -euo pipefail
cd "$(dirname "$0")"

python3 -c "import ecdsa" 2>/dev/null || \
  python3 -m pip install --quiet -r kestrel-core/requirements.txt

export PYTHONPATH="$PWD/kestrel-core${PYTHONPATH:+:$PYTHONPATH}"

if [[ "${1:-}" == "fast" ]]; then
  echo "Running fast tests (crypto + primitives only)…"
  exec python3 -m unittest tests.test_crypto tests.test_primitives -v
fi

VERBOSE="-v"; [[ "${1:-}" == "-q" ]] && VERBOSE=""
exec python3 -m unittest discover -s tests -p "test_*.py" $VERBOSE
