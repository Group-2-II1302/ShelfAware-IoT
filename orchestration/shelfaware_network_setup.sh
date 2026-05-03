#!/usr/bin/env bash
# =============================================================================
# shelfaware_network_setup.sh
# =============================================================================
# Run ONCE during factory provisioning (or first-time setup) on the Pi.
# Idempotent – safe to re-run.
#
# ⚠️  TAILSCALE WARNING: This script calls nmcli and will modify
#     NetworkManager profiles. Do NOT run this over a remote Tailscale /
#     hotspot session unless you are prepared to lose the connection.
#     Run from a local terminal or physical keyboard/monitor only.
#
# What it does:
#   1. Ensures wlan0 is managed by NetworkManager.
#   2. Creates (or recreates) the "ShelfAware_Setup" AP hotspot profile.
#
# Usage:
#   sudo bash shelfaware_network_setup.sh
# =============================================================================

set -euo pipefail

AP_PROFILE="ShelfAware_Setup"
AP_SSID="ShelfAware_Setup"
AP_PASSWORD="shelfaware123"   # ← change before shipping product
AP_IP="192.168.4.1/24"
IFACE="wlan0"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fatal() { echo -e "${RED}[FATAL]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fatal "Must be run as root (sudo)."

info "Ensuring NetworkManager manages $IFACE …"
nmcli device set "$IFACE" managed yes \
    || warn "Could not set $IFACE managed (may already be managed)."

if nmcli connection show "$AP_PROFILE" &>/dev/null; then
    warn "Profile '$AP_PROFILE' already exists – removing and recreating."
    nmcli connection delete "$AP_PROFILE"
fi

info "Creating AP hotspot profile: $AP_PROFILE …"
nmcli connection add \
    type wifi \
    ifname "$IFACE" \
    con-name "$AP_PROFILE" \
    autoconnect no \
    ssid "$AP_SSID" \
    -- \
    wifi.mode ap \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$AP_PASSWORD" \
    ipv4.method shared \
    ipv4.addresses "$AP_IP" \
    ipv6.method disabled

info "AP profile created successfully."
nmcli connection show "$AP_PROFILE"

info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Provisioning complete."
info ""
info "To test AP manually (⚠️  will drop Tailscale):"
info "  sudo nmcli connection up '$AP_PROFILE'"
info ""
info "To restore STA / Tailscale access:"
info "  sudo nmcli connection down '$AP_PROFILE'"
info "  sudo nmcli connection up <your-wifi-profile>"
info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

