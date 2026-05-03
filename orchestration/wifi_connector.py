#!/usr/bin/env python3
"""
wifi_connector.py  –  ShelfAware WiFi Credential Manager
═════════════════════════════════════════════════════════
Called by Process C (the setup web server) when the user submits
their WiFi credentials via the captive portal.

DEV_MODE  (SHELFAWARE_DEV=1):
  All nmcli calls are short-circuited. apply_wifi_credentials()
  logs its intended action and returns ConnectResult.SUCCESS.
  rollback_to_ap() logs and returns True without touching NM.
  Safe to run over Tailscale / mobile hotspot during development.

Defensive contract:
  • Attempts STA connection within CONNECT_TIMEOUT seconds.
  • If not confirmed at L3 within that window → auto-rollback to AP.
  • Thread-safe: module-level lock prevents overlapping apply() calls.
  • rollback_to_ap() is idempotent and callable from any thread.
"""

import logging
import os
import subprocess
import threading
import time
from enum import Enum, auto
from typing import Optional

log = logging.getLogger("shelfaware.wifi_connector")

# ──────────────────────────────────────────────
# DEV MODE  –  set SHELFAWARE_DEV=1 to activate
# ──────────────────────────────────────────────
DEV_MODE = os.environ.get("SHELFAWARE_DEV", "0") == "1"

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
AP_PROFILE_NAME  = "ShelfAware_Setup"
STA_PROFILE_NAME = "ShelfAware_STA"
IFACE            = "wlan0"
CONNECT_TIMEOUT  = 60
VERIFY_POLL_SEC  = 3
NM_CMD_TIMEOUT   = 15

_apply_lock = threading.Lock()


class ConnectResult(Enum):
    SUCCESS = auto()
    TIMEOUT = auto()   # bad password / SSID not found → rolled back to AP
    ERROR   = auto()   # unexpected failure


# ──────────────────────────────────────────────
# Low-level nmcli helpers
# ──────────────────────────────────────────────

