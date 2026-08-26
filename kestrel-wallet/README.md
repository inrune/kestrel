# Kestrel Wallet

Open it and it's ready — no setup.

**To start:** double-click `run.bat` (Windows) or `run.sh` (Mac/Linux).
Or: `pip install ecdsa` then `python app.py`.

On first launch it creates your wallet (write down the backup key it
shows), then finds a Kestrel node on your machine — or quietly runs its
own, which auto-connects to the network (worldwide discovery over the
public BitTorrent DHT, seeds, remembered peers, and LAN discovery). Keys
are created and every transaction is signed locally; the network only
sees the finished signature.

Paste works everywhere: right-click any box ▸ Paste (Ctrl+V too), and
the Restore-from-key dialog is paste-friendly as well. The footer shows
your connection, which node you're using and the current block.

- **Overview** — Available / Immature / Total balances, plus an
  **Unconfirmed** figure the moment money is on its way (in or out).
  A gentle banner reminds you to back up your key until you have.
- **Send** — the form checks itself as you type: address ✓/✗ (it even
  recognizes your contacts by name), amount vs. what you have, and a
  live "after sending you'll have X left" preview. Fee presets
  (min / ×10 / ×100), **Max**, and a clear confirmation summary before
  anything leaves. Payments appear as **Pending** seconds later and
  confirm in ~2 minutes.
- **Receive** — your address with a scannable **QR code** (save it as a
  PNG to print or send), one-click Copy, and click-the-address-to-copy.
- **Transactions** — filter **All / Sent / Received / Mined / Pending**,
  search anything, double-click a row for full details, and export the
  list to **CSV** for a spreadsheet.
- **Contacts** — labeled addresses with live address checking;
  double-click one to send.

Growing the network from the wallet: Settings ▸ **Connect to a network
node** adds a friend's node (your node remembers it and passes it on),
and **Share this computer's node** shows your own address with copy and
a ready-to-send invite message.

Small comforts everywhere: notifications instead of pop-ups, sortable
tables with scrollbars, tooltips, remembered window size and position,
sharp text on high-DPI Windows screens, and keyboard shortcuts —
**Ctrl+1…5** switch views, **F5** refreshes.

Back up once via File ▸ Backup (or Show backup key) — it's the only way to
restore your coins. Running Kestrel Miner alongside gives this wallet a
node automatically.
