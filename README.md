# Kestrel (KSL)

**Fast, light, decentralized money.** A fair-launch scrypt proof-of-work
chain, hard-capped at **44,000,000 KSL** — no premine, no company, an
unspendable genesis reward. The whole thing is small on purpose: a node, a
miner, a wallet, and about 1,500 lines of readable Python with one
dependency. Explorers, pools, and markets are yours to build — every node
serves the full chain as open JSON, and a live dashboard in any browser.

| | |
|---|---|
| proof of work | scrypt (N=1024, r=1, p=1) |
| block time | 120 seconds |
| initial reward | 25 KSL, halving every 880,000 blocks |
| supply cap | 44,000,000 KSL (exact) |
| smallest unit | 1 feather = 0.00000001 KSL |
| addresses | base58check, start with `K` |
| default port | 4444 |
| license | MIT |

## What's in this folder

| Path | What it is |
|---|---|
| `kestrel-core/` | The reference implementation — consensus, node, miner, wallet and CLI. Start here to read or verify the rules. |
| `kestrel-miner/` | **Kestrel Miner** — the one-button desktop mining app (a full node is built in). |
| `kestrel-wallet/` | **Kestrel Wallet** — the desktop wallet: keys and signing stay on your machine. |
| `KESTREL-WHITEPAPER.docx` | The whitepaper (also `kestrel-core/docs/WHITEPAPER.md`). |
| `build-apps.sh` | Rebuilds the three release zips. |
| `deploy/` | One-command VPS anchor-node setup + the go-public walkthrough (`deploy/DEPLOY.md`). |
| `tests/` | Automated test suite (consensus, crypto, wallet, networking). Run `./run-tests.sh`. |

The three folders each ship a self-contained copy of the `kestrel/`
package, so any one of them runs on its own. Keys use the same
`kestrel-wallet.json` format everywhere, so they move freely between the
CLI, the miner and the wallet.

## Quick start

Everything needs **Python 3.10+** and the one dependency (`ecdsa`), which
the launch scripts install for you.

**Mine (desktop app)** — open `kestrel-miner/`, run `run.bat` (Windows) or
`run.sh` (Mac/Linux). It creates a reward address, starts a full node, and
connects to the network by itself. Press **Start mining**.

**Hold & send (desktop app)** — open `kestrel-wallet/`, run `run.bat` /
`run.sh`. Back up the key it shows you once.

**Command line** — from `kestrel-core/`:

```
pip install -r requirements.txt
python -m kestrel.cli start          # create a wallet + run a full node
python -m kestrel.cli mine --blocks 12
python -m kestrel.cli balance
python -m kestrel.cli send K<address> 3.5
python -m kestrel.cli info
```

## The node dashboard

Start any node and open its URL in a browser — for a local node that's
**http://localhost:4444/**. You get a live view of the network: height,
circulating supply and the % of the cap mined, difficulty and estimated
network hashrate, the halving countdown, connected peers with liveness,
the newest blocks, the richest addresses, and a search box that opens any
block, transaction or address. It is one self-contained file
(`kestrel/dashboard.py`) with no dependencies, built entirely on the
node's own public JSON endpoints — API clients still get JSON at the same
URL, so nothing about the API changes. The miner's built-in node serves it
too (Network ▸ **Open dashboard**), and the wallet can open it from
Settings ▸ **Open node dashboard**.

## Networking — ready out of the box

Nodes connect themselves the way Bitcoin launched: worldwide auto-discovery
over the public BitTorrent DHT, LAN discovery, seed nodes and published
seed lists, automatic router port-opening (UPnP), peer exchange and gossip.
Open two Kestrel apps anywhere on Earth and they find each other with
nothing to type. Full detail — including the honest bits about NAT and
reachability, and a public-launch checklist — is in
[`kestrel-core/README.md`](kestrel-core/README.md). To stand up an
always-on anchor node and go public in ~15 minutes, follow
[`deploy/DEPLOY.md`](deploy/DEPLOY.md).

## Build the release zips

```
./build-apps.sh
```

Produces `kestrel-core.zip`, `kestrel-miner.zip` and `kestrel-wallet.zip`
next to the website, excluding caches, backups, keys and local chain data.

## What's new in v1.4.2

- **Node:** mining through the JSON API (`POST /mine`, used by
  `kestrel.cli mine` when a node is already running) no longer holds the
  chain lock during the proof-of-work grind. The node's dashboard, API and
  background sync now stay fully responsive while it mines, and an incoming
  network block interrupts the round so work is never wasted on a stale
  tip. The desktop miner already worked this way; the endpoint now matches.
- **Tests + CI:** added an automated test suite (`tests/`, run with
  `./run-tests.sh`) covering the consensus rules, cryptography, wallet and
  live two-node networking, plus a GitHub Actions workflow that runs it on
  every push across Python 3.10–3.12.
- **Deploy kit:** added `deploy/` — a one-command VPS anchor-node installer
  (`setup-vps.sh`, systemd + firewall), a seed-wiring helper
  (`set-seeds.sh`), and a full go-public walkthrough (`deploy/DEPLOY.md`).
- Added the `build-apps.sh` release packager the READMEs referenced, a
  repo-root `.gitignore`/`LICENSE`, and stopped shipping throwaway keys and
  local chain data in the source tree.

## What's new in v1.4.1

- **Wallet:** fixed a crash that showed an empty, frozen dialog when opening
  *Connect to a network node*, *Node address*, *Restore from key*,
  *Share this computer's node* or *Show backup key*. All dialogs now open
  centered over the window and can't lock the app even if something fails.
- **Miner + Wallet:** repaired the bundled `discovery.py` and
  `rendezvous.py`, which had shipped truncated — LAN auto-discovery
  crashed on the first packet heard and worldwide DHT discovery silently
  did nothing. Both now match the reference implementation exactly.
- **Both apps:** the connect boxes accept loose addresses — `12.34.56.78`,
  `12.34.56.78:4444` and full URLs all work, matching the node API.
- **Wallet:** quitting now shuts the built-in node down cleanly.
- Added the `build-apps.sh` release packager the README always promised
  (it excludes keys, chain data and caches from the zips), refreshed the
  website (live node stats, networking section, more FAQ) and fixed its
  stale version strings.

## License

MIT. The rules of the coin live in `kestrel-core/kestrel/` — read them,
verify them, build on them.
