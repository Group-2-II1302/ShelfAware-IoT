#!/usr/bin/env python3
"""
process_A.py  –  ShelfAware Sensor Reader  (Dual-Zone Edition)
==============================================================
Zone 1: ADC 0x48  →  Channels 0, 1, 2  →  scale_index 0, 1, 2
Zone 2: ADC 0x49  →  Channels 0, 1, 2  →  scale_index 3, 4, 5

Backend contract (IMMUTABLE):
  scale_index is the position in SENSOR_SEQUENCE.
  Reordering SENSOR_SEQUENCE breaks Process B.  Do not reorder.

Polling strategy (Delta-Based Activity Trigger):
  ACTIVE  – a sensor delta > change_threshold_g was detected within
            the last active_cooldown_s seconds. Poll at interval_active.
  IDLE    – no significant delta detected recently. Poll at interval_idle.
            Still reads all sensors for drift compensation, just less often.

  A shelf with 5kg sitting still is IDLE.
  A shelf where a user is adding/removing items is ACTIVE.

Fault model:
  • If an entire ADC is missing at boot (e.g. 0x49 wire loose), that
    chip is marked FAULTED and its three channels emit null payloads
    with an "adc_fault" flag every cycle so Process B can distinguish
    "sensor reads zero" from "sensor is dead".
  • If a single read fails mid-run, the error is logged and that
    sample is skipped; the loop continues for all other sensors.
  • If the I2C bus itself dies, the exception propagates to main()
    which logs it and exits cleanly (letting systemd/orchestrator
    decide whether to restart).
"""

import json
import logging
import os
import socket
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Dict, Optional

