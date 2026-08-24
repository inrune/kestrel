"""
Kestrel (KSL) — consensus parameters.

Every node on the network must agree on every value in this file.
Changing any of them creates a different, incompatible chain.
"""

# ---------------------------------------------------------------- units
COIN = 100_000_000                 # feathers per KSL (1 KSL = 1e8 feathers)
UNIT_NAME = "KSL"
SUBUNIT_NAME = "feather"

# ------------------------------------------------------- monetary policy
MAX_SUPPLY = 44_000_000 * COIN     # hard cap: 44,000,000 KSL
INITIAL_REWARD = 25 * COIN         # 25 KSL per block at launch
HALVING_INTERVAL = 880_000         # blocks between halvings (~3.35 years)
# Geometric series check: 2 * 25 * 880,000 = 44,000,000  ✓

# ------------------------------------------------------------- consensus
TARGET_BLOCK_TIME = 120            # seconds (2-minute blocks)
RETARGET_INTERVAL = 2016           # difficulty adjustment window (~2.8 days)
MAX_TARGET = (1 << 244) - 1        # easiest allowed target (12 leading zero bits)
MAX_FUTURE_DRIFT = 2 * 60 * 60     # reject blocks >2h in the future
MEDIAN_TIME_SPAN = 11              # blocks used for median-time-past
COINBASE_MATURITY = 10             # confirmations before coinbase is spendable
MAX_BLOCK_SIZE = 1_000_000         # bytes, serialized

# scrypt proof-of-work parameters (Litecoin-compatible cost settings)
SCRYPT_N = 1024
SCRYPT_R = 1
SCRYPT_P = 1

# ---------------------------------------------------------------- policy
MIN_RELAY_FEE = 1_000              # feathers (0.00001 KSL) minimum tx fee

# ------------------------------------------------------------- addresses
ADDRESS_VERSION = 0x2D             # base58check version byte -> addresses start with 'K'
WIF_VERSION = 0xAD                 # ADDRESS_VERSION | 0x80, for exported private keys

# ------------------------------------------------------------ networking
DEFAULT_PORT = 4444
DISCOVERY_PORT = 4544              # UDP LAN auto-discovery beacon port
PROTOCOL_VERSION = 2
NETWORK_MAGIC = "kestrel-main-v1"

# ---------------------------------------------------------------- genesis
GENESIS_TIMESTAMP = 1783036800   # 2026-07-03 00:00:00 UTC
GENESIS_MESSAGE = (
    "Kestrel genesis / 03 Jul 2026 / "
    "Fast, light, decentralized money for everyone"
)
GENESIS_NONCE = 6624
# genesis block_id: c8f460e1f38bd483ced56c037400108032a28e33746da71efeddf698735036f1
# genesis pow_hash: 00030b06ee364e0824c9e958fc144930ca389235d9c875f7d070982477022281
GENESIS_TARGET = MAX_TARGET

# Public entry points to the network. Every node and app auto-connects to
# these on launch, then discovers the rest of the mesh through peer
# exchange — exactly how Bitcoin's hardcoded seeds worked. Nodes on the
# same Wi-Fi/LAN additionally find each other automatically via UDP
# broadcast, with zero configuration.
#
# Seeds are merged from three places (any of them works):
#   1. this list                  e.g. ["http://seed1.example.com:4444"]
#   2. env var KESTREL_SEEDS      e.g. "http://1.2.3.4:4444,http://5.6.7.8:4444"
#   3. a seeds.txt file next to the app (one URL per line, # comments ok)
#
# To launch publicly: rent one always-on server, run
#   python -m kestrel.cli start --host 0.0.0.0
# on it, put its URL here, and ship. Everyone who runs any Kestrel app
# then joins the same network automatically.
SEED_NODES: list[str] = []

# Seed LISTS fetched over HTTPS at startup — Bitcoin's DNS-seed idea as a
# plain text file. Host a file of node URLs (one per line, # comments ok)
# anywhere you control — a GitHub "raw" URL is perfect and free — and put
# its address here. You can then add/remove public nodes ANY TIME without
# re-shipping the apps: every node checks the list each launch and caches
# it for offline starts.
# Example:
#   SEED_LIST_URLS = ["https://raw.githubusercontent.com/<you>/kestrel-seeds/main/seeds.txt"]
SEED_LIST_URLS: list[str] = []
