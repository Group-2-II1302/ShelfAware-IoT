"""Tests for :mod:`process_c.handlers`.

Uses ``aiohttp.test_utils`` to spin up the real ``web.Application`` against
an ephemeral port and hit it with the bundled test client.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from process_c import device_state
from process_c.config import Config
from process_c.main import build_app
from process_c.wifi_backend import ConnectResult, FakeWifiBackend


def _cfg(device_path: Path, *, allow_reset: bool = False) -> Config:
    return Config(
        bind_host="127.0.0.1",
        bind_port=0,
        device_file_path=str(device_path),
        allow_reset_endpoint=allow_reset,
        log_level="DEBUG",
        dev_mode=True,
    )


@pytest_asyncio.fixture
async def make_client():
    """Returns a factory that builds a TestClient for a given (cfg, wifi)."""
    clients: list[TestClient] = []

    async def factory(cfg: Config, wifi: FakeWifiBackend) -> TestClient:
        app = build_app(cfg, wifi)
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield factory

    for c in clients:
        await c.close()


class TestHealth:
    async def test_returns_device_descriptor(self, tmp_path: Path, make_client) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.get("/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["device"] == "shelfaware-pi"
        assert body["ready_for_provision"] is True
        assert body["already_provisioned"] is False

    async def test_already_provisioned_when_file_present(
        self, tmp_path: Path, make_client
    ) -> None:
        device_path = tmp_path / "device.json"
        device_state.write(device_path, device_state.make_state("u", "s"))

        cfg = _cfg(device_path)
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.get("/health")
        body = await resp.json()
        assert body["already_provisioned"] is True

    async def test_cors_headers_present(self, tmp_path: Path, make_client) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.get("/health")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_options_preflight(self, tmp_path: Path, make_client) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.options("/provision")
        assert resp.status == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"


class TestProvision:
    async def test_happy_path_writes_device_json_and_returns_202(
        self, tmp_path: Path, make_client
    ) -> None:
        device_path = tmp_path / "device.json"
        cfg = _cfg(device_path)
        wifi = FakeWifiBackend(next_result=ConnectResult.SUCCESS)
        client = await make_client(cfg, wifi)

        resp = await client.post(
            "/provision",
            json={
                "ssid": "MyHomeWiFi",
                "password": "hunter2-strong",
                "user_id": "user-uuid-1",
            },
        )
        assert resp.status == 202
        body = await resp.json()
        assert body["status"] == "accepted"
        assert isinstance(body["shelf_id"], str) and len(body["shelf_id"]) >= 32

        # device.json must exist with the user_id and matching shelf_id
        loaded = device_state.read(device_path)
        assert loaded is not None
        assert loaded.user_id == "user-uuid-1"
        assert loaded.shelf_id == body["shelf_id"]

        # The background WiFi switch should fire — give it a moment.
        for _ in range(20):
            if wifi.calls:
                break
            await asyncio.sleep(0.01)
        assert wifi.calls, "wifi backend was never called"
        assert wifi.calls[0][0] == "MyHomeWiFi"
        assert wifi.calls[0][1] == "hunter2-strong"

    async def test_409_when_already_provisioned(
        self, tmp_path: Path, make_client
    ) -> None:
        device_path = tmp_path / "device.json"
        device_state.write(device_path, device_state.make_state("u", "s"))

        cfg = _cfg(device_path)
        wifi = FakeWifiBackend()
        client = await make_client(cfg, wifi)

        resp = await client.post(
            "/provision",
            json={"ssid": "x", "password": "12345678", "user_id": "u2"},
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["error"] == "already_provisioned"
        assert wifi.calls == []

    async def test_400_on_invalid_json(self, tmp_path: Path, make_client) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post("/provision", data="not json")
        assert resp.status == 400
        body = await resp.json()
        assert body["error"] == "validation"

    @pytest.mark.parametrize(
        "payload,reason",
        [
            ({}, "all fields missing"),
            ({"ssid": "x"}, "missing password and user_id"),
            ({"ssid": "x", "password": "12345678"}, "missing user_id"),
            ({"ssid": "", "password": "12345678", "user_id": "u"}, "empty ssid"),
            ({"ssid": "x", "password": "short", "user_id": "u"}, "password too short"),
            (
                {"ssid": "x", "password": "12345678", "user_id": "u", "extra": True},
                "extra field forbidden",
            ),
        ],
    )
    async def test_400_on_validation_failures(
        self, tmp_path: Path, make_client, payload: dict, reason: str
    ) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post("/provision", json=payload)
        assert resp.status == 400, f"expected 400 for: {reason}"
        body = await resp.json()
        assert body["error"] == "validation"

    async def test_500_when_device_file_unwriteable(
        self, tmp_path: Path, make_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(device_state, "write", boom)

        resp = await client.post(
            "/provision",
            json={"ssid": "x", "password": "12345678", "user_id": "u"},
        )
        assert resp.status == 500
        body = await resp.json()
        assert body["error"] == "persistence"


class TestReset:
    async def test_route_not_registered_when_disabled(
        self, tmp_path: Path, make_client
    ) -> None:
        cfg = _cfg(tmp_path / "device.json", allow_reset=False)
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post("/reset")
        assert resp.status == 404

    async def test_deletes_device_file_when_enabled(
        self, tmp_path: Path, make_client
    ) -> None:
        device_path = tmp_path / "device.json"
        device_state.write(device_path, device_state.make_state("u", "s"))

        cfg = _cfg(device_path, allow_reset=True)
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post("/reset")
        assert resp.status == 200
        body = await resp.json()
        assert body["deleted"] is True
        assert not device_path.exists()

    async def test_reset_idempotent_when_no_file(
        self, tmp_path: Path, make_client
    ) -> None:
        cfg = _cfg(tmp_path / "device.json", allow_reset=True)
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post("/reset")
        assert resp.status == 200
        body = await resp.json()
        assert body["deleted"] is False
