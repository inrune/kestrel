# Kestrel (KSL)

![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Proof of Work: scrypt](https://img.shields.io/badge/PoW-scrypt-orange.svg)
![Supply: 44M KSL](https://img.shields.io/badge/supply-44M%20KSL-yellow.svg)

**Fast, light, decentralized money.** A fair-launch, scrypt proof-of-work
chain hard-capped at **44,000,000 KSL** — no premine, no company, no ICO, and
a genesis reward that can never be spent.

It is small on purpose: a node, a miner, a wallet, and roughly 1,500 lines of
readable Python resting on a single dependency. You can read the entire rule
set in an evening, which is the point — the consensus code *is* the money, so
it should be short enough to actually check.

```
Download → extract → run. It finds the network by itself.
```

---

## Get started in two minutes

You need **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/) —
on Windows, tick *"Add python.exe to PATH"* in the installer). Everything else
installs itself on first launch.

Grab a zip from [**Releases**](https://github.com/inrune/kestrel/releases),
extract it anywhere, then:

| | Windows | Mac / Linux |
|---|---|---|
| **Mine** — earn KSL | `kestrel-miner` ▸ `run.bat` | `kestrel-miner` ▸ `./run.sh` |
| **Hold & send** | `kestrel-wallet` ▸ `run.bat` | `kestrel-wallet` ▸ `./run.sh` |

The miner has a wallet built in, so one app is enough to mine, hold and spend.
Back up the key it shows you the first time — see
[Keep your coins](#keep-your-coins).

### Or use the command line

```bash
cd kestrel-core
pip install -r requirements.txt

python -m kestrel.cli start           # create a wallet, run a full node
python -m kestrel.cli mine --blocks 12
python -m kestrel.cli balance
python -m kestrel.cli send K<address> 3.5
python -m kestrel.cli info
```

---

## The numbers

| | |
|---|---|
| proof of work | scrypt (N=1024, r=1, p=1) |
| block time | 120 seconds |
| initial reward | 25 KSL, halving every 880,000 blocks (~3.3 years) |
| supply cap | 44,000,000 KSL (exact) |
| coinbase maturity | 10 blocks |
| smallest unit | 1 feather = 0.00000001 KSL |
| addresses | base58check, start with `K` |
| default port | 4444 |
| license | MIT |

The cap is not a separate rule bolted on top — it falls out of the schedule:

```
880,000 blocks × (25 + 12.5 + 6.25 + …) = exactly 44,000,000 KSL
```

There is nothing extra to trust.

---

## What's in here

| Path | What it is |
|---|---|
| `kestrel-core/` | The reference implementation — consensus, node, miner, wallet, CLI. **Start here to verify the rules.** |
| `kestrel-miner/` | Desktop mining app. Full node and wallet built in. |
| `kestrel-wallet/` | Desktop wallet. Keys and signing never leave your machine. |
| `tests/` | The test suite. `./run-tests.sh` |
| `deploy/` | One-command VPS node setup + the go-public walkthrough. |
| `build-apps.sh` | Rebuilds the three release zips. |
| `KESTREL-WHITEPAPER.docx` | The whitepaper (also `kestrel-core/docs/WHITEPAPER.md`). |

Each of the three app folders ships a self-contained copy of the `kestrel/`
package, so any one of them runs on its own. Keys use the same
`kestrel-wallet.json` format everywhere and move freely between them.

---

## Every node is an explorer

Start any node and open its address in a browser — locally that is
**http://localhost:4444/**. You get a live view of the chain: height,
circulating supply and percentage of the cap mined, difficulty and estimated
network hashrate, the halving countdown, connected peers, the newest blocks,
the largest holders, and a search box that opens any block, transaction or
address.

It is one self-contained file with no extra dependencies, built on the node's
public JSON API — so the same URL still serves JSON to programs, and nothing
about the API changes. Explorers, pools and markets are yours to build.

---

## Networking that just works

Nodes find each other the way Bitcoin launched, plus a few conveniences:
worldwide discovery over the public BitTorrent DHT, LAN discovery, seed nodes
and published seed lists, automatic router port-opening (UPnP), peer exchange
and block gossip.

Open two Kestrel apps anywhere on Earth and they find each other with nothing
to type.

Full detail — including the honest parts about NAT and reachability — is in
[`kestrel-core/README.md`](kestrel-core/README.md). To run an always-on node
and go public in about fifteen minutes, follow
[`deploy/DEPLOY.md`](deploy/DEPLOY.md). **More independent nodes is the single
best thing anyone can do for the network.**

---

## Keep your coins

Your wallet file **is** your money. Read this bit properly:

- **Back up `kestrel-wallet.json`** (or the backup key the app shows you) to
  something offline — a USB stick, a piece of paper in a drawer.
- **Lose it and the coins are gone.** There is no password reset, no support
  line, no recovery. Nobody can help you, including us.
- **Leak it and they are stolen.** Anyone holding that file can spend from it.
- **Never commit it to Git.** The included `.gitignore` blocks it, but check
  `git status` anyway before you push.

---

## Honest limitations

Kestrel is young, experimental software under the MIT license. No warranty,
run it at your own risk.

- **A small network is easier to disrupt.** A young chain carries little
  accumulated proof-of-work. Treat balances and confirmation counts with that
  in mind until it grows.
- **Verify, don't trust.** The rules are ~1,500 lines in
  `kestrel-core/kestrel/`. Read them, run the tests, satisfy yourself before
  committing anything you cannot afford to lose.
- **Not financial advice.** This is a technical project, not an investment,
  and nothing here promises value.

---

## Contributing

Issues and pull requests welcome. `./run-tests.sh` and the GitHub Actions CI
run the consensus, crypto, wallet, durability and networking tests on Python
3.10–3.12 — please keep them green and add tests for anything touching the
rules. Because the consensus code is the money, changes there get read
closely.

---

## Supporting the project

Kestrel has no company, no funding and no token sale. If it is useful to
you, KSL sent here pays for the seed node and the time that goes into it.
Entirely optional — running a node helps just as much.

```
KTBXt1vWME9FznuXZ77QMconqg5rk5qDVM
```

The website shows the same address and tells readers to check it against
this file before sending anything. That cross-check is the point: a
website can be tampered with, and two independent sources are harder to
change at once than one. If the two ever disagree, trust neither and open
an issue.

## Changelog

### v1.4.7 — announcements, custom dialogs, and a fork-heal that doesn't expire

**Every dialog is now the app's own.** All 28 `messagebox` calls across the
Miner and Wallet drew a native OS box — grey, system font, ignoring the
theme completely. They now use a styled modal built on the existing
`_dialog`/`_present_dialog` helpers: a severity-coloured spine, the app's
own type and palette, Escape and the window button both answering "no",
and Return confirming. Links inside a dialog are shown as selectable
read-only text and are never opened for you.

The native *file* picker is deliberately kept. People expect their own
bookmarks and recent folders when saving a wallet backup, and a
hand-rolled file browser would be worse in every way that matters.

**Announcements moved to the top of the window.** They were a strip along
the bottom, where the update bar and status line already live; a third
strip down there reads as chrome and gets ignored. The strip now sits
directly under the title bar with a spine coloured by level (slate, amber,
red), a label line carrying the level and date, and a `+n more` count when
several are waiting.

**Announcements.** The Miner and Wallet now read `announcements.txt` from
this repo and show new entries in a bar at the bottom of the window. Edit
that file here on GitHub and save — that is the whole publishing process.
Apps check on launch and every 25 minutes after that. Turn it off under
**Settings → Show announcements from the project**; with it off nothing is
fetched at all, so the choice removes the request and not merely the
message. Dismissed entries never come back, and there is a menu item to
un-dismiss them all.

The feed is treated as untrusted input, because anyone who can edit this
repo can put text in front of every user. Only `http(s)` links survive
parsing, links are printed rather than opened, control characters are
stripped and every field is length-capped. Each message carries a fixed
reminder that Kestrel will never ask for your wallet file or key.

**Fixed: fork healing stopped working after ~69 days.** `gossip_block` is
the only mechanism that can repair a fork with a peer that cannot be
dialled — which is most home miners, behind NAT. It pushed the entire
chain, and gave up outright once the chain passed `MAX_PUSH_BLOCKS`:

    if self.chain.height > MAX_PUSH_BLOCKS:   # 50,000
        return

At two-minute blocks that ceiling arrives after about 69 days, after which
two diverged nodes mine in parallel forever with nothing able to reconcile
them. Pushes now carry only the recent 2,000-block suffix plus the height
it starts at, and the receiver splices it onto its own already-validated
prefix. `from` must be a height the receiver holds and can never be `0`,
so the genesis block stays unreplaceable. Omitting `from` keeps the old
whole-chain behaviour, so upgraded and older nodes still interoperate.

Tests: 87 → 113.

### v1.4.6 — no more accidental private chains

- **A node can no longer start its own chain by accident.** Mining used to
  begin the instant you pressed Start, before the app had spoken to any other
  node. At the starting difficulty a few seconds of solo mining can outweigh
  the entire real network, after which this node correctly refuses to switch
  and the two chains never reconcile — which is why the only cure used to be
  deleting your ledger by hand. The app now finds the network first and mines
  on the shared chain. If nobody answers it says so plainly and starts a new
  network on purpose rather than by mistake.
- **"Rebuild from the network" button.** If a node ever does end up out of
  step, one button in Network re-downloads the chain from other nodes and
  adopts theirs if it is better. Wallets and coins are untouched. Nobody
  should ever have to close the app and delete files to get unstuck.
- **Update notices.** A quiet strip appears when a newer version exists, with
  a link to the downloads page. It never installs anything, and if the check
  fails it stays silent rather than guessing.

### v1.4.5 — two ways to lose money, closed

- **Reorgs no longer destroy payments.** When a node switched to a heavier
  chain, transactions already confirmed in the discarded blocks were simply
  dropped — the money reverted to the sender and the recipient was never paid,
  with nothing anywhere to show it had happened. They are now re-queued and
  mined into the new chain, the way Bitcoin has always handled it.
- **The wallet file can no longer be corrupted by a crash.** It used to be
  written by truncating the real file in place, so a crash or power cut
  part-way through left a broken wallet — and that file is the money. Writes
  now go to a temporary file, are flushed to disk, and only then replace the
  old one. A wallet for a different address is copied aside rather than
  overwritten, so no bug can silently destroy somebody's keys.

### v1.4.4 — wallet in the miner, block explorer

- The miner gained a **Wallet tab**: balance split into spendable / confirmed
  / maturing, a send form, and transaction history. No second app needed to
  spend what you mine.
- The website gained a **live block explorer** — search any block, transaction
  or address, browse recent activity and the largest holders.

### v1.4.3 — the fork that could not heal

- **Miners behind a router were silently forking off.** Syncing only worked by
  *pulling* from a peer, which cannot work when that peer is a home machine
  behind NAT — the most common setup there is. Once two chains diverged
  neither side could repair it and both kept mining in parallel forever. Nodes
  now push their chain to a peer that cannot be pulled from. A lighter chain
  still can never displace a heavier one, so this adds no way to rewrite
  history.

### v1.4.2 — responsive nodes, tests, deploy kit

- Mining through the JSON API no longer holds the chain lock during the
  proof-of-work grind, so the dashboard, API and background sync stay
  responsive while mining.
- Added the automated test suite and GitHub Actions CI across Python
  3.10–3.12.
- Added `deploy/` — one-command VPS node installer, seed-wiring helper, and
  the go-public walkthrough.
- Added `build-apps.sh`, a repo-root `.gitignore` and `LICENSE`, and stopped
  shipping throwaway keys or local chain data in the source tree.

<details>
<summary><strong>v1.4.1</strong></summary>

- **Wallet:** fixed a crash that showed an empty, frozen dialog when opening
  *Connect to a network node*, *Node address*, *Restore from key*, *Share this
  computer's node* or *Show backup key*.
- **Miner + Wallet:** repaired the bundled `discovery.py` and `rendezvous.py`,
  which had shipped truncated — LAN discovery crashed on the first packet and
  worldwide DHT discovery silently did nothing.
- **Both apps:** connect boxes accept loose addresses — `12.34.56.78`,
  `12.34.56.78:4444` and full URLs all work.
- **Wallet:** quitting now shuts the built-in node down cleanly.

</details>

---

## License

MIT. The rules of the coin live in `kestrel-core/kestrel/` — read them, verify
them, build on them.
