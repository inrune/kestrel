# Running Kestrel in public — VPS anchor node + launch guide

This is the end‑to‑end path from "code on my laptop" to "a live network
anyone can join." It assumes you've never used a VPS. Everything is
copy‑paste; the two scripts in this folder do the heavy lifting.

## The picture

```
   your PC (miner + wallet apps)          other people's PCs
            │                                    │
            └───────────────┐      ┌─────────────┘
                            ▼      ▼
                   ┌──────────────────────┐
                   │  your VPS anchor node │   ← always on, public IP,
                   │  http://<IP>:4444     │     holds NO wallet
                   └──────────────────────┘
```

The anchor node's only jobs are to **stay reachable** and **serve the
ledger**, so newcomers have a guaranteed first contact. It never mines and
never holds keys — nothing valuable sits on the public server. Once two
people are connected, the network also finds peers on its own (BitTorrent
DHT + peer exchange), but a brand‑new coin needs at least one reliable
always‑on node, and that's this.

Total time: ~15 minutes. Cost: a $4–5/month VPS is plenty.

---

## Step 0 — get a VPS

Any provider works (DigitalOcean, Hetzner, Vultr, Linode, Oracle Cloud
Free Tier…). When you create it:

- **Image / OS:** Ubuntu 22.04 or 24.04 LTS (these scripts target Ubuntu/Debian).
- **Auth:** add your SSH key if offered — easier and safer than a password.
- Note the server's **public IP** and the **login user** (usually `root`,
  sometimes `ubuntu`).

Connect from your own machine:

```bash
ssh root@YOUR_SERVER_IP      # or:  ssh ubuntu@YOUR_SERVER_IP
```

---

## Step 1 — put your code on GitHub

On your own machine, from the project root (the folder with
`kestrel-core/`, `build-apps.sh`, this `deploy/` folder):

```bash
git init
git add -A
git commit -m "Kestrel v1.4.1"
# create an empty repo on github.com first, then:
git remote add origin https://github.com/<you>/<repo>.git
git branch -M main
git push -u origin main
```

The included `.gitignore` guarantees your **wallet keys and local chain
data are never uploaded** — verify with `git status` that no
`kestrel-wallet.json` or `kestrel-data/` is staged.

> Publishing with `SEED_NODES` empty is fine — you'll fill it in Step 4,
> once you know the node's address.

---

## Step 2 — install the anchor node (one command)

SSH into the VPS, clone your repo, and run the installer:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/<you>/<repo>.git kestrel
cd kestrel
sudo bash deploy/setup-vps.sh
```

That script installs Python, creates a locked‑down `kestrel` user, puts the
node in its own virtualenv under `/opt/kestrel`, writes a **systemd
service** (so it runs 24/7 and restarts on crash and on reboot), opens the
firewall for TCP 4444 (while keeping SSH open), and starts it. It finishes
by printing your node's public address and the exact command for Step 4.

Check it's alive:

```bash
sudo systemctl status kestrel-node        # should say "active (running)"
curl http://127.0.0.1:4444/info           # JSON with height 0 at first
```

*(Different port? `sudo bash deploy/setup-vps.sh --port 5555`.)*

---

## Step 3 — confirm the world can reach it

The OS firewall is open, but **many cloud providers add a second firewall**
("security group" / "cloud firewall" / "network rules"). If yours does,
open **inbound TCP 4444** there too, in the provider's web console.

Then test from a *different* machine (your laptop, not the VPS):

```bash
curl http://YOUR_SERVER_IP:4444/info
```

If you get JSON back, you're reachable — done. If it hangs, the cloud
firewall is still blocking; fix that and retry. You can also just open
`http://YOUR_SERVER_IP:4444/` in a browser to see the live dashboard.

---

## Step 4 — connect the apps to your node

Two mechanisms, and you want both:

1. **A baked‑in seed** — your node's URL compiled into the apps. Always
   works, even if GitHub is down.
2. **A published seed list** — a `seeds.txt` you host on GitHub. The apps
   re‑read it every launch, so later you can add or remove public nodes
   **without shipping a new version**.

Set up the published list first. In your repo (on your PC), create
`seeds.txt` at the root:

```
# Kestrel public nodes — one per line.
http://YOUR_SERVER_IP:4444
```

Commit and push it, then open the file on github.com and click **Raw** to
get its URL (looks like
`https://raw.githubusercontent.com/<you>/<repo>/main/seeds.txt`).

