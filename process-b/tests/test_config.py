"""Tests for :mod:`process_b.config`.

These are pure-function tests over a synthetic env mapping. We never touch
``os.environ`` so the suite is order-independent and parallel-safe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from process_b.config import Config, ConfigError


SHELF_A = "550e8400-e29b-41d4-a716-446655440000"
SHELF_B = "660e8400-e29b-41d4-a716-446655440001"


def _minimal_env(*, tmp_path: Path | None = None, **overrides: str) -> dict[str, str]:
    """Return a valid env mapping; tests override individual keys.

    ``DEVICE_FILE_PATH`` is pointed at a guaranteed-nonexistent path inside
    ``tmp_path`` (or a fixed garbage path) so happy-path tests exercise the
    env-fallback branch deterministically. Tests that want to drive the
    device.json branch must override ``DEVICE_FILE_PATH`` themselves.
    """
    if tmp_path is not None:
        device_file = str(tmp_path / "absent-device.json")
    else:
        device_file = "/nonexistent/device.json"

    env = {
        "PI_API_KEY": "secret",
        "BACKEND_URL": "https://api.example.com/",
        "DB_PATH": "/var/lib/process-b/outbox.db",
        "SHELF_IDS": SHELF_A,
        "UDP_LISTEN_PORT": "5005",
        "PROC_A_CONTROL_PORT": "5006",
        "DEVICE_FILE_PATH": device_file,
    }
    env.update(overrides)
    return env


class TestHappyPath:
    def test_minimal_required_env_yields_defaults_for_optionals(self) -> None:
        cfg = Config.from_env(_minimal_env())

        assert cfg.pi_api_key == "secret"
        assert cfg.backend_url == "https://api.example.com"  # trailing slash stripped
        assert cfg.db_path == "/var/lib/process-b/outbox.db"
        assert cfg.shelf_ids == (SHELF_A,)

        assert cfg.udp_listen_host == "0.0.0.0"
        assert cfg.udp_listen_port == 5005
        assert cfg.proc_a_control_host == "127.0.0.1"
        assert cfg.proc_a_control_port == 5006

        assert cfg.drain_interval_sec == pytest.approx(5.0)
        assert cfg.drain_batch_size == 50
        assert cfg.poll_interval_sec == pytest.approx(3.0)

        assert cfg.log_level == "INFO"
        # No device.json present → user_id stays None and shelf_ids comes
        # from the env var (legacy / pre-provisioning path).
        assert cfg.user_id is None

    def test_optionals_when_provided_override_defaults(self) -> None:
        cfg = Config.from_env(
            _minimal_env(
                UDP_LISTEN_HOST="127.0.0.1",
                PROC_A_CONTROL_HOST="10.0.0.5",
                DRAIN_INTERVAL_SEC="2.5",
                DRAIN_BATCH_SIZE="100",
                POLL_INTERVAL_SEC="1",
                LOG_LEVEL="debug",
            )
        )

        assert cfg.udp_listen_host == "127.0.0.1"
        assert cfg.proc_a_control_host == "10.0.0.5"
        assert cfg.drain_interval_sec == pytest.approx(2.5)
        assert cfg.drain_batch_size == 100
        assert cfg.poll_interval_sec == pytest.approx(1.0)
        assert cfg.log_level == "DEBUG"

    def test_shelf_ids_parses_comma_separated_list(self) -> None:
        cfg = Config.from_env(_minimal_env(SHELF_IDS=f"{SHELF_A}, {SHELF_B} ,"))
        assert cfg.shelf_ids == (SHELF_A, SHELF_B)

    def test_config_is_frozen(self) -> None:
        cfg = Config.from_env(_minimal_env())
        with pytest.raises((AttributeError, Exception)):
            cfg.pi_api_key = "other"  # type: ignore[misc]


class TestErrors:
    def test_single_missing_required_var_is_named(self) -> None:
        env = _minimal_env()
        del env["PI_API_KEY"]
        with pytest.raises(ConfigError) as exc:
            Config.from_env(env)
        assert "PI_API_KEY" in str(exc.value)

    def test_blank_required_var_is_treated_as_missing(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(_minimal_env(PI_API_KEY="   "))
        assert "PI_API_KEY" in str(exc.value)

    def test_multiple_missing_vars_all_reported_in_one_error(self) -> None:
        env = _minimal_env()
        del env["PI_API_KEY"]
        del env["BACKEND_URL"]
        del env["UDP_LISTEN_PORT"]
        with pytest.raises(ConfigError) as exc:
            Config.from_env(env)
        msg = str(exc.value)
        assert "PI_API_KEY" in msg
        assert "BACKEND_URL" in msg
        assert "UDP_LISTEN_PORT" in msg

    def test_non_integer_port_is_rejected(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(_minimal_env(UDP_LISTEN_PORT="not-a-number"))
        assert "UDP_LISTEN_PORT" in str(exc.value)
        assert "integer" in str(exc.value)

    def test_non_numeric_drain_interval_is_rejected(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(_minimal_env(DRAIN_INTERVAL_SEC="soon"))
        assert "DRAIN_INTERVAL_SEC" in str(exc.value)

    def test_empty_shelf_ids_is_rejected(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(_minimal_env(SHELF_IDS=" , , "))
        assert "SHELF_IDS" in str(exc.value)


class TestDeviceFile:
    """Behaviour around DEVICE_FILE_PATH and the shelf_id precedence rule."""

    def _write_device_json(
        self, path: Path, *, user_id: str = "user-uuid-1", shelf_id: str = SHELF_B
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "user_id": user_id,
                    "shelf_id": shelf_id,
                    "provisioned_at": "2026-05-04T07:00:00Z",
                    "schema_version": 1,
                }
            )
        )

    def test_device_json_wins_over_env_shelf_ids(self, tmp_path: Path) -> None:
        device_path = tmp_path / "device.json"
        self._write_device_json(device_path, user_id="u-from-file", shelf_id=SHELF_B)

        cfg = Config.from_env(
            _minimal_env(
                tmp_path=tmp_path,
                DEVICE_FILE_PATH=str(device_path),
                SHELF_IDS=SHELF_A,  # should be ignored
            )
        )

        assert cfg.shelf_ids == (SHELF_B,)
        assert cfg.user_id == "u-from-file"
        assert cfg.device_file_path == str(device_path)

    def test_no_device_json_falls_back_to_env(self, tmp_path: Path) -> None:
        # _minimal_env points DEVICE_FILE_PATH at an absent file under tmp_path.
        cfg = Config.from_env(_minimal_env(tmp_path=tmp_path))
        assert cfg.shelf_ids == (SHELF_A,)
        assert cfg.user_id is None

    def test_no_device_json_and_no_env_shelf_ids_is_rejected(
        self, tmp_path: Path
    ) -> None:
        env = _minimal_env(tmp_path=tmp_path)
        del env["SHELF_IDS"]
        with pytest.raises(ConfigError) as exc:
            Config.from_env(env)
        # Error message should be helpful — mention both alternatives.
        assert "SHELF_IDS" in str(exc.value)
        assert "device.json" in str(exc.value)

    def test_malformed_device_json_is_rejected_loudly(self, tmp_path: Path) -> None:
        device_path = tmp_path / "device.json"
        device_path.write_text("not valid json")

        with pytest.raises(ConfigError) as exc:
            Config.from_env(
                _minimal_env(tmp_path=tmp_path, DEVICE_FILE_PATH=str(device_path))
            )
        # Don't silently fall back to env when device.json is corrupt —
        # operator must look.
        assert "DEVICE_FILE_PATH" in str(exc.value)

    def test_device_file_path_default(self, tmp_path: Path) -> None:
        # When DEVICE_FILE_PATH is unset, default is /etc/shelfaware/device.json.
        # (Almost certainly absent on the test box; use the env-fallback path.)
        env = _minimal_env(tmp_path=tmp_path)
        del env["DEVICE_FILE_PATH"]
        cfg = Config.from_env(env)
        assert cfg.device_file_path == "/etc/shelfaware/device.json"
