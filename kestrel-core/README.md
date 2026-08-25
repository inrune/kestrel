# Kestrel

Fast, light, decentralized money. A fair-launch scrypt proof-of-work
chain, hard-capped at **44,000,000 KSL**, with no premine and an
unspendable genesis reward. This is the reference implementation —
consensus, transactions, proof-of-work, peer-to-peer node and
command-line tools in ~1,500 lines of readable Python, one dependency.

Like Bitcoin at launch, Kestrel ships small on purpose: a node, a miner,
a wallet. Explorers, pools, markets and everything else are yours to
build — every node exposes the full chain as plain JSON with open CORS.

## Parameters

| parameter        | value                                   |
|------------------|-----------------------------------------|
| proof of work    | scrypt (N=1024, r=1, p=1)               |
| block time       | 120 seconds                             |
| initial reward   | 25 KSL                                  |
| halving          | every 880,000 blocks                    |
| supply cap       | 44,000,000 KSL (exact)                  |
| retarget         | every 2,016 blocks, clamped 4×          |
| coinbase maturity| 10 blocks                               |
| smallest unit    | 1 feather = 0.00000001 KSL              |
| default port     | 4444                                    |
| addresses        | base58check, start with `K`             |

## Install and run

```
pip install -r requirements.txt

python -m kestrel.cli start                      # create a wallet + run a full node
python -m kestrel.cli node --peer http://<host>:4444    # join a network
python -m kestrel.cli wallet new                 # create a wallet
python -m kestrel.cli mine --blocks 12           # mine (rewards mature after 10)
python -m kestrel.cli mine --threads 8           # use more CPU cores
python -m kestrel.cli balance                    # confirmed + spendable
python -m kestrel.cli send K<address> 3.5        # sign and broadcast
python -m kestrel.cli info                       # chain summary
```

Chain data lives in `kestrel-data/`, your key in `kestrel-wallet.json` —
back that file up like cash: anyone with it can spend, and without it
lost coins are gone forever.

## Official apps

Two simple desktop apps ship separately (`kestrel-miner/` and
`kestrel-wallet/` next to this folder):

- **Kestrel Miner** (`kestrel-miner.zip`) — one button, live hashrate,
  every 25 KSL reward to your address. A full node is built in: flip one
  switch and your miner gossips blocks and serves the JSON API.
- **Kestrel Wallet** (`kestrel-wallet.zip`) — keys created and
  transactions signed on your machine; the network only sees the
  finished signature. Balance, send, history.

Both use the same `kestrel-wallet.json` format as the CLI, so keys move
freely between all three.

## Networking — ready out of the box

Nodes connect themselves, the way Bitcoin launched:

- **Worldwide auto-discovery** — every node announces itself on the
  public **BitTorrent DHT** (a free, decentralized directory of millions
  of nodes running since 2005) under a key derived from the network
  magic, and looks up that key to find everyone else. Two strangers in
  different countries who both open a Kestrel app find each other and
  sync **with no server, no seed list, and nothing to type** — the same
  role IRC then DNS seeds played for early Bitcoin, done peer-to-peer.
  Disable with `KESTREL_DHT=0`; point at custom routers with
  `KESTREL_DHT_BOOTSTRAP`.

**Discovery vs. reachability — the honest part.** Finding other nodes
(discovery) and being able to *connect in* to them (reachability) are
different problems. The DHT, seeds and LAN discovery solve discovery for
everyone. Reachability is limited by home routers: a computer behind NAT
can always connect **out**, but others can only connect **in** if its
port is open. Kestrel opens it automatically with UPnP where the router
allows it, and every node runs a self-check (`POST /checkreach`, asking a
peer to connect back) so the app can tell you plainly whether you're
reachable. Nodes that aren't reachable still sync and mine perfectly —
they pull from reachable nodes. This is exactly how Bitcoin works: most
wallets aren't reachable; the network stands on the nodes that are. So a
healthy public Kestrel network needs **at least one reachable node**, and
the more people run one (VPS, or a home PC with TCP 4444 forwarded), the
more robust it gets. Private/loopback addresses are never advertised to
the wider internet, so peer lists stay clean and connectable.
- **LAN auto-discovery** — every node broadcasts a tiny UDP beacon
  (port 4544). Two computers on the same Wi-Fi/router find each other
  with zero configuration. Nothing to type.
- **Seed nodes** — for the public internet, nodes auto-connect to the
  entry points listed in `kestrel/params.py` (`SEED_NODES`), in the
  `KESTREL_SEEDS` environment variable, or in a `seeds.txt` file (one
  URL per line). One reachable seed is enough.
- **Published seed lists** — Bitcoin's DNS-seed idea, as a plain file:
  point `SEED_LIST_URLS` at a text file you host (a GitHub raw URL is
  free and perfect). Every app checks it on launch and caches it, so you
  can add or remove public nodes any time without re-shipping anything.
- **Automatic port opening (UPnP + NAT-PMP)** — on launch, and again
  every 15 minutes, each node asks the home router to forward its port
  (UPnP-IGD first, then NAT-PMP), the same trick early Bitcoin used, so
  ordinary home computers accept incoming connections with no setup —
  and keep accepting them across router reboots and lease expiries.
  If the router refuses, the node still works outbound.
