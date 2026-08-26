"""
Kestrel command-line interface.

  python -m kestrel.cli start                      # create a wallet + run a full node
  python -m kestrel.cli wallet new
  python -m kestrel.cli mine --blocks 12
  python -m kestrel.cli balance [ADDRESS]
  python -m kestrel.cli send KADDRESS 1.5 [--fee 0.0001]
  python -m kestrel.cli node --port 4444 [--peer http://host:4444]
  python -m kestrel.cli info
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from . import params
from .blockchain import Blockchain, ValidationError
from .wallet import Wallet, format_ksl, parse_ksl
from .miner import mine
from .node import Node

WALLET_FILE = "kestrel-wallet.json"


def load_or_create_wallet(path: str) -> Wallet:
    if os.path.exists(path):
        return Wallet.load(path)
    wallet = Wallet.create()
    wallet.save(path)
    print(f"created new wallet -> {path}")
    print(f"address: {wallet.address}\n")
    return wallet


def post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get_json(url: str, timeout: int = 8) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def local_node() -> str | None:
    """URL of a Kestrel node already running on this machine, if any.

    When a node is running, the CLI talks to it over HTTP instead of
    touching the chain files directly — two writers on one data dir
    would silently overwrite each other's blocks and transactions.
    """
    for port in range(params.DEFAULT_PORT, params.DEFAULT_PORT + 4):
        url = f"http://127.0.0.1:{port}"
        try:
            if get_json(url + "/info", timeout=1.5).get("magic") \
                    == params.NETWORK_MAGIC:
                return url
        except Exception:
            continue
    return None


def main(argv=None):
    p = argparse.ArgumentParser(prog="kestrel", description="Kestrel (KSL) node & wallet")
    p.add_argument("--data-dir", default=None, help="chain data directory")
    p.add_argument("--wallet", default=WALLET_FILE, help="wallet file path")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wallet", help="manage the local wallet")
    w.add_argument("action", choices=["new", "show"])

    sub.add_parser("info", help="chain summary")

    b = sub.add_parser("balance", help="balance of an address (default: your wallet)")
    b.add_argument("address", nargs="?")

    m = sub.add_parser("mine", help="mine blocks to your wallet address")
    m.add_argument("--blocks", type=int, default=1)
    m.add_argument("--address", default=None)
    m.add_argument("--threads", type=int, default=0,
                   help="CPU threads (default: all cores but one)")

    s = sub.add_parser("send", help="send KSL")
    s.add_argument("to")
    s.add_argument("amount", help="amount in KSL, e.g. 1.5")
    s.add_argument("--fee", default="0.00001")
    s.add_argument("--node", default=None,
                   help="submit via a node URL instead of the local mempool")

    n = sub.add_parser("node", help="run a full node")
    n.add_argument("--host", default="0.0.0.0")
    n.add_argument("--port", type=int, default=params.DEFAULT_PORT)
    n.add_argument("--peer", action="append", default=[],
                   help="peer address, repeatable: --peer 1.2.3.4 or http://host:4444")

    st = sub.add_parser("start",
                        help="create a wallet if needed, then run a full node")
    st.add_argument("--host", default="127.0.0.1")
    st.add_argument("--port", type=int, default=params.DEFAULT_PORT)
    st.add_argument("--peer", action="append", default=[],
                    help="peer address, repeatable: --peer 1.2.3.4 or http://host:4444")

    args = p.parse_args(argv)

    if args.cmd == "wallet":
        if args.action == "new" and os.path.exists(args.wallet):
            print(f"refusing to overwrite existing wallet at {args.wallet}")
            return 1
        wallet = load_or_create_wallet(args.wallet)
        print(f"address : {wallet.address}")
        print(f"pubkey  : {wallet.public_key.hex()}")
        print(f"file    : {os.path.abspath(args.wallet)}")
        return 0

    # a node already running on this machine owns the chain files; route
    # everything through its HTTP API instead of writing beside it
    node_url = None if args.cmd in ("node", "start") else local_node()

    if args.cmd == "info":
        if node_url:
            s = get_json(node_url + "/supply")
            print(f"network    : kestrel ({s['magic']})  via {node_url}")
            print(f"height     : {s['height']}")
            print(f"tip        : {s['tip']}")
            print(f"difficulty : {s['difficulty']:.4f}")
            print(f"supply     : {s['circulating_ksl']} "
                  f"of {s['max_supply_ksl']}")
            print(f"next reward: {s['next_reward_ksl']}")
            print(f"mempool    : {s['mempool']} tx")
            return 0
        chain = Blockchain(data_dir=args.data_dir)
        tip = chain.tip
        print(f"network    : kestrel ({params.NETWORK_MAGIC})")
        print(f"height     : {chain.height}")
        print(f"tip        : {tip.block_id}")
        print(f"difficulty : {chain.difficulty_of(tip.target):.4f}")
        print(f"supply     : {format_ksl(chain.circulating_supply())} "
              f"of {format_ksl(params.MAX_SUPPLY)}")
        print(f"next reward: {format_ksl(chain.block_subsidy(chain.height + 1))}")
        print(f"mempool    : {len(chain.mempool)} tx")
        return 0

    if args.cmd == "balance":
        address = args.address or load_or_create_wallet(args.wallet).address
        if node_url:
            bal = get_json(f"{node_url}/balance/{address}")
        else:
            bal = Blockchain(data_dir=args.data_dir).balance(address)
        print(f"address   : {address}")
        print(f"confirmed : {format_ksl(bal['confirmed'])}")
        print(f"spendable : {format_ksl(bal['spendable'])}")
        return 0

    if args.cmd == "mine":
        from .miner import default_threads
        address = args.address or load_or_create_wallet(args.wallet).address
        threads = args.threads or default_threads()
        print(f"mining {args.blocks} block(s) to {address} "
              f"({threads} thread(s))")
        if node_url:
            print(f"a node is running here — mining through it ({node_url})")
            out = post_json(node_url + "/mine",
                            {"address": address, "count": args.blocks,
                             "threads": threads}, timeout=3600)
            print(f"height now {out.get('height')}")
            return 0
        chain = Blockchain(data_dir=args.data_dir)
        mine(chain, address, count=args.blocks, threads=threads)
        print(f"height now {chain.height}, "
              f"supply {format_ksl(chain.circulating_supply())}")
        return 0

    if args.cmd == "send":
        wallet = load_or_create_wallet(args.wallet)
        try:
            amount, fee = parse_ksl(args.amount), parse_ksl(args.fee)
        except ValueError as e:
            print(f"error: {e}")
            return 1
        try:
            target = (args.node.rstrip("/") if args.node else node_url)
            if target:
                utxos = get_json(f"{target}/utxos/{wallet.address}")["utxos"]
                tx = wallet.build_transaction(utxos, args.to, amount, fee)
                out = post_json(target + "/tx", {"tx": tx.to_dict()})
                print(json.dumps(out, indent=2))
            else:
                chain = Blockchain(data_dir=args.data_dir)
                tx = wallet.build_transaction(
                    chain.utxos_for(wallet.address), args.to, amount, fee)
                txid = chain.add_transaction(tx)
                chain.save()
                print(f"queued in local mempool: {txid}")
                print("mine a block to confirm it:  python -m kestrel.cli mine")
        except ValidationError as e:
            print(f"error: {e}")
            return 1
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error", str(e))
            except Exception:
                detail = str(e)
            print(f"node rejected it: {detail}")
            return 1
        return 0

    if args.cmd == "node":
        chain = Blockchain(data_dir=args.data_dir)
        Node(chain, host=args.host, port=args.port, peers=args.peer).serve_forever()
        return 0

    if args.cmd == "start":
        # first run made easy: ensure a wallet exists, then run a full node
        wallet = load_or_create_wallet(args.wallet)
        shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
        print(f"your wallet address: {wallet.address}")
        print(f"JSON API will be at http://{shown}:{args.port}/")
        chain = Blockchain(data_dir=args.data_dir)
        Node(chain, host=args.host, port=args.port, peers=args.peer).serve_forever()
        return 0


if __name__ == "__main__":
    sys.exit(main())
