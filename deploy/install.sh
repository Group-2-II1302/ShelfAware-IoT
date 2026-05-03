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
pip3 install --break-system-packages --quiet \
    adafruit-blinka \
    adafruit-circuitpython-ads1x15

# ── 3. Deploy application files ───────────────────────────────────────────────
info "Deploying application to $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR/orchestration"
mkdir -p "$INSTALL_DIR/process-a"
mkdir -p "$INSTALL_DIR/process-b/process_b"
mkdir -p "$INSTALL_DIR/process-c"

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

# Process B
cp -r "$REPO_ROOT/process-b/process_b/." "$INSTALL_DIR/process-b/process_b/"

# Process C (placeholder until ready)
if [[ -f "$REPO_ROOT/process-c/process_c.py" ]]; then
    cp "$REPO_ROOT/process-c/process_c.py" "$INSTALL_DIR/process-c/"
    chmod 755 "$INSTALL_DIR/process-c/process_c.py"
else
    warn "process_c.py not found – creating placeholder stub."
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
