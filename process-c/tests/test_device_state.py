"""Tests for :mod:`process_c.device_state`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from process_c import device_state
from process_c.device_state import DeviceState, DeviceStateError, make_state


@pytest.fixture
def device_path(tmp_path: Path) -> Path:
    return tmp_path / "device.json"


class TestRead:
    def test_returns_none_when_file_missing(self, device_path: Path) -> None:
        assert device_state.read(device_path) is None

    def test_returns_state_for_well_formed_file(self, device_path: Path) -> None:
        device_path.write_text(
            json.dumps(
                {
                    "user_id": "u1",
                    "shelf_id": "s1",
                    "provisioned_at": "2026-05-04T07:00:00Z",
                    "schema_version": 1,
                }
            )
        )
        result = device_state.read(device_path)
        assert result == DeviceState(
            user_id="u1",
            shelf_id="s1",
            provisioned_at="2026-05-04T07:00:00Z",
            schema_version=1,
        )

    def test_raises_when_not_json(self, device_path: Path) -> None:
        device_path.write_text("not json")
        with pytest.raises(DeviceStateError) as exc:
            device_state.read(device_path)
        assert "JSON" in str(exc.value)

    def test_raises_when_root_is_not_object(self, device_path: Path) -> None:
        device_path.write_text("[1, 2, 3]")
        with pytest.raises(DeviceStateError):
            device_state.read(device_path)

    def test_raises_when_required_field_missing(self, device_path: Path) -> None:
        device_path.write_text(json.dumps({"user_id": "u", "shelf_id": "s"}))
        with pytest.raises(DeviceStateError) as exc:
            device_state.read(device_path)
        assert "provisioned_at" in str(exc.value)

    def test_default_schema_version_when_missing(self, device_path: Path) -> None:
        # schema_version is the only optional field; tolerate older writers.
        device_path.write_text(
            json.dumps(
                {"user_id": "u", "shelf_id": "s", "provisioned_at": "2026-01-01T00:00:00Z"}
            )
        )
        state = device_state.read(device_path)
        assert state is not None
        assert state.schema_version == 1


class TestWrite:
    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "device.json"
        device_state.write(nested, make_state("u", "s"))
        assert nested.exists()

    def test_round_trips(self, device_path: Path) -> None:
        original = make_state("user-uuid", "shelf-uuid")
        device_state.write(device_path, original)
        loaded = device_state.read(device_path)
        assert loaded == original

    def test_overwrites_existing_atomically(self, device_path: Path) -> None:
        device_state.write(device_path, make_state("u1", "s1"))
        device_state.write(device_path, make_state("u2", "s2"))

        loaded = device_state.read(device_path)
        assert loaded is not None
        assert loaded.user_id == "u2"
        assert loaded.shelf_id == "s2"

    def test_no_temp_file_left_behind(self, tmp_path: Path) -> None:
        device_state.write(tmp_path / "device.json", make_state("u", "s"))
        leftover = list(tmp_path.glob(".device-*.json.tmp"))
        assert leftover == []


class TestDelete:
    def test_returns_true_when_file_existed(self, device_path: Path) -> None:
        device_state.write(device_path, make_state("u", "s"))
        assert device_state.delete(device_path) is True
        assert not device_path.exists()

    def test_returns_false_when_no_file(self, device_path: Path) -> None:
        assert device_state.delete(device_path) is False


class TestIsProvisioned:
    def test_false_when_missing(self, device_path: Path) -> None:
        assert device_state.is_provisioned(device_path) is False

    def test_true_when_present_even_if_malformed(self, device_path: Path) -> None:
        # is_provisioned is the cheap check — it doesn't validate fields.
        # That's by design: callers that need validation use read().
        device_path.write_text("garbage")
        assert device_state.is_provisioned(device_path) is True


class TestMakeState:
    def test_stamps_provisioned_at(self) -> None:
        state = make_state("u", "s")
        # Roughly ISO 8601 UTC; we don't pin exact format here.
        assert state.provisioned_at.endswith("Z")
        assert "T" in state.provisioned_at
        assert state.user_id == "u"
        assert state.shelf_id == "s"
        assert state.schema_version == 1