import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def _build_logger() -> logging.Logger:
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler = RotatingFileHandler(
        "/var/log/shelfaware_process_a.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)

    log = logging.getLogger("shelfaware.process_a")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.addHandler(console)
    return log


log = _build_logger()

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SensorConfig:
    adc_channel:   int
    adc_address:   int   = 0x48
    a_const:       float = 14.56
    b_const:       float = 1.05
    full_weight_g: float = 1000.0


@dataclass
class SystemConfig:
    shelf_id:            str   = "62d1eea1-6253-4157-a663-8f099eb3a9fe"
    udp_ip:              str   = "127.0.0.1"
    udp_port:            int   = 5005
    deadzone_adc:        int   = 150
    window_size:         int   = 5
    drift_margin:        float = 0.15
    near_zero_threshold: int   = 300
    storage_path:        str   = "baselines.json"
    fault_threshold:     int   = 5

    # ── Delta-Based Activity Trigger ──────────────────────────────────────────
    # Fast polling rate when user interaction is detected
    interval_active:     float = 0.5
    # Slow polling rate when shelf is mechanically idle (items just resting)
    interval_idle:       float = 10.0
    # How long (seconds) to stay in ACTIVE mode after the last detected delta
    active_cooldown_s:   float = 10.0
    # Minimum gram change required to count as user interaction (filters drift)
    change_threshold_g:  float = 15.0


CONFIG = SystemConfig()

# ──────────────────────────────────────────────────────────────────────────────
# Sensor map  (insertion order is the scale_index contract)
# ──────────────────────────────────────────────────────────────────────────────

SENSOR_MAP: Dict[str, SensorConfig] = {
    # ── Zone 1  –  ADC 0x48  –  scale_index 0, 1, 2 ─────────────────────────
    "Z1_A0": SensorConfig(adc_channel=0, adc_address=0x48),
    "Z1_A1": SensorConfig(adc_channel=1, adc_address=0x48),
    "Z1_A2": SensorConfig(adc_channel=2, adc_address=0x48),
    # ── Zone 2  –  ADC 0x49  –  scale_index 3, 4, 5 ─────────────────────────
    "Z2_A0": SensorConfig(adc_channel=0, adc_address=0x49),
    "Z2_A1": SensorConfig(adc_channel=1, adc_address=0x49),
    "Z2_A2": SensorConfig(adc_channel=2, adc_address=0x49),
}

# Explicit ordered sequence – this is the SINGLE source of truth for
# scale_index assignment.  enumerate(SENSOR_SEQUENCE) → (scale_index, s_id).
# Never derive this from dict.keys() alone; be explicit.
SENSOR_SEQUENCE = [
    "Z1_A0",  # scale_index 0
    "Z1_A1",  # scale_index 1
    "Z1_A2",  # scale_index 2
    "Z2_A0",  # scale_index 3
    "Z2_A1",  # scale_index 4
    "Z2_A2",  # scale_index 5
]

# Validate at import time – catches typos before the Pi boots
assert set(SENSOR_SEQUENCE) == set(SENSOR_MAP.keys()), (
    "SENSOR_SEQUENCE and SENSOR_MAP are out of sync. "
    "Every sensor must appear in both, exactly once."
)
assert len(SENSOR_SEQUENCE) == len(set(SENSOR_SEQUENCE)), (
    "SENSOR_SEQUENCE contains duplicates."
)

# ──────────────────────────────────────────────────────────────────────────────
# Hardware manager  (handles multiple I2C addresses)
# ──────────────────────────────────────────────────────────────────────────────

class HardwareManager:
    """
    Singleton-style I2C / ADC factory.

    One shared I2C bus.  One ADS1115 instance per unique address.
    Addresses that fail to initialise are placed in _faulted_addresses
    so the rest of the system degrades gracefully.
    """

    _i2c: Optional[busio.I2C]              = None
    _ads_instances: Dict[int, ADS.ADS1115] = {}
    _faulted_addresses: set                = set()

    @classmethod
    def _ensure_i2c(cls) -> None:
        if cls._i2c is None:
            cls._i2c = busio.I2C(board.SCL, board.SDA)
            log.info("I2C bus initialised.")

    @classmethod
    def get_adc_channel(cls, address: int, channel_index: int) -> Optional[AnalogIn]:
        if address in cls._faulted_addresses:
            return None

        cls._ensure_i2c()

        if address not in cls._ads_instances:
            try:
                cls._ads_instances[address] = ADS.ADS1115(
                    cls._i2c, address=address
                )
                log.info("ADS1115 at 0x%02X initialised.", address)
            except Exception as exc:
                log.error(
                    "Failed to initialise ADS1115 at 0x%02X: %s. "
                    "All channels on this ADC will be marked faulted.",
                    address, exc,
                )
                cls._faulted_addresses.add(address)
                return None

        return AnalogIn(cls._ads_instances[address], channel_index)

    @classmethod
    def is_address_faulted(cls, address: int) -> bool:
        return address in cls._faulted_addresses


# ──────────────────────────────────────────────────────────────────────────────
# Signal processing
# ──────────────────────────────────────────────────────────────────────────────

class MovingAverage:
    def __init__(self, size: int):
        self.buffer      = deque(maxlen=size)
        self.running_sum = 0.0

    def add(self, value: float) -> float:
        if len(self.buffer) == self.buffer.maxlen:
            self.running_sum -= self.buffer[0]
        self.buffer.append(value)
        self.running_sum += value
        return self.running_sum / len(self.buffer)


class PersistentDriftCompensator:
    """
    Loads and saves per-sensor baselines to disk so zero-point drift
    survives reboots.  DO NOT MODIFY – untouched per spec.
    """

    def __init__(self, sensor_id: str):
        self.sensor_id = sensor_id
        self._baseline: Optional[float] = self._load_baseline()

    def _load_baseline(self) -> Optional[float]:
        if os.path.exists(CONFIG.storage_path):
            try:
                with open(CONFIG.storage_path, "r") as f:
                    return json.load(f).get(self.sensor_id)
            except Exception as exc:
                log.warning("Could not load baseline for %s: %s",
                            self.sensor_id, exc)
        return None

    def _save_baseline(self, value: float) -> None:
        data: dict = {}
        if os.path.exists(CONFIG.storage_path):
            try:
                with open(CONFIG.storage_path, "r") as f:
                    data = json.load(f)
            except Exception as exc:
                log.warning("Could not read baseline file before write: %s", exc)
        data[self.sensor_id] = value
        try:
            with open(CONFIG.storage_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            log.error("Could not save baseline for %s: %s", self.sensor_id, exc)

    def correct(self, filtered_adc: float) -> float:
        if self._baseline is None:
            self._baseline = filtered_adc
            self._save_baseline(filtered_adc)
            log.info("Baseline captured for %s: %.1f", self.sensor_id, filtered_adc)
            return 0.0

        if filtered_adc <= CONFIG.near_zero_threshold:
            drift_delta = abs(filtered_adc - self._baseline)
            if (drift_delta / (self._baseline + 1)) > CONFIG.drift_margin:
                log.debug("Drift corrected for %s: %.1f → %.1f",
                          self.sensor_id, self._baseline, filtered_adc)
                self._baseline = filtered_adc
                self._save_baseline(filtered_adc)

        return max(0.0, filtered_adc - self._baseline)


def adc_to_grams(adc: float, cfg: SensorConfig) -> float:
    if adc <= 0:
        return 0.0
    return (adc / cfg.a_const) ** (1.0 / cfg.b_const)


# ──────────────────────────────────────────────────────────────────────────────
# Per-sensor fault tracker
# ──────────────────────────────────────────────────────────────────────────────

class SensorFaultTracker:
    def __init__(self, sensor_id: str, threshold: int = CONFIG.fault_threshold):
        self.sensor_id    = sensor_id
        self.threshold    = threshold
        self._consecutive = 0
        self._is_faulted  = False

    def record_success(self) -> None:
        if self._is_faulted:
            log.info("Sensor %s recovered after fault.", self.sensor_id)
            self._is_faulted = False
        self._consecutive = 0

    def record_failure(self, exc: Exception) -> None:
        self._consecutive += 1
        if self._consecutive >= self.threshold and not self._is_faulted:
            log.error(
                "Sensor %s faulted after %d consecutive errors. Last: %s",
                self.sensor_id, self._consecutive, exc,
            )
            self._is_faulted = True
        elif not self._is_faulted:
            log.warning("Sensor %s read error (%d/%d): %s",
                        self.sensor_id, self._consecutive, self.threshold, exc)

    @property
    def is_faulted(self) -> bool:
        return self._is_faulted


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("PROCESS A STARTING  |  SHELF: %s", CONFIG.shelf_id)
    log.info("Sensor sequence (scale_index order): %s", SENSOR_SEQUENCE)
    log.info(
        "Delta-based polling | active=%.1fs  idle=%.1fs  "
        "cooldown=%.1fs  threshold=%.1fg",
        CONFIG.interval_active, CONFIG.interval_idle,
        CONFIG.active_cooldown_s, CONFIG.change_threshold_g,
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ── Initialise per-sensor objects ─────────────────────────────────────────
    filters:        Dict[str, MovingAverage]              = {}
    compensators:   Dict[str, PersistentDriftCompensator] = {}
    fault_trackers: Dict[str, SensorFaultTracker]         = {}
    channels:       Dict[str, Optional[AnalogIn]]         = {}

    for s_id, cfg in SENSOR_MAP.items():
        filters[s_id]        = MovingAverage(CONFIG.window_size)
        compensators[s_id]   = PersistentDriftCompensator(s_id)
        fault_trackers[s_id] = SensorFaultTracker(s_id)
        channels[s_id]       = HardwareManager.get_adc_channel(
            cfg.adc_address, cfg.adc_channel
        )
        status = "OK" if channels[s_id] is not None else "FAULTED (ADC init failed)"
        log.info("Channel %s at 0x%02X ch%d → %s",
                 s_id, cfg.adc_address, cfg.adc_channel, status)

    log.info("Initialisation complete. Entering read loop.")

    # ── Delta-Based Activity State ─────────────────────────────────────────────
    # Tracks the last confirmed gram reading per sensor.
    # Initialised to 0.0 — first cycle will always compute a delta from zero,
    # but only triggers ACTIVE if that delta exceeds change_threshold_g.
    previous_grams: Dict[str, float] = {s: 0.0 for s in SENSOR_SEQUENCE}

    # Timestamp of the last sensor delta that exceeded change_threshold_g.
    # Initialised to 0.0 so the system starts in IDLE mode on boot.
    last_activity_ts: float = 0.0

    # ── Read loop ─────────────────────────────────────────────────────────────
    while True:
        timestamp = datetime.now(timezone.utc).isoformat()

        # Determine current mode BEFORE processing this cycle's readings.
        # This ensures the print statements reflect the mode that governed
        # the sleep we just woke up from, which is what Furkan wants to see.
        is_active = (time.time() - last_activity_ts) < CONFIG.active_cooldown_s
        mode_label = "ACTIVE" if is_active else "IDLE  "

        for scale_index, s_id in enumerate(SENSOR_SEQUENCE):
            cfg     = SENSOR_MAP[s_id]
            channel = channels[s_id]
            zone    = "Z1" if cfg.adc_address == 0x48 else "Z2"

            # ── ADC-level fault ───────────────────────────────────────────────
            if channel is None or HardwareManager.is_address_faulted(cfg.adc_address):
                payload = {
                    "est_grams":   None,
                    "shelf_id":    CONFIG.shelf_id,
                    "sampled_at":  timestamp,
                    "scale_index": scale_index,
                    "adc_fault":   True,
                    "sensor_id":   s_id,
                }
                sock.sendto(
                    json.dumps(payload).encode(),
                    (CONFIG.udp_ip, CONFIG.udp_port),
                )
                log.debug("[%s] scale_index=%d  ADC FAULTED – null payload sent.",
                          s_id, scale_index)
                continue

            # ── Normal read path ──────────────────────────────────────────────
            try:
                raw_val   = channel.value
                clean     = 0 if raw_val < CONFIG.deadzone_adc else raw_val
                smoothed  = filters[s_id].add(float(clean))
                corrected = compensators[s_id].correct(smoothed)
                grams     = adc_to_grams(corrected, cfg)

                fault_trackers[s_id].record_success()

                # ── Delta-Based Activity Trigger ──────────────────────────────
                delta = abs(grams - previous_grams[s_id])

                if delta > CONFIG.change_threshold_g:
                    # Significant change detected — user is interacting.
                    # Reset the cooldown clock.
                    last_activity_ts = time.time()
                    log.debug(
                        "[%s] Activity detected: delta=%.1fg "
                        "(%.1f → %.1f). Cooldown reset.",
                        s_id, delta, previous_grams[s_id], grams,
                    )

                # Update previous reading for next cycle's delta calculation
                previous_grams[s_id] = grams
                # ── End Delta Logic ───────────────────────────────────────────

                # Terminal diagnostic — mode label visible for Furkan's testing
                print(
                    f"[{mode_label}] [{s_id}({zone}|idx={scale_index})] "
                    f"ADC:{raw_val:5}  Corr:{corrected:7.1f}  "
                    f"g:{grams:7.2f}  Δ:{delta:6.1f}g"
                )

                payload = {
                    "est_grams":   round(grams, 2),
                    "shelf_id":    CONFIG.shelf_id,
                    "sampled_at":  timestamp,
                    "scale_index": scale_index,
                    "adc_fault":   False,
                }
                sock.sendto(
                    json.dumps(payload).encode(),
                    (CONFIG.udp_ip, CONFIG.udp_port),
                )

            except Exception as exc:
                fault_trackers[s_id].record_failure(exc)
                payload = {
                    "est_grams":   None,
                    "shelf_id":    CONFIG.shelf_id,
                    "sampled_at":  timestamp,
                    "scale_index": scale_index,
                    "adc_fault":   True,
                    "sensor_id":   s_id,
                }
                sock.sendto(
                    json.dumps(payload).encode(),
                    (CONFIG.udp_ip, CONFIG.udp_port),
                )

        # ── Dynamic Sleep ─────────────────────────────────────────────────────
        # Re-evaluate is_active after processing all sensors this cycle,
        # in case a delta was detected mid-loop that should affect sleep now.
        is_active = (time.time() - last_activity_ts) < CONFIG.active_cooldown_s
        sleep_duration = CONFIG.interval_active if is_active else CONFIG.interval_idle

        log.debug("Cycle complete. Mode: %s. Sleeping %.1fs.",
                  "ACTIVE" if is_active else "IDLE", sleep_duration)

        time.sleep(sleep_duration)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Process A stopped by keyboard interrupt.")
    except Exception as exc:
        log.critical("Process A crashed: %s", exc, exc_info=True)
        raise
