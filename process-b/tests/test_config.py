"""Tests for :mod:`process_b.config`.

These are pure-function tests over a synthetic env mapping. We never touch
``os.environ`` so the suite is order-independent and parallel-safe.
"""

from __future__ import annotations

import pytest

from process_b.config import Config, ConfigError


SHELF_A = "550e8400-e29b-41d4-a716-446655440000"
SHELF_B = "660e8400-e29b-41d4-a716-446655440001"


def _minimal_env(**overrides: str) -> dict[str, str]:
    """Return a valid env mapping; tests override individual keys."""
    env = {
        "PI_API_KEY": "secret",
        "BACKEND_URL": "https://api.example.com/",
        "DB_PATH": "/var/lib/process-b/outbox.db",
        "SHELF_IDS": SHELF_A,
        "UDP_LISTEN_PORT": "5005",
        "PROC_A_CONTROL_PORT": "5006",
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
