"""Tests for :mod:`process_c.config`."""

from __future__ import annotations

import pytest

from process_c.config import Config, ConfigError


class TestFromEnvHappyPath:
    def test_all_defaults(self) -> None:
        cfg = Config.from_env(env={})
        assert cfg.bind_host == "0.0.0.0"
        assert cfg.bind_port == 80
        assert cfg.device_file_path == "/etc/shelfaware/device.json"
        assert cfg.allow_reset_endpoint is False
        assert cfg.log_level == "INFO"
        assert cfg.dev_mode is False

    def test_full_overrides(self) -> None:
        cfg = Config.from_env(
            env={
                "BIND_HOST": "127.0.0.1",
                "BIND_PORT": "8080",
                "DEVICE_FILE_PATH": "/tmp/device.json",
                "ALLOW_RESET_ENDPOINT": "1",
                "LOG_LEVEL": "DEBUG",
                "SHELFAWARE_DEV": "1",
            }
        )
        assert cfg.bind_host == "127.0.0.1"
        assert cfg.bind_port == 8080
        assert cfg.device_file_path == "/tmp/device.json"
        assert cfg.allow_reset_endpoint is True
        assert cfg.log_level == "DEBUG"
        assert cfg.dev_mode is True

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1", True), ("true", True), ("True", True), ("YES", True), ("on", True),
            ("0", False), ("false", False), ("FALSE", False), ("no", False), ("off", False),
        ],
    )
    def test_bool_parses_common_aliases(self, raw: str, expected: bool) -> None:
        cfg = Config.from_env(env={"SHELFAWARE_DEV": raw})
        assert cfg.dev_mode is expected

    def test_log_level_uppercased(self) -> None:
        cfg = Config.from_env(env={"LOG_LEVEL": "debug"})
        assert cfg.log_level == "DEBUG"

    def test_blank_bind_host_falls_back_to_default(self) -> None:
        cfg = Config.from_env(env={"BIND_HOST": "   "})
        assert cfg.bind_host == "0.0.0.0"


class TestFromEnvErrors:
    def test_invalid_port_string(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(env={"BIND_PORT": "not-a-number"})
        assert "BIND_PORT" in str(exc.value)

    def test_port_out_of_range(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(env={"BIND_PORT": "70000"})
        assert "1..65535" in str(exc.value)

    def test_port_zero_rejected(self) -> None:
        with pytest.raises(ConfigError):
            Config.from_env(env={"BIND_PORT": "0"})

    def test_blank_device_file_path(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(env={"DEVICE_FILE_PATH": "   "})
        assert "DEVICE_FILE_PATH" in str(exc.value)

    def test_invalid_log_level(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(env={"LOG_LEVEL": "TRACE"})
        assert "LOG_LEVEL" in str(exc.value)

    def test_invalid_bool(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(env={"SHELFAWARE_DEV": "maybe"})
        assert "SHELFAWARE_DEV" in str(exc.value)

    def test_multiple_errors_aggregated(self) -> None:
        with pytest.raises(ConfigError) as exc:
            Config.from_env(
                env={"BIND_PORT": "abc", "LOG_LEVEL": "TRACE", "SHELFAWARE_DEV": "x"}
            )
        msg = str(exc.value)
        assert "BIND_PORT" in msg
        assert "LOG_LEVEL" in msg
        assert "SHELFAWARE_DEV" in msg