def _nmcli(*args: str, timeout: int = NM_CMD_TIMEOUT) -> subprocess.CompletedProcess:
    cmd = ["nmcli"] + list(args)
    log.debug("nmcli: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        log.warning("nmcli (rc=%d): %s", result.returncode, result.stderr.strip())
    return result


def _connection_exists(profile: str) -> bool:
    r = _nmcli("connection", "show", profile)
    return r.returncode == 0


def _is_connected_to_sta() -> bool:
    """Returns True if wlan0 is fully connected on the STA profile."""
    r = _nmcli("--terse", "--fields", "DEVICE,STATE,CONNECTION", "device", "status")
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == IFACE:
            if parts[1].lower() == "connected" and parts[2] == STA_PROFILE_NAME:
                return True
    return False


def _create_or_update_sta_profile(ssid: str, password: str) -> bool:
    if _connection_exists(STA_PROFILE_NAME):
        log.info("Removing stale STA profile …")
        _nmcli("connection", "delete", STA_PROFILE_NAME)

    log.info("Creating STA profile for SSID '%s' …", ssid)
    r = _nmcli(
        "connection", "add",
        "type",        "wifi",
        "ifname",      IFACE,
        "con-name",    STA_PROFILE_NAME,
        "autoconnect", "yes",
        "ssid",        ssid,
        "--",
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk",      password,
        "ipv4.method",       "auto",
        "ipv6.method",       "disabled",
        timeout=NM_CMD_TIMEOUT,
    )
    return r.returncode == 0


def _activate_sta_profile() -> bool:
    log.info("Activating STA profile …")
    try:
        _nmcli("connection", "down", AP_PROFILE_NAME)
    except Exception:
        pass

    r = _nmcli(
        "--timeout", str(CONNECT_TIMEOUT),
        "connection", "up", STA_PROFILE_NAME,
        timeout=CONNECT_TIMEOUT + 5,
    )
    return r.returncode == 0


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def rollback_to_ap() -> bool:
    """
    Tear down STA and restore the AP hotspot.
    Idempotent. Safe to call from any thread.
    """
    if DEV_MODE:
        log.info(
            "[DEV MODE] Skipping AP rollback — would run: "
            "nmcli connection down '%s' && nmcli connection up '%s'. "
            "Returning True.", STA_PROFILE_NAME, AP_PROFILE_NAME
        )
        return True

    log.warning("ROLLBACK: reverting to AP mode …")

    try:
        _nmcli("connection", "down", STA_PROFILE_NAME)
    except Exception:
        pass

    try:
        r = _nmcli("connection", "up", AP_PROFILE_NAME, timeout=20)
        if r.returncode == 0:
            log.info("ROLLBACK: AP mode restored successfully.")
            return True
        log.error("ROLLBACK: nmcli returned non-zero bringing up AP.")
    except subprocess.TimeoutExpired:
        log.error("ROLLBACK: timed out activating AP profile.")
    except Exception as exc:
        log.error("ROLLBACK: unexpected error: %s", exc, exc_info=True)

    return False


def apply_wifi_credentials(
    ssid: str,
    password: str,
    timeout: int = CONNECT_TIMEOUT,
) -> ConnectResult:
    """
    Thread-safe entry point for Process C.

    In DEV_MODE: logs the intended action, returns SUCCESS immediately.
    In production: creates STA profile, activates it, polls for L3
    confirmation, rolls back to AP if not confirmed within `timeout`.
    """
    if not ssid or not password:
        log.error("apply_wifi_credentials: ssid and password must be non-empty.")
        return ConnectResult.ERROR

    if DEV_MODE:
        log.info(
            "[DEV MODE] Skipping real WiFi connect — would attempt to join "
            "SSID '%s' and roll back to '%s' on failure. Returning SUCCESS.",
            ssid, AP_PROFILE_NAME,
        )
        return ConnectResult.SUCCESS

    with _apply_lock:
        log.info("Applying WiFi credentials for SSID '%s' …", ssid)
        t_start = time.monotonic()

        try:
            if not _create_or_update_sta_profile(ssid, password):
                log.error("Failed to create STA profile.")
                rollback_to_ap()
                return ConnectResult.ERROR
        except Exception as exc:
            log.error("STA profile creation exception: %s", exc, exc_info=True)
            rollback_to_ap()
            return ConnectResult.ERROR

        try:
            activated = _activate_sta_profile()
        except subprocess.TimeoutExpired:
            log.warning("nmcli connect timed out – rolling back.")
            rollback_to_ap()
            return ConnectResult.TIMEOUT
        except Exception as exc:
            log.error("STA activation exception: %s", exc, exc_info=True)
            rollback_to_ap()
            return ConnectResult.ERROR

        if not activated:
            log.warning("nmcli returned non-zero on connect – rolling back.")
            rollback_to_ap()
            return ConnectResult.TIMEOUT

        log.info("Verifying L3 connectivity (polling for up to %d s) …", timeout)
        deadline = t_start + timeout

        while time.monotonic() < deadline:
            if _is_connected_to_sta():
                elapsed = time.monotonic() - t_start
                log.info("Connected to '%s' in %.1f s.", ssid, elapsed)
                return ConnectResult.SUCCESS
            log.debug("Not yet connected. %.0f s remaining …",
                      deadline - time.monotonic())
            time.sleep(VERIFY_POLL_SEC)

        log.warning(
            "Could not verify connection to '%s' within %d s – rolling back.",
            ssid, timeout,
        )
        rollback_to_ap()
        return ConnectResult.TIMEOUT


# ──────────────────────────────────────────────
# CLI smoke-test
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <SSID> <password>")
        print(f"       SHELFAWARE_DEV=1 {sys.argv[0]} <SSID> <password>  (safe/mock)")
        sys.exit(1)

    result = apply_wifi_credentials(sys.argv[1], sys.argv[2])
    print(f"\nResult: {result.name}")
    sys.exit(0 if result == ConnectResult.SUCCESS else 1)
