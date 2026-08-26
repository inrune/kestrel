# Kestrel: Fast, Light, Decentralized Money

**A peer-to-peer electronic cash system with a fixed supply of 44,000,000
coins, scrypt proof-of-work, and two-minute blocks.**

The Kestrel Developers · July 2026 · Whitepaper v1.0
(Formatted edition: `WHITEPAPER.docx`)

## Abstract

Kestrel is a decentralized digital currency in the tradition of Bitcoin and
Litecoin. It is issued by no company and administered by no authority: a
peer-to-peer network of equal nodes maintains a shared ledger, secured by
scrypt proof-of-work, in which exactly **44,000,000 KSL** will ever exist.
New coins enter circulation only as mining rewards — 25 KSL per two-minute
block, halving every 880,000 blocks — and the genesis reward is unspendable,
so the launch is provably fair with no premine. This paper specifies the
complete protocol as realized in the open-source reference implementation.

## 1. Introduction

Conventional money is a liability of an institution: its supply is set by
committee, its transfer requires intermediaries, and access to it can be
granted or withdrawn by decision. Bitcoin (2008) demonstrated a different
construction — money whose rules are enforced not by an issuer but by every
participant at once. Litecoin showed the same design running on a memory-hard
hash function with faster blocks.

Kestrel continues that lineage with three commitments: **scarcity as
arithmetic** (a 44,000,000 KSL cap that follows mechanically from the reward
schedule), **lightness** (two-minute blocks and a protocol small enough that
its entire consensus definition fits in one parameters file), and
**fairness** (no premine; the genesis reward is unspendable by construction).

## 2. Design overview

The network is a set of equal peers. Each node stores the full chain, relays
transactions and blocks, and independently verifies every rule; nothing from
the network is trusted. A block is a header (version, height, previous-block
id, merkle root, timestamp, target, nonce) plus transactions. Blocks are
identified by the double SHA-256 of the header; proof-of-work validity is
judged by the scrypt hash of the same header. Serialization is canonical
JSON (sorted keys, no whitespace): byte-identical on every node, and
human-readable by design.

| Parameter | Value |
|---|---|
| Ticker / unit | KSL; 1 feather = 10⁻⁸ KSL |
| Consensus | Proof of work, heaviest valid chain |
| PoW hash | scrypt, N=1024, r=1, p=1 |
| Block identifier | double SHA-256 of the header |
| Maximum supply | 44,000,000 KSL |
| Initial reward | 25 KSL |
| Halving interval | 880,000 blocks (~3.35 years) |
| Target block time | 120 seconds |
| Difficulty retarget | every 2,016 blocks, clamped to 4× |
| Coinbase maturity | 10 confirmations |
| Minimum relay fee | 0.00001 KSL |
| Maximum block size | 1,000,000 bytes |
| Signatures | ECDSA secp256k1, RFC 6979, DER |
| Address format | base58check, version 0x2D → prefix `K` |
| Genesis | 2026-07-03 00:00 UTC, reward unspendable |

## 3. Transactions and the UTXO model

