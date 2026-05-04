"""Process C configuration loaded from environment variables.

Mirrors Process B's :class:`process_b.config.Config` style: a frozen
dataclass with a single :meth:`Config.from_env` classmethod that aggregates
all validation errors into one :class:`ConfigError` so operators see every
problem at once instead of fix-one-rerun cycles.

Environment variables
---------------------
``BIND_HOST``               default ``0.0.0.0``     — interface to bind on
``BIND_PORT``               default ``80``          — TCP port
``DEVICE_FILE_PATH``        default ``/etc/shelfaware/device.json``
``ALLOW_RESET_ENDPOINT``    default ``0``           — set ``1`` to expose POST /reset
``LOG_LEVEL``               default ``INFO``        — Python logging level name
``SHELFAWARE_DEV``          default ``0``           — set ``1`` to skip real WiFi switching

In ``SHELFAWARE_DEV=1`` mode, the WiFi backend is mocked (no nmcli calls);
this matches the convention used by the orchestrator and wifi_connector so
a single env var enables dev mode for the whole stack.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when one or more env vars are missing or malformed."""


@dataclass(frozen=True)
class Config:
    bind_host: str
    bind_port: int
    device_file_path: str
    allow_reset_endpoint: bool
    log_level: str
    dev_mode: bool

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        """Build a :class:`Config` from ``env`` (defaults to ``os.environ``).

        Aggregates every validation problem and raises one
        :class:`ConfigError` with a multi-line message at the end.
        """
        env = dict(os.environ) if env is None else env
        errors: list[str] = []

        bind_host = env.get("BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"

        bind_port = _parse_int(env, "BIND_PORT", default="80", errors=errors)
        if bind_port is not None and not (1 <= bind_port <= 65535):
            errors.append(f"BIND_PORT must be in 1..65535 (got {bind_port}).")

        device_file_path = env.get(
            "DEVICE_FILE_PATH", "/etc/shelfaware/device.json"
        ).strip()
        if not device_file_path:
            errors.append("DEVICE_FILE_PATH must not be blank.")

        allow_reset_endpoint = _parse_bool(
            env, "ALLOW_RESET_ENDPOINT", default="0", errors=errors
        )

        log_level = env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            errors.append(
                f"LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL "
                f"(got {log_level!r})."
            )

        dev_mode = _parse_bool(env, "SHELFAWARE_DEV", default="0", errors=errors)

        if errors:
            raise ConfigError(
                "Process C configuration is invalid:\n  - " + "\n  - ".join(errors)
            )

        # mypy: bind_port and the bool helpers are guaranteed non-None here
        # because errors would have populated above. cast via assert for typing.
        assert bind_port is not None
        assert allow_reset_endpoint is not None
        assert dev_mode is not None

        return cls(
            bind_host=bind_host,
            bind_port=bind_port,
            device_file_path=device_file_path,
            allow_reset_endpoint=allow_reset_endpoint,
            log_level=log_level,
            dev_mode=dev_mode,
        )


def _parse_int(
    env: dict[str, str], key: str, *, default: str, errors: list[str]
) -> int | None:
    raw = env.get(key, default).strip() or default
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{key} must be an integer (got {raw!r}).")
        return None


def _parse_bool(
    env: dict[str, str], key: str, *, default: str, errors: list[str]
) -> bool:
    raw = env.get(key, default).strip().lower() or default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    errors.append(f"{key} must be 0/1/true/false (got {raw!r}).")
    return False
