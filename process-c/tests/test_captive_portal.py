"""Tests for captive-portal additions: GET /, OS probes, form-encoded POST."""

from __future__ import annotations

import json
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


# ──────────────────────────────────────────────────────────────────────────────
# GET / — captive-portal landing page
# ──────────────────────────────────────────────────────────────────────────────


class TestIndex:
    async def test_serves_html_with_form(self, tmp_path: Path, make_client) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.get("/")
        assert resp.status == 200
        assert "text/html" in resp.headers["Content-Type"]

        body = await resp.text()
        # Smoke check the form is present and points where we expect.
        assert '<form' in body
        assert 'action="/provision"' in body
        assert 'name="ssid"' in body
        assert 'name="password"' in body
        assert 'name="user_id"' in body

    async def test_works_even_when_already_provisioned(
        self, tmp_path: Path, make_client
    ) -> None:
        # Form should still render — the POST handler is what enforces
        # the 409. Keeping the URL stable matters for captive-portal UX.
        device_path = tmp_path / "device.json"
        device_state.write(device_path, device_state.make_state("u", "s"))

        cfg = _cfg(device_path)
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.text()
        assert '<form' in body

    async def test_template_is_cached_across_requests(
        self, tmp_path: Path, make_client
    ) -> None:
        # Simple "doesn't blow up under repeated requests" smoke check —
        # the template is read from disk once via importlib.resources and
        # cached in a module-global.
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        for _ in range(5):
            resp = await client.get("/")
            assert resp.status == 200


# ──────────────────────────────────────────────────────────────────────────────
# Captive-portal probe endpoints
# ──────────────────────────────────────────────────────────────────────────────


class TestCaptiveProbes:
    """The probes deliberately fail each OS's `is there internet?` check
    so the OS pops its captive-portal browser. We assert they return
    bodies/codes that won't be mistaken for "real internet"."""

    @pytest.mark.parametrize(
        "path",
        ["/generate_204", "/gen_204"],
    )
    async def test_android_probe_is_not_204_empty(
        self, tmp_path: Path, make_client, path: str
    ) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.get(path)
        # Real internet returns 204 with empty body. We MUST not.
        assert resp.status != 204
        body = await resp.read()
        assert body, f"Android probe at {path} returned empty body — won't trigger portal"

    @pytest.mark.parametrize(
        "path",
        ["/hotspot-detect.html", "/library/test/success.html"],
    )
    async def test_apple_probe_does_not_contain_success(
        self, tmp_path: Path, make_client, path: str
    ) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.get(path)
        assert resp.status == 200
        body = await resp.text()
        # iOS treats body containing literal "Success" as "internet works".
        assert "Success" not in body, (
            f"Apple probe at {path} contains 'Success' — iOS would skip captive portal"
        )

    @pytest.mark.parametrize(
        "path",
        ["/ncsi.txt", "/connecttest.txt"],
    )
    async def test_windows_probe_is_not_microsoft_ncsi(
        self, tmp_path: Path, make_client, path: str
    ) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.get(path)
        assert resp.status == 200
        body = await resp.text()
        assert body.strip() != "Microsoft NCSI", (
            f"Windows probe at {path} returned exact upstream string — no portal popup"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Form-encoded POST /provision (no-JS browser fallback)
# ──────────────────────────────────────────────────────────────────────────────


class TestProvisionFormEncoded:
    async def test_form_post_returns_html_success_page(
        self, tmp_path: Path, make_client
    ) -> None:
        device_path = tmp_path / "device.json"
        cfg = _cfg(device_path)
        wifi = FakeWifiBackend(next_result=ConnectResult.SUCCESS)
        client = await make_client(cfg, wifi)

        resp = await client.post(
            "/provision",
            data={
                "ssid": "MyWifi",
                "password": "hunter2-strong",
                "user_id": "user-uuid-1",
            },
        )
        # Form submission returns HTML, not JSON.
        assert resp.status == 202
        assert "text/html" in resp.headers["Content-Type"]

        body = await resp.text()
        assert "Setup accepted" in body or "accepted" in body.lower()

        # device.json was still written (state is the same regardless of
        # how the request came in).
        loaded = device_state.read(device_path)
        assert loaded is not None
        assert loaded.user_id == "user-uuid-1"

    async def test_form_post_validation_error_returns_html(
        self, tmp_path: Path, make_client
    ) -> None:
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post(
            "/provision",
            data={"ssid": "x", "password": "short", "user_id": "u"},
        )
        # Validation failure for a form submission still renders HTML.
        assert resp.status == 400
        assert "text/html" in resp.headers["Content-Type"]
        body = await resp.text()
        assert "Setup failed" in body or "error" in body.lower()

    async def test_form_post_already_provisioned_returns_html(
        self, tmp_path: Path, make_client
    ) -> None:
        device_path = tmp_path / "device.json"
        device_state.write(device_path, device_state.make_state("u", "s"))

        cfg = _cfg(device_path)
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post(
            "/provision",
            data={"ssid": "x", "password": "12345678", "user_id": "u2"},
        )
        assert resp.status == 409
        assert "text/html" in resp.headers["Content-Type"]

    async def test_json_post_still_returns_json(
        self, tmp_path: Path, make_client
    ) -> None:
        # Regression: adding form-encoded support shouldn't break the
        # existing JSON contract that Emmanuel and curl/native apps use.
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post(
            "/provision",
            json={
                "ssid": "x",
                "password": "12345678",
                "user_id": "u",
            },
        )
        assert resp.status == 202
        assert "application/json" in resp.headers["Content-Type"]
        body = await resp.json()
        assert body["status"] == "accepted"

    async def test_explicit_accept_html_returns_html_even_for_json_body(
        self, tmp_path: Path, make_client
    ) -> None:
        # Edge case: a browser JS app that explicitly says Accept: text/html
        # (unusual). We honour the header.
        cfg = _cfg(tmp_path / "device.json")
        client = await make_client(cfg, FakeWifiBackend())

        resp = await client.post(
            "/provision",
            json={"ssid": "x", "password": "12345678", "user_id": "u"},
            headers={"Accept": "text/html"},
        )
        assert resp.status == 202
        assert "text/html" in resp.headers["Content-Type"]
