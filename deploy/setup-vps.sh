#!/usr/bin/env bash
# =====================================================================
# Kestrel — 24/7 anchor node installer for a fresh Ubuntu/Debian VPS.
#
# What it does, in one command:
#   • installs Python + git + firewall tools
#   • creates a locked-down 'kestrel' system user (no login, no wallet)
#   • installs the node under /opt/kestrel in its own virtualenv
#   • writes a hardened systemd service so the node runs 24/7 and
#     restarts on crash AND on reboot
#   • opens TCP 4444 in the firewall (and keeps SSH open)
#   • starts the node and prints your public address + next steps
#
# This anchor node holds NO wallet and mines nothing — it just keeps the
# network reachable and serves the ledger, so nothing valuable sits on a
# public server. (You mine/hold from the desktop apps on your own PC.)
#
# Run it from a clone of your Kestrel repo on the VPS:
#     sudo bash deploy/setup-vps.sh
#
# Options:
#     --port N          listen port (default 4444)
#     --repo URL        git-clone the code from URL instead of copying
#                       the checkout this script sits in
#     --branch NAME     branch to clone (default main)
# =====================================================================
set -euo pipefail

PORT=4444
REPO_URL=""
BRANCH="main"
KUSER="kestrel"
PREFIX="/opt/kestrel"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)   PORT="${2:?}"; shift 2;;
    --repo)   REPO_URL="${2:?}"; shift 2;;
    --branch) BRANCH="${2:?}"; shift 2;;
    -h|--help) sed -n '2,40p' "$0"; exit 0;;
    *) echo "unknown option: $1"; exit 1;;
  esac
done

say(){ printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
ok(){  printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
die(){ printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Please run with sudo:  sudo bash deploy/setup-vps.sh"
command -v apt-get >/dev/null 2>&1 || die \
  "This installer targets Ubuntu/Debian (apt). For another distro, install
   python3/python3-venv/git yourself, then adapt deploy/kestrel-node.service."

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  die "--port must be 1..65535"
fi

# --- locate the source tree ------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"     # deploy/ lives at repo root
SRC=""
if [[ -z "$REPO_URL" && -d "$REPO_ROOT/kestrel-core/kestrel" ]]; then
  SRC="$REPO_ROOT/kestrel-core"
fi

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ufw curl rsync >/dev/null
ok "python3, git, ufw, curl ready"

say "Creating the '$KUSER' service user"
if ! id "$KUSER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$PREFIX" --shell /usr/sbin/nologin "$KUSER"
  ok "user '$KUSER' created (system account, no login)"
else
  ok "user '$KUSER' already exists"
fi
mkdir -p "$PREFIX"

say "Installing the Kestrel node code"
if [[ -n "$REPO_URL" ]]; then
  rm -rf "$PREFIX/src"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$PREFIX/src"
  [[ -d "$PREFIX/src/kestrel-core/kestrel" ]] || die \
    "Cloned repo has no kestrel-core/kestrel — check --repo / --branch"
  SRC="$PREFIX/src/kestrel-core"
elif [[ -n "$SRC" ]]; then
  ok "using the checkout this script sits in: $SRC"
else
  die "Could not find kestrel-core next to this script.
   Either run this from a clone of your repo, or pass --repo <git-url>."
fi

rm -rf "$PREFIX/kestrel-core"
rsync -a --delete \
  --exclude='kestrel-data/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='kestrel-wallet.json' --exclude='*-settings.json' \
  "$SRC/" "$PREFIX/kestrel-core/"
mkdir -p "$PREFIX/kestrel-core/kestrel-data"
ok "code at $PREFIX/kestrel-core"

say "Creating the Python virtualenv + installing dependencies"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/kestrel-core/requirements.txt"
ok "virtualenv ready ($("$PREFIX/venv/bin/python" -c 'import ecdsa;print("ecdsa",ecdsa.__version__)'))"

chown -R "$KUSER:$KUSER" "$PREFIX"

# --- systemd service --------------------------------------------------
say "Writing the systemd service (24/7, auto-restart, starts on boot)"
DATA_DIR="$PREFIX/kestrel-core/kestrel-data"
cat > /etc/systemd/system/kestrel-node.service <<UNIT
[Unit]
Description=Kestrel (KSL) anchor node
Documentation=https://github.com/  (your repo)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$KUSER
Group=$KUSER
WorkingDirectory=$PREFIX/kestrel-core
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$PREFIX/venv/bin/python -m kestrel.cli --data-dir $DATA_DIR node --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5
# --- hardening: the node needs to write only its own data directory ---
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
ProtectControlGroups=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
UNIT
ok "/etc/systemd/system/kestrel-node.service written"

# --- firewall ---------------------------------------------------------
say "Configuring the firewall (allowing SSH first, then Kestrel)"
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
yes | ufw enable >/dev/null 2>&1 || true
ok "firewall active — SSH (22) and Kestrel ($PORT/tcp) allowed"

# --- launch -----------------------------------------------------------
say "Starting the node"
systemctl daemon-reload
systemctl enable --now kestrel-node >/dev/null 2>&1
sleep 3

PUBIP="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
[[ -z "$PUBIP" ]] && PUBIP="$(hostname -I 2>/dev/null | awk '{print $1}')"

if systemctl is-active --quiet kestrel-node; then
  ok "kestrel-node is RUNNING"
else
  echo "  (node did not report active yet — see logs below)"
fi

# quick local health check
HEALTH="$(curl -fsS --max-time 4 "http://127.0.0.1:${PORT}/info" 2>/dev/null || true)"

cat <<DONE

──────────────────────────────────────────────────────────────────────
 Kestrel anchor node is installed.
──────────────────────────────────────────────────────────────────────
 Public address :  http://${PUBIP:-<your-server-ip>}:${PORT}
 Dashboard      :  open that URL in a browser
 Local health   :  ${HEALTH:-<node still starting; check logs>}

 Manage it:
   sudo systemctl status kestrel-node      # is it running?
   sudo journalctl -u kestrel-node -f       # live logs
   sudo systemctl restart kestrel-node      # restart
   sudo systemctl stop kestrel-node         # stop

 NEXT — connect the whole network to this node (run on your own PC,
 in your repo, then commit + rebuild the apps):

   ./deploy/set-seeds.sh \\
       --seed http://${PUBIP:-<your-server-ip>}:${PORT} \\
       --list https://raw.githubusercontent.com/<you>/<repo>/main/seeds.txt

 IMPORTANT: if your VPS provider has its own cloud firewall / security
 group (AWS, Oracle, GCP, Azure, Hetzner Cloud…), also allow inbound
 TCP ${PORT} there — the OS firewall above is not enough on its own.
 Verify from another machine:  curl http://${PUBIP:-<ip>}:${PORT}/info
──────────────────────────────────────────────────────────────────────
DONE
