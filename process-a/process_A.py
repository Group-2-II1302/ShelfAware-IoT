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
  ACTIVE  - a sensor delta > change_threshold_g was detected within
            the last active_cooldown_s seconds. Poll at interval_active.
  IDLE    - no significant delta detected recently. Poll at interval_idle.

CPU governor strategy:
  ACTIVE -> "performance"  (locks CPU to max 1500MHz)
  IDLE   -> "ondemand"     (CPU scales naturally with load)

  "ondemand" chosen over "powersave" to protect WiFi throughput.
  The WiFi chip (CYW43455) is on a separate clock domain and is never
  affected by cpufreq, but CPU starvation at 600MHz could cause UDP
  processing latency in Process B. "ondemand" avoids this entirely.

  Governor writes require root OR the following sudoers rule:
    group2 ALL=(ALL) NOPASSWD: /usr/bin/tee /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
  In production (systemd, runs as root) sudo is bypassed automatically.
  If neither condition is met, a warning is logged and the process
  continues normally - governor management is non-critical.

Fault model:
  If an entire ADC is missing at boot, it is marked FAULTED and its
  channels emit null payloads with adc_fault=True every cycle.
  Single read failures are logged and skipped; loop continues.
  I2C bus death propagates to main() for orchestrator to handle.
"""

import json
import logging
import os
import socket
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional

import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

DEVICE_FILE_PATH= os.environ.get("DEVICE_FILE_PATH", "/etc/shelfaware/device.json")

def _load_shelf_id(default: str) -> tuple[str, str]:
    path = Path(DEVICE_FILE_PATH)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            shelf_id = data.get("shelf_id")
            if isinstance(shelf_id, str) and shelf_id:
                return shelf_id, f"device.json ({path})"
        except Exception as exc:
            print(f"WARN: could not parse {path}: {exc}", flush=True)

    env_shelf= os.environ.get("SHELF_ID")
    if env_shelf:
        return env_shelf, "SHELF_ID env var"

    return default, "hardcoded default"

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------

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

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

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
    # Delta-Based Activity Trigger
    interval_active:     float = 0.5
    interval_idle:       float = 10.0
    active_cooldown_s:   float = 10.0
    change_threshold_g:  float = 15.0
    # CPU Governor
    governor_active:     str   = "performance"
    governor_idle:       str   = "ondemand"
    # battery telemetry
    battery_adc_address:    int   = 0x48
    battery_adc_channel:    int   = 3
    battery_divider_ratio:  int   = 2.0 # TBD
    battery_window_size:    int   = 5
    #Different polling intervals from scale loop
    battery_interval_active: float = 10.0
    battery_interval_idle:   float = 30.0


CONFIG = SystemConfig()

# ------------------------------------------------------------------------------
# Battery telemetry
# ------------------------------------------------------------------------------

BATTERY_STATE_TABLE = [ # Should be tested
    ("full",     5.05, 4.95),
    ("normal",   4.95, 4.85),
    ("low",      4.80, 4.70),
    ("critical", 4.70, 0.00),
]

# ------------------------------------------------------------------------------
# Sensor map  (insertion order is the scale_index contract)
# ------------------------------------------------------------------------------

SENSOR_MAP: Dict[str, SensorConfig] = {
    "Z1_A0": SensorConfig(adc_channel=0, adc_address=0x48),
    "Z1_A1": SensorConfig(adc_channel=1, adc_address=0x48),
    "Z1_A2": SensorConfig(adc_channel=2, adc_address=0x48),
    "Z2_A0": SensorConfig(adc_channel=0, adc_address=0x49),
    "Z2_A1": SensorConfig(adc_channel=1, adc_address=0x49),
    "Z2_A2": SensorConfig(adc_channel=2, adc_address=0x49),
}

SENSOR_SEQUENCE = [
    "Z1_A0",  # scale_index 0
    "Z1_A1",  # scale_index 1
    "Z1_A2",  # scale_index 2
    "Z2_A0",  # scale_index 3
    "Z2_A1",  # scale_index 4
    "Z2_A2",  # scale_index 5
]

assert set(SENSOR_SEQUENCE) == set(SENSOR_MAP.keys()), \
    "SENSOR_SEQUENCE and SENSOR_MAP are out of sync."
assert len(SENSOR_SEQUENCE) == len(set(SENSOR_SEQUENCE)), \
    "SENSOR_SEQUENCE contains duplicates."

# ------------------------------------------------------------------------------
# CPU Governor Manager
# ------------------------------------------------------------------------------

class CpuGovernor:
    """
    Manages the Linux cpufreq governor in sync with the shelf activity state.

    Write strategy (tried in order until one succeeds):
      1. Direct sysfs write  - works when running as root (production/systemd).
      2. sudo tee            - works in dev when the sudoers rule is in place.
      3. Disabled            - logs a warning once and never tries again.

    Only writes to sysfs when the governor actually needs to change,
    avoiding redundant kernel calls every cycle.
    """

    GOVERNOR_PATH  = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    AVAILABLE_PATH = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors")

    def __init__(self, governor_active: str, governor_idle: str):
        self._governor_active   = governor_active
        self._governor_idle     = governor_idle
        self._current_governor: Optional[str] = None
        self._enabled           = self._check_available()

    def _check_available(self) -> bool:
        if not self.GOVERNOR_PATH.exists():
            log.warning(
                "CpuGovernor: sysfs path not found (%s). "
                "CPU frequency management disabled.", self.GOVERNOR_PATH,
            )
            return False
        try:
            available = self.AVAILABLE_PATH.read_text().split()
            for gov in (self._governor_active, self._governor_idle):
                if gov not in available:
                    log.warning(
                        "CpuGovernor: governor '%s' not available. "
                        "Available: %s. CPU frequency management disabled.",
                        gov, available,
                    )
                    return False
        except Exception as exc:
            log.warning("CpuGovernor: could not read available governors: %s", exc)
            return False

        log.info("CpuGovernor: initialised. active='%s'  idle='%s'",
                 self._governor_active, self._governor_idle)
        return True

    def _write_governor(self, governor: str) -> bool:
        # Strategy 1: direct write (root / production)
        try:
            self.GOVERNOR_PATH.write_text(governor)
            return True
        except PermissionError:
            pass
        except Exception as exc:
            log.warning("CpuGovernor: direct write failed: %s", exc)
            return False

        # Strategy 2: sudo tee (dev with sudoers rule)
        try:
            result = subprocess.run(
                ["sudo", "tee", str(self.GOVERNOR_PATH)],
                input=governor,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True
            log.warning("CpuGovernor: sudo tee failed (rc=%d): %s",
                        result.returncode, result.stderr.strip())
        except subprocess.TimeoutExpired:
            log.warning("CpuGovernor: sudo tee timed out.")
        except Exception as exc:
            log.warning("CpuGovernor: sudo tee error: %s", exc)

        return False

    def set(self, is_active: bool) -> None:
        """
        Call once per cycle with the current activity state.
        No-ops if governor is already correct.
        """
        if not self._enabled:
            return

        target = self._governor_active if is_active else self._governor_idle

        if target == self._current_governor:
            return

        if self._write_governor(target):
            log.info("CpuGovernor: %s -> %s",
                     self._current_governor or "unknown", target)
            self._current_governor = target
        else:
            log.error(
                "CpuGovernor: could not write governor '%s'. "
                "Check sudoers rule or run as root. Disabling for this session.",
                target,
            )
            self._enabled = False

    def restore_default(self) -> None:
        """Restore 'ondemand' on clean exit."""
        if not self._enabled:
            return
        if self._write_governor("ondemand"):
            log.info("CpuGovernor: restored to 'ondemand' on exit.")
        else:
            log.warning("CpuGovernor: could not restore governor on exit.")


# ------------------------------------------------------------------------------
# Hardware manager
# ------------------------------------------------------------------------------

class HardwareManager:
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
                cls._ads_instances[address] = ADS.ADS1115(cls._i2c, address=address)
                log.info("ADS1115 at 0x%02X initialised.", address)
            except Exception as exc:
                log.error(
                    "Failed to initialise ADS1115 at 0x%02X: %s. "
                    "All channels on this ADC will be marked faulted.", address, exc,
                )
                cls._faulted_addresses.add(address)
                return None
        return AnalogIn(cls._ads_instances[address], channel_index)

    @classmethod
    def is_address_faulted(cls, address: int) -> bool:
        return address in cls._faulted_addresses


# ------------------------------------------------------------------------------
# Signal processing
# ------------------------------------------------------------------------------

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
    """DO NOT MODIFY - untouched per spec."""

    def __init__(self, sensor_id: str):
        self.sensor_id = sensor_id
        self._baseline: Optional[float] = self._load_baseline()

    def _load_baseline(self) -> Optional[float]:
        if os.path.exists(CONFIG.storage_path):
            try:
                with open(CONFIG.storage_path, "r") as f:
                    return json.load(f).get(self.sensor_id)
            except Exception as exc:
                log.warning("Could not load baseline for %s: %s", self.sensor_id, exc)
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
                log.debug("Drift corrected for %s: %.1f -> %.1f",
                          self.sensor_id, self._baseline, filtered_adc)
                self._baseline = filtered_adc
                self._save_baseline(filtered_adc)
        return max(0.0, filtered_adc - self._baseline)


def adc_to_grams(adc: float, cfg: SensorConfig) -> float:
    if adc <= 0:
        return 0.0
    return (adc / cfg.a_const) ** (1.0 / cfg.b_const)

def determine_battery_state(voltage: float, prev_state: str) -> str:
    """
    Hysteretic battery state machine.

    Prevents rapid state flapping near thresholds.
    """
    for state, enter_v, exit_v in BATTERY_STATE_TABLE:
        if prev_state == state:
            if voltage >= exit_v:
                return state
        else:
            if voltage >= enter_v:
                return state

    return "critical"

# ------------------------------------------------------------------------------
# Per-sensor fault tracker
# ------------------------------------------------------------------------------

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
            log.error("Sensor %s faulted after %d consecutive errors. Last: %s",
                      self.sensor_id, self._consecutive, exc)
            self._is_faulted = True
        elif not self._is_faulted:
            log.warning("Sensor %s read error (%d/%d): %s",
                        self.sensor_id, self._consecutive, self.threshold, exc)

    @property
    def is_faulted(self) -> bool:
        return self._is_faulted


# ------------------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------------------

def main() -> None:
    shelf_id, source = _load_shelf_id(default=CONFIG.shelf_id)
    CONFIG.shelf_id = shelf_id

    log.info("=" * 60)
    log.info("PROCESS A STARTING  |  SHELF: %s | source: %s", CONFIG.shelf_id, source)
    log.info("Sensor sequence (scale_index order): %s", SENSOR_SEQUENCE)
    log.info(
        "Delta-based polling | active=%.1fs  idle=%.1fs  "
        "cooldown=%.1fs  threshold=%.1fg",
        CONFIG.interval_active, CONFIG.interval_idle,
        CONFIG.active_cooldown_s, CONFIG.change_threshold_g,
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # CPU Governor - initialised once, used every cycle
    cpu = CpuGovernor(CONFIG.governor_active, CONFIG.governor_idle)

    # Per-sensor objects
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
        log.info("Channel %s at 0x%02X ch%d -> %s",
                 s_id, cfg.adc_address, cfg.adc_channel, status)

    log.info("Initialisation complete. Entering read loop.")

# ------------------------------------------------------------------------------
# Battery monitor initialisation
# ------------------------------------------------------------------------------

    battery_channel = HardwareManager.get_adc_channel(
        CONFIG.battery_adc_address,
        CONFIG.battery_adc_channel,
    )

    battery_filter = MovingAverage(CONFIG.battery_window_size)

    battery_prev_voltage: Optional[float] = None
    battery_state = "full"

    last_battery_sample = 0.0

    if battery_channel is None:
        log.warning(
            "Battery ADC channel unavailable "
            "(0x%02X ch%d). Battery telemetry disabled.",
            CONFIG.battery_adc_address,
            CONFIG.battery_adc_channel,
        )
    else:
        log.info(
            "Battery telemetry enabled at "
            "0x%02X ch%d",
            CONFIG.battery_adc_address,
            CONFIG.battery_adc_channel,
        )

    # Delta-Based Activity State
    previous_grams:   Dict[str, float] = {s: 0.0 for s in SENSOR_SEQUENCE}
    last_activity_ts: float            = 0.0

    while True:
        timestamp  = datetime.now(timezone.utc).isoformat()
        is_active  = (time.time() - last_activity_ts) < CONFIG.active_cooldown_s
        mode_label = "ACTIVE" if is_active else "IDLE  "

        # Apply governor at the START of each cycle
        cpu.set(is_active)

        for scale_index, s_id in enumerate(SENSOR_SEQUENCE):
            cfg     = SENSOR_MAP[s_id]
            channel = channels[s_id]
            zone    = "Z1" if cfg.adc_address == 0x48 else "Z2"

            # ADC-level fault
            if channel is None or HardwareManager.is_address_faulted(cfg.adc_address):
                payload = {
                    "est_grams":   None,
                    "shelf_id":    CONFIG.shelf_id,
                    "sampled_at":  timestamp,
                    "scale_index": scale_index,
                    "adc_fault":   True,
                    "sensor_id":   s_id,
                }
                sock.sendto(json.dumps(payload).encode(), (CONFIG.udp_ip, CONFIG.udp_port))
                log.debug("[%s] scale_index=%d  ADC FAULTED.", s_id, scale_index)
                continue

            try:
                raw_val   = channel.value
                clean     = 0 if raw_val < CONFIG.deadzone_adc else raw_val
                smoothed  = filters[s_id].add(float(clean))
                corrected = compensators[s_id].correct(smoothed)
                grams     = adc_to_grams(corrected, cfg)

                fault_trackers[s_id].record_success()

                # Delta-Based Activity Trigger
                delta = abs(grams - previous_grams[s_id])
                if delta > CONFIG.change_threshold_g:
                    last_activity_ts = time.time()
                    log.debug("[%s] Activity: delta=%.1fg (%.1f->%.1f). Cooldown reset.",
                              s_id, delta, previous_grams[s_id], grams)
                previous_grams[s_id] = grams

                print(
                    f"[{mode_label}] [{s_id}({zone}|idx={scale_index})] "
                    f"ADC:{raw_val:5}  Corr:{corrected:7.1f}  "
                    f"g:{grams:7.2f}  d:{delta:6.1f}g"
                )

                payload = {
                    "est_grams":   round(grams, 2),
                    "shelf_id":    CONFIG.shelf_id,
                    "sampled_at":  timestamp,
                    "scale_index": scale_index,
                    "adc_fault":   False,
                }
                sock.sendto(json.dumps(payload).encode(), (CONFIG.udp_ip, CONFIG.udp_port))

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
                sock.sendto(json.dumps(payload).encode(), (CONFIG.udp_ip, CONFIG.udp_port))

# ------------------------------------------------------------------------------
# Battery telemetry
# ------------------------------------------------------------------------------

            now = time.time()

            battery_interval = (
                CONFIG.battery_interval_active
                if is_active
                else CONFIG.battery_interval_idle
            )

            should_sample_battery = (
                battery_channel is not None and
                (now - last_battery_sample) >= battery_interval
            )

            if should_sample_battery:
                try:
                    raw_voltage = battery_channel.voltage

                    measured_voltage = (
                        raw_voltage * CONFIG.battery_divider_ratio
                    )

                    smoothed_voltage = battery_filter.add(measured_voltage)

                    trend = None
                    if battery_prev_voltage is not None:
                        trend = (
                            smoothed_voltage -
                            battery_prev_voltage
                        )

                    battery_prev_voltage = smoothed_voltage

                    battery_state = determine_battery_state(
                        smoothed_voltage,
                        battery_state,
                    )

                    battery_payload = {
                        "metric_type": "battery",
                        "shelf_id": CONFIG.shelf_id,
                        "sampled_at": timestamp,

                        "voltage": round(smoothed_voltage, 3),

                        "state": battery_state,

                        "trend": (
                            round(trend, 5)
                            if trend is not None
                            else None
                        ),

                        "adc_fault": False,
                    }

                    sock.sendto(
                        json.dumps(battery_payload).encode(),
                        (CONFIG.udp_ip, CONFIG.udp_port),
                    )

                    log.debug(
                        "[BATTERY] "
                        "V=%.3f  "
                        "state=%s  "
                        "trend=%s",
                        smoothed_voltage,
                        battery_state,
                        (
                            f"{trend:+.5f}"
                            if trend is not None
                            else "None"
                        ),
                    )

                    # Optional early warning
                    if battery_state == "critical":
                        log.warning(
                            "[BATTERY] CRITICAL BATTERY LEVEL "
                            "(%.3fV)",
                            smoothed_voltage,
                        )

                    last_battery_sample = now

                except Exception as exc:
                    log.warning(
                        "Battery telemetry read failed: %s",
                        exc,
                    )

                    battery_payload = {
                        "metric_type": "battery",
                        "shelf_id": CONFIG.shelf_id,
                        "sampled_at": timestamp,

                        "voltage": None,
                        "state": "fault",
                        "trend": None,

                        "adc_fault": True,
                    }

                    sock.sendto(
                        json.dumps(battery_payload).encode(),
                        (CONFIG.udp_ip, CONFIG.udp_port),
                    )

        # Re-evaluate after full sweep — a delta mid-loop takes effect now
        is_active      = (time.time() - last_activity_ts) < CONFIG.active_cooldown_s
        sleep_duration = CONFIG.interval_active if is_active else CONFIG.interval_idle

        # Update governor if state flipped during the sweep
        cpu.set(is_active)

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
    finally:
        try:
            CpuGovernor(CONFIG.governor_active, CONFIG.governor_idle).restore_default()
        except Exception:
            pass