- **Connect by address — any shape** — sharing addresses works the way
  people actually type them: `12.34.56.78`, `12.34.56.78:4444` and
  `http://12.34.56.78:4444` are all accepted, in the apps' connect box,
  in `--peer`, in `seeds.txt` and at `POST /peers/add`. Adding an
  address connects **immediately** — the node says hello (so the other
  side learns your address too) and syncs on the spot. One side being
  reachable is enough to link two people: whoever can't be reached
  simply connects to the one who can.
- **Peer exchange** — every handshake shares peer lists, so knowing one
  node means learning the whole mesh. Peers are remembered on disk,
  re-announced continuously, and dropped after repeated failures.
- **Gossip** — new blocks and transactions push to all peers instantly;
  a background loop catches up anything missed (incremental sync — only
  missing blocks are downloaded — with full re-validation on forks).
  Mempools sync too, so a transaction sent anywhere reaches every miner.

### Going public — the launch checklist

With worldwide DHT discovery, nodes find each other on their own — the
network can bootstrap with no central server at all. For a **fast,
reliable** launch it still helps to run one always-on anchor node (the
DHT can take a minute, and some restrictive networks block UDP), but it
is now a convenience, not a requirement.

1. **Run the anchor node.** Any always-on machine works — a $4/month
   VPS, an old PC with port 4444 forwarded, anything:
   `python -m kestrel.cli start --host 0.0.0.0`
2. **Publish the seed list.** Create a public GitHub repo with one file,
   `seeds.txt`, containing `http://<your-node-ip>:4444`. Copy the file's
   **Raw** URL.
3. **Point the apps at it.** Put that URL into `SEED_LIST_URLS` in
   `kestrel/params.py` (all three folders) — done. Ship the apps.
4. **Grow.** Every person who opens the miner or wallet lands on the
   network, downloads the full ledger, and (thanks to UPnP) most become
   reachable nodes themselves. As volunteers appear, add their addresses
   to `seeds.txt` — every app in the world follows the update on its
   next launch, no re-release needed.

That's the same shape Bitcoin launched with: hardcoded entry points,
peer exchange for the rest, every node holding the whole ledger.

## Node HTTP API — build your apps here

Every node speaks plain JSON over HTTP (CORS open):

```
GET  /                    live dashboard (browser) · JSON welcome (API)
GET  /info                node + chain summary
GET  /supply              rich chain statistics
GET  /latest?n=15         newest blocks
GET  /chain?from=H        full blocks from height H
GET  /block/<height>      one block with transactions, fees, miner
GET  /blockhash/<id>      block by id
GET  /tx/<txid>           transaction with fee + confirmations
GET  /address/<addr>      balance, UTXOs, history
GET  /balance/<addr>      confirmed + spendable
GET  /utxos/<addr>        spendable outputs
GET  /richlist?n=20       largest balances
GET  /search/<query>      classify height / block / tx / address
GET  /mempool             pending transactions (incl. raw, for relay)
GET  /peers               known peers + liveness
POST /tx                  submit a signed transaction   {"tx": {...}}
POST /block               submit a mined block          {"block": {...}}
POST /announce            p2p hello                     {"port", "id"}
POST /peers/add           register a peer               {"url": "http://..."}
POST /mine                mine n blocks (loopback only) {"address", "count", "threads"}
```

An explorer is a weekend project: `/latest` for the feed, `/search` to
route queries, `/block`, `/tx` and `/address` for the pages. A wallet is
`/utxos` + local signing + `POST /tx` — the signature scheme
(deterministic ECDSA over canonical JSON, sha256d sighash) is specified
in the whitepaper and implemented in `kestrel/wallet.py`.

Don't want to build one first? **Open any node's URL in a browser.** Every node serves a live dashboard — height, circulating supply, difficulty and estimated network hashrate, halving countdown, peers, recent blocks and a search box — at `/`, built entirely on the endpoints above (`kestrel/dashboard.py`, one self-contained file, no dependencies).

## Consensus in one paragraph

Block ids are sha256d of the header; the proof-of-work hash is scrypt of
the same header against a per-block target; the valid chain is the one
with the most accumulated work. Transactions spend prior unspent outputs
by ECDSA (secp256k1, RFC 6979, DER) signatures over a sha256d sighash
that commits to every input and output. Nodes verify everything and
trust nothing.

## Layout

```
kestrel/params.py         consensus constants
kestrel/crypto_utils.py   hashing, base58check, keys, signatures
kestrel/transaction.py    transactions, txids, sighash
kestrel/block.py          blocks, merkle root, proof of work
kestrel/blockchain.py     validation, UTXO set, retargeting, persistence
kestrel/wallet.py         keys, coin selection, transaction building
kestrel/miner.py          candidate assembly, multi-core proof-of-work
kestrel/discovery.py      LAN auto-discovery + seed loading
kestrel/node.py           HTTP p2p node + JSON API
kestrel/dashboard.py      self-contained browser dashboard (served at /)
kestrel/cli.py            command-line interface
../kestrel-miner, ../kestrel-wallet   the official desktop apps
docs/                     the Kestrel whitepaper
```

MIT licensed. The rules of the coin are in this folder — read them,
verify them, build on them.
