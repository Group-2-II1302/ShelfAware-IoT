"""Periodic sampling of device metadata (battery, RSSI) for telemetry envelopes.

Process B samples these on a slow timer and attaches whatever's current at
drain time. This way:

- The outbox stays lean (no per-row metadata duplication).
- Battery reflects "now" rather than "the battery level when this reading was
  taken hours ago" (which can happen if the Pi was offline for a while).
- The backend gets one battery/RSSI snapshot per drain batch.

The backend's ``metadata`` schema is strict: only ``battery`` (int, 0-100) and
``rssi`` (int, -120 to 0) are allowed. Sending anything else (including null
fields, or unknown keys) 400s the entire batch. Don't add fields here without
coordinating with the backend first.

Hardware dependence
-------------------
- ``battery``: read from ``/sys/class/power_supply/BAT0/capacity``. The exact
  path depends on the UPS HAT (PiSugar, PiJuice, etc.). Mains-powered Pis
  return None and the field is omitted.
- ``rssi``: read from ``iwconfig wlan0`` output. Ethernet-only Pis return
  None and the field is omitted.

If both sensors return None, ``get_current()`` returns ``{}``, and the
drainer sends an empty metadata object on the wire (which the backend
accepts as a no-op).
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Adjust if your battery hardware uses a different sysfs path.
BATTERY_SYSFS_PATH = "/sys/class/power_supply/BAT0/capacity"

# How long to wait for `iwconfig` to respond before giving up on a sample.
RSSI_SUBPROCESS_TIMEOUT = 5.0

# Internal state. Re-discovered lazily on first failure of each sensor.
_battery_supported: Optional[bool] = None
_rssi_supported: Optional[bool] = None
_current_snapshot: dict[str, int] = {}


def _read_battery_pct() -> Optional[int]:
    """Read battery percentage from sysfs. Returns None if unavailable."""
    try:
        with open(BATTERY_SYSFS_PATH, encoding="utf-8") as f:
            text = f.read().strip()
        value = int(text)
        if 0 <= value <= 100:
            return value
        logger.warning("battery sysfs returned out-of-range value", extra={"value": value})
        return None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _read_rssi_dbm() -> Optional[int]:
    """Read WiFi signal level via iwconfig. Returns None if not on WiFi."""
    try:
        result = subprocess.run(
            ["iwconfig", "wlan0"],
            capture_output=True,
            text=True,
            timeout=RSSI_SUBPROCESS_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None

    # iwconfig output includes lines like: "Signal level=-65 dBm"
    match = re.search(r"Signal level=(-?\d+)\s*dBm", result.stdout)
    if not match:
        return None

    try:
        value = int(match.group(1))
    except ValueError:
        return None

    if -120 <= value <= 0:
        return value

    logger.warning("rssi out of expected range", extra={"value": value})
    return None


def sample_once() -> dict[str, int]:
    """Sample all currently-available metadata fields. Returns a snapshot dict.

    Sensors that return None on first call are remembered as unsupported and
    not retried (avoids repeated subprocess calls on Ethernet-only Pis).
    Reset state via :func:`reset_for_test` if needed.
    """
    global _battery_supported, _rssi_supported

    snapshot: dict[str, int] = {}

    if _battery_supported is not False:
        battery = _read_battery_pct()
        if battery is not None:
            snapshot["battery"] = battery
            if _battery_supported is None:
                _battery_supported = True
                logger.info("battery sensor detected", extra={"value": battery})
        elif _battery_supported is None:
            _battery_supported = False
            logger.info("battery sensor not available; omitting from metadata")

    if _rssi_supported is not False:
        rssi = _read_rssi_dbm()
        if rssi is not None:
            snapshot["rssi"] = rssi
            if _rssi_supported is None:
                _rssi_supported = True
                logger.info("rssi available", extra={"value": rssi})
        elif _rssi_supported is None:
            _rssi_supported = False
            logger.info("rssi not available (likely ethernet); omitting from metadata")

    return snapshot


def get_current() -> dict[str, int]:
    """Return a copy of the most-recently-sampled metadata. Empty if never sampled."""
    return dict(_current_snapshot)


async def run_sampler(interval: float, stop_event: asyncio.Event) -> None:
    """Background task: sample metadata every ``interval`` seconds until ``stop_event``.

    Errors during a single tick are logged and the loop continues with the
    previous snapshot intact. The previous snapshot is never cleared, so a
    transient sysfs read failure doesn't blank out the metadata.
    """
    global _current_snapshot

    logger.info("device metadata sampler started", extra={"interval": interval})

    while not stop_event.is_set():
        try:
            new_snapshot = sample_once()
            _current_snapshot = new_snapshot
            logger.debug("metadata sampled", extra={"snapshot": new_snapshot})
        except Exception:
            logger.exception("metadata sample failed; keeping previous snapshot")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("device metadata sampler stopped")


def reset_for_test() -> None:
    """Reset cached sensor-availability state. Test-only utility."""
    global _battery_supported, _rssi_supported, _current_snapshot
    _battery_supported = None
    _rssi_supported = None
    _current_snapshot = {}