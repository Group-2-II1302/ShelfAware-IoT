#!/usr/bin/env bash
# =============================================================================
# install.sh  –  ShelfAware deployment script
# =============================================================================
# Run once on a freshly flashed Raspberry Pi OS Bookworm (headless).
# Idempotent – safe to re-run.
#
# ⚠️  TAILSCALE WARNING: This script calls nmcli and systemctl which will
#     modify NetworkManager and may sever your remote connection.
#     Run from a local terminal or physical keyboard/monitor only.
#
# Usage:
#   sudo bash install.sh
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fatal() { echo -e "${RED}[FATAL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fatal "Must be run as root (sudo bash install.sh)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="/opt/shelfaware"
SERVICE_FILE="shelfaware-orchestrator.service"

# ── 1. Dependencies ──────────────────────────────────────────────────────────
info "Installing system dependencies …"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip network-manager iproute2 iputils-ping

# ── 2. Install Python deps ────────────────────────────────────────────────────
info "Installing Python packages …"

# Process A: hardware drivers
pip3 install --break-system-packages --quiet \
    adafruit-blinka \
    adafruit-circuitpython-ads1x15

# Process B's runtime deps are installed in section 3 below, AFTER its
# source has been staged to $INSTALL_DIR. Installing here would either
# pin to the repo working tree (wrong: /opt is the source of truth in
# production) or fail (no pyproject.toml in $INSTALL_DIR yet).

# ── 3. Deploy application files ───────────────────────────────────────────────
info "Deploying application to $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR/orchestration"
mkdir -p "$INSTALL_DIR/process-a"
mkdir -p "$INSTALL_DIR/process-b/process_b"
mkdir -p "$INSTALL_DIR/process-c/process_c"

# Orchestration layer
cp "$REPO_ROOT/orchestration/orchestrator.py"              "$INSTALL_DIR/orchestration/"
cp "$REPO_ROOT/orchestration/wifi_connector.py"            "$INSTALL_DIR/orchestration/"
cp "$REPO_ROOT/orchestration/shelfaware_network_setup.sh"  "$INSTALL_DIR/orchestration/"
chmod 755 "$INSTALL_DIR/orchestration/orchestrator.py"
chmod 644 "$INSTALL_DIR/orchestration/wifi_connector.py"
chmod 755 "$INSTALL_DIR/orchestration/shelfaware_network_setup.sh"

# Process A
cp "$REPO_ROOT/process-a/process_A.py" "$INSTALL_DIR/process-a/"
chmod 755 "$INSTALL_DIR/process-a/process_A.py"

# Process B: stage source AND pyproject so we can pip install from $INSTALL_DIR
cp -r "$REPO_ROOT/process-b/process_b/." "$INSTALL_DIR/process-b/process_b/"
cp    "$REPO_ROOT/process-b/pyproject.toml" "$INSTALL_DIR/process-b/"
if [[ -f "$REPO_ROOT/process-b/README.md" ]]; then
    cp "$REPO_ROOT/process-b/README.md" "$INSTALL_DIR/process-b/"
fi

info "Installing Process B's Python dependencies …"
pip3 install --break-system-packages --quiet -e "$INSTALL_DIR/process-b"

# Process C: stage source AND pyproject so we can pip install from $INSTALL_DIR.
# The orchestrator launches it via /opt/shelfaware/process-c/process_c.py
# (a small shim that forwards to process_c.main:run on the installed package).
if [[ -d "$REPO_ROOT/process-c/process_c" ]]; then
    cp -r "$REPO_ROOT/process-c/process_c/." "$INSTALL_DIR/process-c/process_c/"
    cp    "$REPO_ROOT/process-c/pyproject.toml" "$INSTALL_DIR/process-c/"
    if [[ -f "$REPO_ROOT/process-c/README.md" ]]; then
        cp "$REPO_ROOT/process-c/README.md" "$INSTALL_DIR/process-c/"
    fi
    cp "$REPO_ROOT/process-c/process_c.py" "$INSTALL_DIR/process-c/process_c.py"
    chmod 755 "$INSTALL_DIR/process-c/process_c.py"

    info "Installing Process C's Python dependencies …"
    pip3 install --break-system-packages --quiet -e "$INSTALL_DIR/process-c"
else
    warn "process-c package not found – creating placeholder stub."
    cat > "$INSTALL_DIR/process-c/process_c.py" <<STUB
#!/usr/bin/env python3
import time, logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("process_c")
log.info("process_c placeholder – replace with real implementation.")
while True:
    time.sleep(60)
STUB
    chmod 755 "$INSTALL_DIR/process-c/process_c.py"
fi

# ── 4. State and log directories ──────────────────────────────────────────────
mkdir -p /var/lib/shelfaware
chmod 750 /var/lib/shelfaware

touch /var/log/shelfaware_orchestrator.log
chmod 640 /var/log/shelfaware_orchestrator.log

# ── 4b. Process B environment file ────────────────────────────────────────────
# Process B reads PI_API_KEY, BACKEND_URL, SHELF_IDS, DB_PATH, etc. from env.
# The systemd unit loads them from /etc/shelfaware/process-b.env.
# If the file doesn't already exist, drop a template that the operator must
# fill in BEFORE the orchestrator first launches Process B.
mkdir -p /etc/shelfaware
chmod 750 /etc/shelfaware

PROCESS_B_ENV="/etc/shelfaware/process-b.env"
if [[ ! -f "$PROCESS_B_ENV" ]]; then
    info "Creating Process B env template at $PROCESS_B_ENV …"
    if [[ -f "$REPO_ROOT/process-b/deploy/process-b.env.example" ]]; then
        cp "$REPO_ROOT/process-b/deploy/process-b.env.example" "$PROCESS_B_ENV"
    else
        cat > "$PROCESS_B_ENV" <<'ENV_TEMPLATE'
# Process B environment file. Loaded by the orchestrator's systemd unit.
# Fill in real values before starting shelfaware-orchestrator.
PI_API_KEY=changeme
BACKEND_URL=https://shelfaware-backend.example.workers.dev
SHELF_IDS=00000000-0000-0000-0000-000000000000
DB_PATH=/var/lib/shelfaware/process-b.sqlite
UDP_LISTEN_HOST=127.0.0.1
UDP_LISTEN_PORT=5005
PROC_A_CONTROL_HOST=127.0.0.1
PROC_A_CONTROL_PORT=5006
ENV_TEMPLATE
    fi
    chmod 640 "$PROCESS_B_ENV"
    warn "Edit $PROCESS_B_ENV with real values before starting the service."
else
    info "Process B env file already exists at $PROCESS_B_ENV – leaving untouched."
fi

# ── 5. NetworkManager – ensure it's running ───────────────────────────────────
info "Enabling NetworkManager …"
systemctl enable NetworkManager
systemctl start  NetworkManager

for i in {1..10}; do
    nmcli general status &>/dev/null && break
    warn "Waiting for nmcli ($i/10) …"; sleep 2
done

# ── 6. Pre-configure the AP hotspot profile ───────────────────────────────────
info "Configuring AP hotspot profile …"
bash "$INSTALL_DIR/orchestration/shelfaware_network_setup.sh"

# ── 7. Install and enable the systemd service ─────────────────────────────────
info "Installing systemd service …"
cp "$SCRIPT_DIR/$SERVICE_FILE" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_FILE"

info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Installation complete."
info ""
info "Start the service now:  systemctl start shelfaware-orchestrator"
info "Watch live logs:        journalctl -fu shelfaware-orchestrator"
info "Or tail the file:       tail -f /var/log/shelfaware_orchestrator.log"
info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
