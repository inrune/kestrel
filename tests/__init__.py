"""
Kestrel test suite.

Making ``tests`` a package lets it put the reference implementation
(``kestrel-core/``) on the import path no matter where the tests are run
from, so both ``python -m unittest discover`` and ``pytest`` just work
without any environment setup.
"""

import os
import sys

_CORE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "kestrel-core"))
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