Now wire both into all three apps at once (this is the command
`setup-vps.sh` printed for you):

```bash
./deploy/set-seeds.sh \
    --seed http://YOUR_SERVER_IP:4444 \
    --list https://raw.githubusercontent.com/<you>/<repo>/main/seeds.txt
```

It edits `SEED_NODES` and `SEED_LIST_URLS` in `kestrel-core`, `kestrel-miner`
and `kestrel-wallet` together, so they can't drift apart.

---

## Step 5 — ship

Rebuild the downloadable apps with the seeds baked in, then publish:

```bash
./build-apps.sh          # makes kestrel-core.zip / -miner.zip / -wallet.zip
git add -A && git commit -m "Point apps at the live network" && git push
```

Put the three zips where your download buttons expect them — the simplest
is a **GitHub Release**: on your repo, *Releases → Draft a new release →*
attach the three zips. (The website's download links are relative
`kestrel-*.zip`; if you host `kestrel-website.html` somewhere, drop the
zips next to it, or repoint the links at your Release URLs.)

Anyone who now downloads the miner or wallet lands straight on your
network — it fetches the seed list, connects to your anchor, downloads the
full ledger, and starts mining or shows a balance.

**You're live.** 🎉

---

## Growing the network later

As volunteers run their own reachable nodes (a home PC with TCP 4444
forwarded, or another VPS), just add their addresses to `seeds.txt` and
push — every app in the world picks up the change on its next launch, no
re‑release needed:

```
http://YOUR_SERVER_IP:4444
http://volunteer-node-ip:4444
```

Running a second anchor of your own in a different region is the single
best resilience upgrade: repeat Step 2 on another VPS and add its IP to
`seeds.txt`.

---

## Operating the node

```bash
sudo systemctl status kestrel-node     # health
sudo journalctl -u kestrel-node -f      # live logs (Ctrl‑C to exit)
sudo systemctl restart kestrel-node     # restart
sudo systemctl stop kestrel-node        # stop (won't restart until 'start')
sudo systemctl start kestrel-node       # start
```

**Updating to new code you've pushed:**

```bash
cd ~/kestrel && git pull
sudo bash deploy/setup-vps.sh           # re-syncs code, keeps your ledger
```

The installer preserves `/opt/kestrel/kestrel-core/kestrel-data`, so the
chain isn't re‑downloaded.

---

## Security notes (worth reading once)

- **No keys on the server.** The anchor runs the keyless `node` command —
  there is no wallet on the VPS to steal. Mine and hold from the desktop
  apps on your own machine.
- **Runs as an unprivileged user** (`kestrel`) under a hardened systemd
  unit (`ProtectSystem=strict`, `NoNewPrivileges`, private `/tmp`, write
  access to only its data directory).
- **The API is public and read‑only to the world.** Writes that matter
  (`/mine`) are loopback‑only. Serving the full chain as open JSON is the
  point — it's how explorers and wallets are built.
- **Basic server hygiene:** keep SSH on keys (not passwords), run
  `sudo apt-get update && sudo apt-get upgrade` occasionally, and consider
  `unattended-upgrades` for automatic security patches.
- The node tolerates internet noise (port scanners, half‑open connections)
  quietly by design — those aren't errors.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `curl YOUR_IP:4444/info` hangs from outside, works on the box | Cloud provider's security group is blocking — open inbound TCP 4444 in their console (Step 3). |
| `systemctl status` shows `failed` | `sudo journalctl -u kestrel-node -e` to see why. Usual causes: port already in use, or Python deps missing (`/opt/kestrel/venv/bin/pip install -r /opt/kestrel/kestrel-core/requirements.txt`). |
| Apps don't find the network | Confirm the Raw seed‑list URL loads in a browser and lists your node; re‑run `set-seeds.sh`; rebuild with `build-apps.sh`. |
| Dashboard shows 0 peers for a while | Normal for the very first node — peers appear once a second node connects. DHT discovery can take a minute. |
| Locked out over SSH after enabling the firewall | The installer allows SSH before enabling `ufw`, so this shouldn't happen. If it does, use the provider's web console / recovery console and run `sudo ufw allow OpenSSH`. |

That's everything. If you get stuck, `sudo journalctl -u kestrel-node -f`
almost always shows what's going on.
