"""Environment-variable configuration loader for Process B.

All configuration is read from environment variables — no config files. The
daemon is supervised by ``systemd`` in production, which provides the env vars
via the unit file.

The single entry point is :meth:`Config.from_env`, which validates everything
up front and raises :class:`ConfigError` listing *all* problems at once. This
makes misconfiguration easy to fix in one shot rather than one var at a time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class ConfigError(ValueError):
    """Raised when one or more required env vars are missing or malformed.

    The message lists every problem found, one per line, so the operator can
    fix all misconfiguration in a single pass.
    """


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved configuration for a Process B daemon instance."""

    pi_api_key: str
    backend_url: str
    db_path: str

    shelf_ids: tuple[str, ...]

    udp_listen_host: str
    udp_listen_port: int

    proc_a_control_host: str
    proc_a_control_port: int

    drain_interval_sec: float
    drain_batch_size: int

    poll_interval_sec: float

    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build a :class:`Config` from environment variables.

        Parameters
        ----------
        env:
            Optional mapping to read from. Defaults to :data:`os.environ`.
            Injectable for tests so we never mutate real process env.

        Raises
        ------
        ConfigError
            If any required variable is missing, any integer/float fails to
            parse, or ``SHELF_IDS`` is empty. All errors collected in one
            message.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        errors: list[str] = []

        def require(name: str) -> str:
            value = source.get(name, "").strip()
            if not value:
                errors.append(f"{name} is required")
                return ""
            return value

        def optional(name: str, default: str) -> str:
            value = source.get(name)
            if value is None or value.strip() == "":
                return default
            return value.strip()

        def parse_int(name: str, raw: str) -> int:
            try:
                return int(raw)
            except ValueError:
                errors.append(f"{name} must be an integer, got {raw!r}")
                return 0

        def parse_float(name: str, raw: str) -> float:
            try:
                return float(raw)
            except ValueError:
                errors.append(f"{name} must be a number, got {raw!r}")
                return 0.0

        pi_api_key = require("PI_API_KEY")
        backend_url = require("BACKEND_URL").rstrip("/")
        db_path = require("DB_PATH")

        shelf_ids_raw = require("SHELF_IDS")
        shelf_ids: tuple[str, ...] = ()
        if shelf_ids_raw:
            parts = tuple(s.strip() for s in shelf_ids_raw.split(",") if s.strip())
            if not parts:
                errors.append("SHELF_IDS must contain at least one shelf UUID")
            shelf_ids = parts

        udp_listen_host = optional("UDP_LISTEN_HOST", "0.0.0.0")
        udp_listen_port_raw = require("UDP_LISTEN_PORT")
        udp_listen_port = parse_int("UDP_LISTEN_PORT", udp_listen_port_raw) if udp_listen_port_raw else 0

        proc_a_control_host = optional("PROC_A_CONTROL_HOST", "127.0.0.1")
        proc_a_control_port_raw = require("PROC_A_CONTROL_PORT")
        proc_a_control_port = (
            parse_int("PROC_A_CONTROL_PORT", proc_a_control_port_raw)
            if proc_a_control_port_raw
            else 0
        )

        drain_interval_sec = parse_float(
            "DRAIN_INTERVAL_SEC", optional("DRAIN_INTERVAL_SEC", "5")
        )
        drain_batch_size = parse_int(
            "DRAIN_BATCH_SIZE", optional("DRAIN_BATCH_SIZE", "50")
        )
        poll_interval_sec = parse_float(
            "POLL_INTERVAL_SEC", optional("POLL_INTERVAL_SEC", "3")
        )

        log_level = optional("LOG_LEVEL", "INFO").upper()

        if errors:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))

        return cls(
            pi_api_key=pi_api_key,
            backend_url=backend_url,
            db_path=db_path,
            shelf_ids=shelf_ids,
            udp_listen_host=udp_listen_host,
            udp_listen_port=udp_listen_port,
            proc_a_control_host=proc_a_control_host,
            proc_a_control_port=proc_a_control_port,
            drain_interval_sec=drain_interval_sec,
            drain_batch_size=drain_batch_size,
            poll_interval_sec=poll_interval_sec,
            log_level=log_level,
        )
