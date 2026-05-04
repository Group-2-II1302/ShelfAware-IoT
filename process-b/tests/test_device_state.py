"""Tests for :mod:`process_b.device_state` (Process B's read-only view of device.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from process_b import device_state
from process_b.device_state import DeviceState, DeviceStateError


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

    def test_default_schema_version_when_missing(self, device_path: Path) -> None:
        device_path.write_text(
            json.dumps(
                {"user_id": "u", "shelf_id": "s", "provisioned_at": "2026-01-01T00:00:00Z"}
            )
        )
        state = device_state.read(device_path)
        assert state is not None
        assert state.schema_version == 1

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
