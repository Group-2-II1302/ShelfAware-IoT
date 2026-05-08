#!/usr/bin/env python3
"""
orchestrator.py  –  ShelfAware Boot Orchestrator
═════════════════════════════════════════════════
Execution flow:
  1. Poll NetworkManager readiness (up to 30 s).
  2. Ping 8.8.8.8 to verify internet connectivity (timeout 10 s).
  3a. Internet OK  → kill B/C, launch Process A.
  3b. No internet  → kill A, switch to AP mode, launch Process C then B.

DEV_MODE  (SHELFAWARE_DEV=1):
  All nmcli / NetworkManager calls are short-circuited.
  Safe to run over Tailscale / mobile hotspot during development.
  Export SHELFAWARE_DEV=1 before running to activate.

Defensive guarantees:
  • Every subprocess call is time-bounded.
  • Opposing process group is fully reaped before new one starts.
  • Rolling log → /var/log/shelfaware_orchestrator.log (5 MB × 3).
  • Consecutive-failure state file prevents silent brick loops.
  • SIGTERM → SIGKILL escalation with configurable grace period.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────
# DEV MODE  –  set SHELFAWARE_DEV=1 to activate
# ──────────────────────────────────────────────
DEV_MODE = os.environ.get("SHELFAWARE_DEV", "0") == "1"

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
LOG_FILE         = Path("/home/group2/group2/ShelfAware-IoT/logs/shelfaware_orchestrator.log")
LOG_MAX_BYTES    = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

if DEV_MODE:
    BASE = Path("/home/group2/group2/ShelfAware-IoT")
    STATE_FILE    = BASE / "logs/orchestrator_state.json"
    PROCESS_A_CMD = [sys.executable, str(BASE / "process-a/process_A.py")]
    PROCESS_B_CMD = [sys.executable, str(BASE / "process-b/process_b/main.py")]
    PROCESS_C_CMD = [sys.executable, str(BASE / "process-c/process_c.py")]
else:
    STATE_FILE    = Path("/var/lib/shelfaware/orchestrator_state.json")
    PROCESS_A_CMD = [sys.executable, "/opt/shelfaware/process-a/process_A.py"]
    PROCESS_B_CMD = [sys.executable, "/opt/shelfaware/process-b/process_b/main.py"]
    PROCESS_C_CMD = [sys.executable, "/opt/shelfaware/process-c/process_c.py"]

PROCESS_A_PATTERN = "process-a/process_A.py"
PROCESS_B_PATTERN = "process-b/process_b/main.py"
PROCESS_C_PATTERN = "process-c/process_c.py"

PING_HOST        = "8.8.8.8"
PING_TIMEOUT_SEC = 10
PING_COUNT       = 3

NM_READY_TIMEOUT = 30
NM_POLL_INTERVAL = 2

AP_PROFILE_NAME  = "ShelfAware_Setup"
CONNECT_TIMEOUT  = 30

PROC_TERM_GRACE  = 5
MAX_FAILURES     = 5

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

def _build_logger() -> logging.Logger:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(funcName)-30s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    handler.setFormatter(fmt)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    log = logging.getLogger("shelfaware.orchestrator")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.addHandler(console)
    return log


log = _build_logger()

# ──────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────

def _load_state() -> dict:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception as exc:
        log.warning("Could not read state file: %s", exc)
    return {"consecutive_failures": 0, "last_mode": None}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("Could not write state file: %s", exc)


# ──────────────────────────────────────────────
# NetworkManager readiness guard
# ──────────────────────────────────────────────

def wait_for_networkmanager(timeout: int = NM_READY_TIMEOUT) -> bool:
    if DEV_MODE:
        log.info("[DEV MODE] Skipping NetworkManager readiness check — returning True.")
        return True

    deadline = time.monotonic() + timeout
    log.info("Waiting up to %d s for NetworkManager to be ready …", timeout)

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["nmcli", "general", "status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if result.returncode == 0:
                log.info("NetworkManager is ready.")
                return True
        except subprocess.TimeoutExpired:
            log.debug("nmcli status timed out – NM still starting.")
        except FileNotFoundError:
            log.critical("nmcli not found. Is NetworkManager installed?")
            return False
        except Exception as exc:
            log.debug("nmcli status error: %s", exc)

        time.sleep(NM_POLL_INTERVAL)

    log.error("NetworkManager did not become ready within %d s.", timeout)
    return False


# ──────────────────────────────────────────────
# Internet connectivity check
# ──────────────────────────────────────────────

def has_internet(host: str = PING_HOST, count: int = PING_COUNT,
                 timeout: int = PING_TIMEOUT_SEC) -> bool:
    if DEV_MODE:
        log.info("[DEV MODE] Skipping ping check — simulating ONLINE.")
        return True

    cmd = ["ping", "-c", str(count), "-W", str(timeout), host]
    log.info("Checking internet: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 5,
        )
        reachable = result.returncode == 0
        log.info("Internet check → %s", "ONLINE" if reachable else "OFFLINE")
        return reachable

    except subprocess.TimeoutExpired:
        log.error("Ping command timed out after %d s.", timeout + 5)
        return False
    except FileNotFoundError:
        log.critical("`ping` binary not found.")
        return False
    except Exception as exc:
        log.error("Unexpected ping error: %s", exc, exc_info=True)
        return False


# ──────────────────────────────────────────────
# Process management
# ──────────────────────────────────────────────

_running: dict[str, Optional[subprocess.Popen]] = {
    "A": None,
    "B": None,
    "C": None,
}


def _kill_process(name: str, grace: int = PROC_TERM_GRACE) -> None:
    proc: Optional[subprocess.Popen] = _running.get(name)

    if proc is None:
        log.debug("Process %s: no handle tracked, skipping kill.", name)
        return

    if proc.poll() is not None:
        log.debug("Process %s (PID %d) already exited with code %s.",
                  name, proc.pid, proc.returncode)
        _running[name] = None
        return

    log.info("Sending SIGTERM to Process %s (PID %d) …", name, proc.pid)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        log.warning("SIGTERM to %s failed: %s", name, exc)

    try:
        proc.wait(timeout=grace)
        log.info("Process %s exited cleanly after SIGTERM.", name)
    except subprocess.TimeoutExpired:
        log.warning("Process %s did not exit in %d s – escalating to SIGKILL.",
                    name, grace)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=3)
            log.info("Process %s killed via SIGKILL.", name)
        except Exception as exc:
            log.error("SIGKILL to Process %s failed: %s", name, exc)
    except Exception as exc:
        log.error("Unexpected error waiting on Process %s: %s", name, exc)

    _running[name] = None


def _kill_group(*names: str) -> None:
    for name in names:
        _kill_process(name)


def _launch_process(name: str, cmd: list[str]) -> Optional[subprocess.Popen]:
    log.info("Launching Process %s: %s", name, " ".join(cmd))
    try:
        log_path = f"/var/log/shelfaware_proc_{name.lower()}.log"
        log_fh = open(log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _running[name] = proc
        log.info("Process %s started (PID %d).", name, proc.pid)
        return proc
    except FileNotFoundError:
        log.critical("Process %s executable not found: %s", name, cmd[0])
    except PermissionError:
        log.critical("Process %s: permission denied executing %s.", name, cmd[0])
    except Exception as exc:
        log.critical("Process %s failed to start: %s", name, exc, exc_info=True)

    _running[name] = None
    return None

def _kill_orphans() -> None:
    """Kill any leftover process_a/b/c instances from previous orchestrator runs.

    Called at startup before any launches. Without this, an unclean shutdown
    of a previous orchestrator session can leave Process A/B/C running outside
    our cgroup, causing port conflicts and double-instances when we relaunch.
    """
    if DEV_MODE:
        log.info("[DEV MODE] skipping orphan cleanup")
        return

    patterns = [
        "process-b/process_b/main.py",
        "process-a/process_A.py",
        "process-c/process_c.py",
    ]
    for pat in patterns:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pat],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split()
                log.warning("Found orphaned %s (pids=%s); killing.", pat, pids)
                subprocess.run(["pkill", "-9", "-f", pat], timeout=5)
        except Exception as exc:
            log.warning("Orphan check for %s failed: %s", pat, exc)

    time.sleep(2)

# ──────────────────────────────────────────────
# NetworkManager / AP helpers
# ──────────────────────────────────────────────

def _kill_by_pattern(pattern: str) -> None:
    """Kill any process whose cmdline matches ``pattern``. No-op if none.

    Used as defence-in-depth before launching A/B/C. If a previous orchestrator
    session left an orphan that we haven't tracked in ``_running``, this
    ensures we don't race against it for a port (e.g. UDP 5005 for B).
    """
    if DEV_MODE:
        return
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split()
            log.warning(
                "Killing stale process matching %r (pids=%s) before relaunch.",
                pattern, pids,
            )
            subprocess.run(["pkill", "-9", "-f", pattern], timeout=5)
            time.sleep(1)
    except Exception as exc:
        log.warning("Stale-process kill for %r failed: %s", pattern, exc)


def _safe_launch(name: str, cmd: list[str], pattern: str) -> Optional[subprocess.Popen]:
    """Kill any orphan matching ``pattern`` then launch. See :func:`_kill_by_pattern`."""
    _kill_by_pattern(pattern)
    return _launch_process(name, cmd)

def _nmcli(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    cmd = ["nmcli"] + list(args)
    log.debug("nmcli cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        log.error("nmcli error (rc=%d): %s", result.returncode, result.stderr.strip())
        result.check_returncode()
    return result


def switch_to_ap_mode() -> bool:
    if DEV_MODE:
        log.info(
            "[DEV MODE] Skipping AP mode switch — would run: "
            "nmcli connection up '%s'. Returning True.", AP_PROFILE_NAME
        )
        return True

    log.info("Switching to AP mode (profile: %s) …", AP_PROFILE_NAME)
    try:
        _nmcli("device", "disconnect", "wlan0", timeout=10)
    except Exception:
        pass

    try:
        _nmcli("connection", "up", AP_PROFILE_NAME, timeout=20)
        log.info("AP mode active.")
        return True
    except subprocess.TimeoutExpired:
        log.error("Timed out activating AP profile.")
    except subprocess.CalledProcessError as exc:
        log.error("Failed to activate AP profile: %s", exc)
    except Exception as exc:
        log.error("Unexpected AP switch error: %s", exc, exc_info=True)
    return False


# ──────────────────────────────────────────────
# Main orchestration logic
# ──────────────────────────────────────────────

def run_online_mode() -> None:
    log.info("=== ONLINE MODE ===")
    _kill_group("C")

    if not DEV_MODE:
        try:
            _nmcli("connection", "down", AP_PROFILE_NAME, timeout=10)
            log.info("AP profile brought down.")
        except subprocess.CalledProcessError:
            pass
        except Exception as exc:
            log.warning("Could not bring AP down: %s", exc)
        
        try:
            _nmcli("connection", "up", "ShelfAware_STA", timeout=20)
            log.info("STA profile reconnected.")
        except Exception as exc:
            log.warning("Could not reconnect STA: %s", exc)

    if _running["A"] and _running["A"].poll() is None:
        log.info("Process A already running (PID %d).", _running["A"].pid)
    else:
        _safe_launch("A", PROCESS_A_CMD, PROCESS_A_PATTERN)

    # Always restart B on entry to online mode so the registrar gets a
    # fresh attempt with the (now-reachable) backend. The registrar runs
    # once at B startup; if it gave up during the offline phase (or is
    # mid-backoff), the only reliable way to retrigger is a clean restart.

    if _running["B"] and _running["B"].poll() is None:
        log.info("Restarting Process B (PID %d) to refresh registrar.", _running["B"].pid)
        _kill_process("B")
    _safe_launch("B", PROCESS_B_CMD, PROCESS_B_PATTERN)

def run_offline_mode() -> None:
    log.info("=== OFFLINE / SETUP MODE ===")
    _kill_group("A")

    if not switch_to_ap_mode():
        log.critical("Cannot enter AP mode. Orchestrator cannot proceed safely.")
        raise RuntimeError("AP mode switch failed")

    time.sleep(3)

    if not (_running["C"] and _running["C"].poll() is None):
        _safe_launch("C", PROCESS_C_CMD, PROCESS_C_PATTERN)
        time.sleep(2)

    if not (_running["B"] and _running["B"].poll() is None):
        _safe_launch("B", PROCESS_B_CMD, PROCESS_B_PATTERN)


def main() -> None:
    log.info("━" * 60)
    log.info("ShelfAware Orchestrator starting (PID %d).", os.getpid())

    if DEV_MODE:
        log.warning(
            "[DEV MODE] SHELFAWARE_DEV=1 — all nmcli/NM calls are "
            "short-circuited. Safe for Tailscale / hotspot development."
        )

    _kill_orphans()

    state = _load_state()

    if state["consecutive_failures"] >= MAX_FAILURES:
        log.critical(
            "Exceeded %d consecutive failures. Refusing to loop. "
            "Manual intervention required. Entering AP mode as safe fallback.",
            MAX_FAILURES,
        )
        switch_to_ap_mode()
        sys.exit(1)

    if not wait_for_networkmanager():
        state["consecutive_failures"] += 1
        _save_state(state)
        log.critical("NetworkManager unavailable. Aborting.")
        sys.exit(1)

    try:
        online = has_internet()
    except Exception as exc:
        log.error("Internet check raised an unexpected exception: %s", exc,
                  exc_info=True)
        online = False

    try:
        if online:
            run_online_mode()
            state["last_mode"] = "online"
        else:
            run_offline_mode()
            state["last_mode"] = "offline"

        state["consecutive_failures"] = 0
        _save_state(state)

    except Exception as exc:
        log.critical("Orchestrator encountered a fatal error: %s", exc,
                     exc_info=True)
        state["consecutive_failures"] += 1
        _save_state(state)
        sys.exit(1)

    log.info("Orchestrator entering steady-state monitor loop.")
    check_interval = 60

    while True:
        time.sleep(check_interval)

        try:
            still_online = has_internet()
        except Exception as exc:
            log.warning("Periodic internet check failed: %s", exc)
            continue

        if still_online and state["last_mode"] != "online":
            log.info("Network recovered – switching to online mode.")
            run_online_mode()
            state["last_mode"] = "online"
            _save_state(state)

        elif not still_online and state["last_mode"] != "offline":
            log.warning("Network lost – switching to offline/setup mode.")
            run_offline_mode()
            state["last_mode"] = "offline"
            _save_state(state)

        for name, proc in _running.items():
            if proc is not None:
                rc = proc.poll()
                if rc is not None:
                    log.warning("Process %s (PID %d) exited unexpectedly "
                                "with code %d.", name, proc.pid, rc)
                    _running[name] = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Orchestrator stopped by keyboard interrupt.")
    except Exception as exc:
        log.critical("Orchestrator crashed: %s", exc, exc_info=True)
        raise
