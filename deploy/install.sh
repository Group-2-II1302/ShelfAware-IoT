#!/usr/bin/env bash
# =============================================================================
# install.sh  –  SmartShelf deployment script
# =============================================================================
# Run once on a freshly flashed Raspberry Pi OS Bookworm (headless).
# Idempotent – safe to re-run.
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
INSTALL_DIR="/opt/smartshelf"
SERVICE_FILE="smartshelf-orchestrator.service"

# ── 1. Dependencies ──────────────────────────────────────────────────────────
info "Installing system dependencies …"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip network-manager iproute2 iputils-ping

# ── 2. Install Python deps ────────────────────────────────────────────────────
info "Installing Python packages …"
pip3 install --break-system-packages --quiet psutil

# ── 3. Deploy application files ───────────────────────────────────────────────
info "Deploying application to $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/orchestrator.py"   "$INSTALL_DIR/"
cp "$SCRIPT_DIR/wifi_connector.py" "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR/orchestrator.py"
chmod 644 "$INSTALL_DIR/wifi_connector.py"

# Placeholder stubs so the service can start even without real processes
for proc in process_a process_b process_c; do
    target="$INSTALL_DIR/${proc}.py"
    if [[ ! -f "$target" ]]; then
        warn "Stub: $target not found – creating placeholder."
        cat > "$target" <<STUB
#!/usr/bin/env python3
import time, logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("${proc}")
log.info("${proc} placeholder running – replace with real implementation.")
while True:
    time.sleep(60)
STUB
        chmod 755 "$target"
    fi
done

# ── 4. State directory ────────────────────────────────────────────────────────
mkdir -p /var/lib/smartshelf
chmod 750 /var/lib/smartshelf

# ── 5. Log directory ─────────────────────────────────────────────────────────
touch /var/log/smartshelf_orchestrator.log
chmod 640 /var/log/smartshelf_orchestrator.log

# ── 6. NetworkManager – ensure it's running ───────────────────────────────────
info "Enabling NetworkManager …"
systemctl enable NetworkManager
systemctl start  NetworkManager

# Wait for NM to be available
for i in {1..10}; do
    nmcli general status &>/dev/null && break
    warn "Waiting for nmcli ($i/10) …"; sleep 2
done

# ── 7. Pre-configure the AP hotspot profile ───────────────────────────────────
info "Configuring AP hotspot profile …"
bash "$SCRIPT_DIR/smartshelf_network_setup.sh"

# ── 8. Install and enable the systemd service ────────────────────────────────
info "Installing systemd service …"
cp "$SCRIPT_DIR/$SERVICE_FILE" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_FILE"

info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Installation complete."
info ""
info "Start the service now:  systemctl start smartshelf-orchestrator"
info "Watch live logs:        journalctl -fu smartshelf-orchestrator"
info "Or tail the file:       tail -f /var/log/smartshelf_orchestrator.log"
info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
