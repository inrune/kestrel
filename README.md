# Kestrel Miner

Open it and you're mining — no setup.

**To start:** double-click `run.bat` (Windows) or `run.sh` (Mac/Linux).
Or: `pip install ecdsa` then `python app.py`.

On first launch the app creates your reward address (write down the backup
key it shows you) and starts a full node that connects by itself —
worldwide discovery over the public BitTorrent DHT (so it finds Kestrel
nodes anywhere on Earth with no setup), plus seed nodes, remembered peers,
and any node on your own network. You're live out of the box. Press
**Start mining** and every block found pays the reward to your address.

**Mining to your own address:** click **Paste** (or right-click the
address box ▸ Paste) and your address is used and remembered — including
across restarts. **Mine to my wallet** switches back to the app's own
address. A ✓/✗ hint tells you instantly whether an address is valid.

- **Mine** — balance, session earnings and an all-time earnings counter
  that survives restarts; CPU-threads picker; **start mining when the app
  opens** checkbox; a live speed graph with peak and average lines, an
  estimated time-per-block at your speed, session totals (hashes, average
  speed, time mining) and your share of the whole network. Every block
  found pops up a notification and lands in the session table.
- **Explorer** — the public ledger live from your own full node. Search
  any block height, address, block id or transaction id — and then just
  keep clicking: every id in a result is a link (previous/next block,
  miner, inputs, outputs). Quick views for **Top holders**, **Pending
  transactions** and the newest block, plus the latest 100 blocks with
  age, size and difficulty. Blocks you mined are highlighted.
- **Network** — a big **Share your node** card up top: copy your address
  with one click, or **Copy invite** for a ready-to-send message with the
  address and the three steps to join — paste it into any chat. One
  friend connects once and nodes pass peers to each other, so the network
  spreads by itself. Below it: nodes online / known / pending txs /
  height at a glance, your node's status and JSON API (copy it, or open
  it in the browser), whether people can reach you from the internet, and
  a Connect box for adding a node. Right-click a node to copy its address
  or sync with it right now. If friends can't connect IN to you, a **Fix
  my connection** button appears: with your permission it opens just this
  app's port in Windows Firewall (you approve the standard Windows prompt)
  and retries the router forward, then re-checks — undo any time from
  Help ▸ Remove firewall rule.
- **Activity** — a plain-language log with **All / Blocks / Network /
  Problems** filters, plus one-click copy of the whole log.

Small comforts everywhere: notifications instead of pop-ups, sortable
tables with scrollbars, tooltips, remembered window size and position,
sharp text on high-DPI Windows screens, and keyboard shortcuts —
**Ctrl+1…4** switch views, **Ctrl+M** starts/stops mining, **F5**
refreshes everything.

Spend what you mine in Kestrel Wallet (File ▸ Restore with your backup
key), which finds this node on your machine by itself. Data lives in
`kestrel-data/`, your key in `kestrel-wallet.json`, your preferences in
`miner-settings.json` — guard the wallet file like cash.