The ledger's state is a set of unspent transaction outputs (UTXOs), each
locked to an address. A transaction consumes existing outputs and creates new
ones under three rules: **existence and uniqueness** (every input references
a UTXO no other input claims), **authorization** (each input carries a public
key hashing to the UTXO's address and an ECDSA signature over the
transaction digest — SHA-256d of the transaction serialized without
signatures, SIGHASH_ALL semantics), and **conservation** (inputs ≥ outputs;
the difference is the miner's fee).

The one exception is the coinbase: the first transaction of a block, with no
real inputs, paying the miner subsidy + fees. Coinbase outputs mature after
10 confirmations so reorganizations cannot strand reward-funded payments.
All amounts are integers in feathers; floating point never touches money.

## 4. Keys and addresses

Ownership is a secp256k1 keypair. Public keys are 33-byte compressed;
signatures are DER-encoded and deterministic (RFC 6979).

```
address = base58check( 0x2D || SHA256d(pubkey)[0..20) )
```

Version byte 0x2D yields addresses beginning with **K**. For the 160-bit
hash, Kestrel uses double SHA-256 truncated to 20 bytes rather than
RIPEMD-160(SHA-256): equivalent security for this purpose, identical address
format, and no dependency on OpenSSL's legacy RIPEMD provider. Private keys
export as WIF under version byte 0xAD.

## 5. Proof of work

A block is valid only if `scrypt(header, N=1024, r=1, p=1) ≤ target`.
These are Litecoin's cost settings: each evaluation touches a 128 KiB
scratchpad, narrowing the gap between commodity hardware and specialized
silicon. The block id remains SHA-256d, keeping chain links cheap to verify.
Each block contributes `2²⁵⁶ / (target+1)` expected work; nodes follow the
valid chain with the greatest total work, making history rewrites
exponentially expensive — and even a majority attacker can only reorder
recent payments, never forge signatures or mint coins.

## 6. Difficulty adjustment

Every 2,016 blocks each node recomputes
`new_target = old_target × actual_timespan / expected_timespan`, with the
timespan clamped to [expected/4, expected×4]. Timestamps must exceed the
median of the previous 11 blocks and be no more than two hours ahead of the
validator's clock. The launch target permits a block after ~4,096 hashes —
deliberately easy so CPUs can mine the young network; the retarget rule
raises difficulty automatically as hash power arrives.

## 7. Monetary policy: the 44,000,000 cap

```
subsidy(height) = 25 KSL >> (height / 880,000)      (integer floor)
```

Summing the series: 25 × 880,000 × (1 + ½ + ¼ + …) = **2 × 25 × 880,000 =
44,000,000 KSL**. Each era lasts ~3.35 years at the two-minute block time.

| Era | Block heights | Reward | ~Years | Cumulative |
|---|---|---|---|---|
| I | 0 – 879,999 | 25 | 2026–2029 | 22,000,000 |
| II | 880,000 – 1,759,999 | 12.5 | 2029–2033 | 33,000,000 |
| III | 1,760,000 – 2,639,999 | 6.25 | 2033–2036 | 38,500,000 |
| IV | 2,640,000 – 3,519,999 | 3.125 | 2036–2039 | 41,250,000 |
| V | 3,520,000 – 4,399,999 | 1.5625 | 2039–2043 | 42,625,000 |
| … | continuing halvings | → 0 | … | → 44,000,000 |

Because the coinbase may claim subsidy + fees and no more, and every node
recomputes the permitted amount for every block, the schedule is not a
promise — it is a validity condition. A block that overpays is not a Kestrel
block.

## 8. Network and consensus

Nodes speak a minimal HTTP protocol: new transactions and blocks are pushed
to peers on arrival, and nodes periodically compare total chain work. On
finding a heavier chain, a node downloads and re-validates it in full — every
proof-of-work, signature, and UTXO rule, from genesis — before adopting it;
valid pending transactions are re-admitted afterward. Mempool admission
enforces block-level rules plus a 0.00001 KSL minimum fee; miners fill
blocks by fee rate. The chain persists to JSON and is re-verified on load.
Every node also serves the full chain as open-CORS JSON and a live HTML
dashboard at its root URL, so explorers, wallets, and the apps that ship
with Kestrel are ordinary clients of the same public endpoints.

## 9. Relation to Bitcoin and Litecoin

| Property | Bitcoin | Litecoin | **Kestrel** |
|---|---|---|---|
| Hard cap | 21,000,000 | 84,000,000 | **44,000,000** |
| Block time | 10 min | 2.5 min | **2 min** |
| Proof of work | SHA-256d | scrypt | **scrypt** |
| Halving interval | 210,000 blocks (~4 y) | 840,000 (~4 y) | **880,000 (~3.35 y)** |
| Initial reward | 50 BTC | 50 LTC | **25 KSL** |
| Address prefix | 1 / 3 / bc1 | L / M / ltc1 | **K** |
| Launch | fair, no premine | fair, no premine | **fair, no premine** |

## 10. Security considerations

- **Majority hash power.** A sustained majority can reorder recent blocks
  and double-spend; the risk is highest while total work is small. Large
  payments deserve proportionally more confirmations.
- **Launch difficulty.** The easy starting target helps everyone, including
  adversaries. Deployments needing immediate robustness should harden the
  genesis target and raise coinbase maturity.
- **Key custody.** A wallet file holds a raw private key. Whoever reads it
  owns the coins; there is no recovery. That is the cost of money that
  cannot be frozen.
- **Small surface.** No scripting language — outputs lock to key hashes
  only. Fewer features, and a validation path auditable line by line.
- **Audit status.** Complete and internally tested, but no independent
  security review yet. A working, readable foundation for study,
  experimentation, and private networks.

## 11. Reference implementation

~1,500 lines of Python, one dependency (`ecdsa`). Consensus constants in
`params.py`; primitives in `crypto_utils.py`; validation engine across
`transaction.py`, `block.py`, `blockchain.py`; wallet, miner, HTTP node,
browser dashboard, and CLI in `wallet.py`, `miner.py`, `node.py`,
`dashboard.py`, `cli.py`.

```
$ pip install -r requirements.txt
$ python -m kestrel.cli start
$ python -m kestrel.cli wallet new
$ python -m kestrel.cli mine --blocks 12
$ python -m kestrel.cli send K<address> 3.5
$ python -m kestrel.cli node --port 4444 --peer http://<peer>:4444
```

MIT License. Genesis mined 2026-07-03 00:00 UTC, message *"Kestrel genesis /
03 Jul 2026 / Fast, light, decentralized money for everyone"*, identifier
`c8f460e1f38bd483ced56c037400108032a28e33746da71efeddf698735036f1`.

## 12. Disclaimer

Kestrel is experimental software provided "as is", without warranty of any
kind. This document describes a protocol; it is not an offer, solicitation,
or recommendation to acquire any asset, and nothing in it constitutes
financial, legal, or investment advice. Cryptocurrency values can go to
zero. Participants are responsible for their own keys and for compliance
with the laws of their jurisdiction.
