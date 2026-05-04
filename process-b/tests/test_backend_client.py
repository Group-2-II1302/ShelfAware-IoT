"""Tests for :mod:`process_b.backend_client`.

Uses ``respx`` to mock httpx at the transport layer. We never hit a real
network. Each test instantiates its own client; the ``async with`` form makes
shutdown order obvious.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from process_b.backend_client import (
    BackendClient,
    Command,
    PermanentBackendError,
    ReadingPayload,
    TransientBackendError,
)


BASE_URL = "https://api.example.com"
API_KEY = "secret-pi-key"
SHELF_A = "550e8400-e29b-41d4-a716-446655440000"


def _client() -> BackendClient:
    return BackendClient(base_url=BASE_URL, api_key=API_KEY)


def _readings(n: int = 1) -> list[ReadingPayload]:
    return [
        ReadingPayload(reading_id=f"00000000-0000-4000-8000-00000000000{i}", scale_index=i, est_grams=100.0 + i)
        for i in range(n)
    ]


class TestPostTelemetry:
    @respx.mock
    async def test_2xx_returns_parsed_response(self) -> None:
        route = respx.post(f"{BASE_URL}/telemetry").mock(
            return_value=httpx.Response(200, json={"accepted": 2, "skipped": []})
        )

        async with _client() as client:
            result = await client.post_telemetry(
                shelf_id=SHELF_A,
                sampled_at="2026-04-21T08:30:00Z",
                metadata={"battery": 88},
                readings=_readings(2),
            )

        assert result.accepted == 2
        assert result.skipped == []
        assert route.called

    @respx.mock
    async def test_sends_required_headers_and_body(self) -> None:
        route = respx.post(f"{BASE_URL}/telemetry").mock(
            return_value=httpx.Response(200, json={"accepted": 1, "skipped": []})
        )

        async with _client() as client:
            await client.post_telemetry(
                shelf_id=SHELF_A,
                sampled_at="2026-04-21T08:30:00Z",
                metadata=None,
                readings=_readings(1),
            )

        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert request.headers["X-Shelf-Id"] == SHELF_A

        import json as _json

        body = _json.loads(request.content)
        assert body["shelf_id"] == SHELF_A
        assert body["sampled_at"] == "2026-04-21T08:30:00Z"
        assert body["metadata"] is None
        assert len(body["readings"]) == 1
        assert body["readings"][0]["scale_index"] == 0

    @respx.mock
    async def test_2xx_with_skipped_entries_parses(self) -> None:
        respx.post(f"{BASE_URL}/telemetry").mock(
            return_value=httpx.Response(
                200,
                json={
                    "accepted": 1,
                    "skipped": [
                        {"reading_id": "abc", "reason": "unknown_scale"},
                        {"reading_id": "def", "reason": "future_unknown_reason"},
                    ],
                },
            )
        )

        async with _client() as client:
            result = await client.post_telemetry(
                shelf_id=SHELF_A,
                sampled_at="2026-04-21T08:30:00Z",
                metadata=None,
                readings=_readings(3),
            )

        assert result.accepted == 1
        assert [s.reading_id for s in result.skipped] == ["abc", "def"]
        assert result.skipped[0].reason == "unknown_scale"

    @respx.mock
    async def test_4xx_raises_permanent_error(self) -> None:
        respx.post(f"{BASE_URL}/telemetry").mock(
            return_value=httpx.Response(400, text="bad request body")
        )

        async with _client() as client:
            with pytest.raises(PermanentBackendError) as exc:
                await client.post_telemetry(
                    shelf_id=SHELF_A,
                    sampled_at="2026-04-21T08:30:00Z",
                    metadata=None,
                    readings=_readings(1),
                )

        assert exc.value.status == 400
        assert "bad request body" in (exc.value.body or "")

    @respx.mock
    async def test_5xx_raises_transient_error(self) -> None:
        respx.post(f"{BASE_URL}/telemetry").mock(
            return_value=httpx.Response(503, text="upstream down")
        )

        async with _client() as client:
            with pytest.raises(TransientBackendError) as exc:
                await client.post_telemetry(
                    shelf_id=SHELF_A,
                    sampled_at="2026-04-21T08:30:00Z",
                    metadata=None,
                    readings=_readings(1),
                )

        assert exc.value.status == 503

    @respx.mock
    async def test_network_error_is_transient(self) -> None:
        respx.post(f"{BASE_URL}/telemetry").mock(side_effect=httpx.ConnectError("refused"))

        async with _client() as client:
            with pytest.raises(TransientBackendError) as exc:
                await client.post_telemetry(
                    shelf_id=SHELF_A,
                    sampled_at="2026-04-21T08:30:00Z",
                    metadata=None,
                    readings=_readings(1),
                )

        assert exc.value.status is None
        assert "network" in str(exc.value).lower()

    @respx.mock
    async def test_timeout_is_transient(self) -> None:
        respx.post(f"{BASE_URL}/telemetry").mock(side_effect=httpx.ReadTimeout("slow"))

        async with _client() as client:
            with pytest.raises(TransientBackendError) as exc:
                await client.post_telemetry(
                    shelf_id=SHELF_A,
                    sampled_at="2026-04-21T08:30:00Z",
                    metadata=None,
                    readings=_readings(1),
                )

        assert exc.value.status is None
        assert "timeout" in str(exc.value).lower()

    @respx.mock
    async def test_2xx_malformed_body_is_permanent(self) -> None:
        respx.post(f"{BASE_URL}/telemetry").mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"})
        )

        async with _client() as client:
            with pytest.raises(PermanentBackendError):
                await client.post_telemetry(
                    shelf_id=SHELF_A,
                    sampled_at="2026-04-21T08:30:00Z",
                    metadata=None,
                    readings=_readings(1),
                )

    @respx.mock
    async def test_2xx_non_json_body_is_permanent(self) -> None:
        respx.post(f"{BASE_URL}/telemetry").mock(
            return_value=httpx.Response(200, text="not json at all")
        )

        async with _client() as client:
            with pytest.raises(PermanentBackendError):
                await client.post_telemetry(
                    shelf_id=SHELF_A,
                    sampled_at="2026-04-21T08:30:00Z",
                    metadata=None,
                    readings=_readings(1),
                )


class TestGetCommands:
    @respx.mock
    async def test_returns_empty_list_when_no_commands(self) -> None:
        respx.get(f"{BASE_URL}/commands").mock(
            return_value=httpx.Response(200, json={"commands": []})
        )

        async with _client() as client:
            result = await client.get_commands(shelf_id=SHELF_A)

        assert result == []

    @respx.mock
    async def test_parses_wake_command(self) -> None:
        respx.get(f"{BASE_URL}/commands").mock(
            return_value=httpx.Response(
                200,
                json={
                    "commands": [
                        {
                            "id": "cmd-1",
                            "command": "wake",
                            "payload": None,
                            "expires_at": "2026-04-21T08:35:00Z",
                        }
                    ]
                },
            )
        )

        async with _client() as client:
            result = await client.get_commands(shelf_id=SHELF_A)

        assert len(result) == 1
        cmd = result[0]
        assert isinstance(cmd, Command)
        assert cmd.id == "cmd-1"
        assert cmd.command == "wake"
        assert cmd.expires_at == "2026-04-21T08:35:00Z"

    @respx.mock
    async def test_sends_required_headers(self) -> None:
        route = respx.get(f"{BASE_URL}/commands").mock(
            return_value=httpx.Response(200, json={"commands": []})
        )

        async with _client() as client:
            await client.get_commands(shelf_id=SHELF_A)

        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert request.headers["X-Shelf-Id"] == SHELF_A

    @respx.mock
    async def test_5xx_raises_transient_error(self) -> None:
        respx.get(f"{BASE_URL}/commands").mock(return_value=httpx.Response(502))

        async with _client() as client:
            with pytest.raises(TransientBackendError):
                await client.get_commands(shelf_id=SHELF_A)

    @respx.mock
    async def test_4xx_raises_permanent_error(self) -> None:
        respx.get(f"{BASE_URL}/commands").mock(return_value=httpx.Response(401, text="bad token"))

        async with _client() as client:
            with pytest.raises(PermanentBackendError) as exc:
                await client.get_commands(shelf_id=SHELF_A)

        assert exc.value.status == 401


class TestRegisterShelf:
    @respx.mock
    async def test_2xx_returns_parsed_response(self) -> None:
        route = respx.post(f"{BASE_URL}/shelves").mock(
            return_value=httpx.Response(
                200, json={"shelf_id": SHELF_A, "user_id": "u1"}
            )
        )

        async with _client() as client:
            result = await client.register_shelf(shelf_id=SHELF_A, user_id="u1")

        assert result.shelf_id == SHELF_A
        assert route.called

    @respx.mock
    async def test_sends_required_headers_and_body(self) -> None:
        captured: dict[str, object] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            captured["auth"] = request.headers.get("Authorization")
            captured["shelf_header"] = request.headers.get("X-Shelf-Id")
            return httpx.Response(201, json={"shelf_id": SHELF_A})

        respx.post(f"{BASE_URL}/shelves").mock(side_effect=capture)

        async with _client() as client:
            await client.register_shelf(shelf_id=SHELF_A, user_id="u1")

        import json as _json

        body = _json.loads(captured["body"])  # type: ignore[arg-type]
        assert body == {"shelf_id": SHELF_A, "user_id": "u1"}
        assert captured["auth"] == f"Bearer {API_KEY}"
        assert captured["shelf_header"] == SHELF_A

    @respx.mock
    async def test_5xx_raises_transient_error(self) -> None:
        respx.post(f"{BASE_URL}/shelves").mock(
            return_value=httpx.Response(503, text="upstream down")
        )

        async with _client() as client:
            with pytest.raises(TransientBackendError) as exc:
                await client.register_shelf(shelf_id=SHELF_A, user_id="u1")

        assert exc.value.status == 503

    @respx.mock
    async def test_4xx_raises_permanent_error(self) -> None:
        respx.post(f"{BASE_URL}/shelves").mock(
            return_value=httpx.Response(400, text="invalid user_id")
        )

        async with _client() as client:
            with pytest.raises(PermanentBackendError) as exc:
                await client.register_shelf(shelf_id=SHELF_A, user_id="u1")

        assert exc.value.status == 400


class TestLifecycle:
    async def test_aclose_closes_underlying_client(self) -> None:
        client = _client()
        assert not client._client.is_closed  # type: ignore[reportPrivateUsage]
        await client.aclose()
        assert client._client.is_closed  # type: ignore[reportPrivateUsage]

    async def test_context_manager_closes_on_exit(self) -> None:
        async with _client() as client:
            inner = client._client  # type: ignore[reportPrivateUsage]
            assert not inner.is_closed
        assert inner.is_closed
