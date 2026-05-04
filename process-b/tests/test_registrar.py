"""Tests for :mod:`process_b.registrar`."""

from __future__ import annotations

import asyncio

import pytest

from process_b.backend_client import (
    PermanentBackendError,
    ShelfRegistrationResponse,
    TransientBackendError,
)
from process_b.registrar import register_with_backoff


SHELF = "shelf-uuid-1"
USER = "user-uuid-1"


class _FakeClient:
    """Minimal stand-in for BackendClient.register_shelf().

    Each call pops the next response from ``responses`` and either returns
    it or raises it. Callers track ``calls`` to assert behaviour.
    """

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def register_shelf(
        self, *, shelf_id: str, user_id: str
    ) -> ShelfRegistrationResponse:
        self.calls.append((shelf_id, user_id))
        if not self.responses:
            raise AssertionError("ran out of canned responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# Patch _BACKOFF_INITIAL_SEC to something tiny so tests run fast.
@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    from process_b import registrar as _r

    monkeypatch.setattr(_r, "_BACKOFF_INITIAL_SEC", 0.001)
    monkeypatch.setattr(_r, "_BACKOFF_CAP_SEC", 0.01)


class TestRegisterWithBackoff:
    async def test_success_first_try(self) -> None:
        client = _FakeClient([ShelfRegistrationResponse(shelf_id=SHELF)])
        stop = asyncio.Event()

        await register_with_backoff(
            client=client, shelf_id=SHELF, user_id=USER, stop_event=stop
        )

        assert client.calls == [(SHELF, USER)]

    async def test_retries_on_transient_then_succeeds(self) -> None:
        client = _FakeClient(
            [
                TransientBackendError("net dead", status=502),
                TransientBackendError("net dead", status=503),
                ShelfRegistrationResponse(shelf_id=SHELF),
            ]
        )
        stop = asyncio.Event()

        await register_with_backoff(
            client=client, shelf_id=SHELF, user_id=USER, stop_event=stop
        )

        assert len(client.calls) == 3

    async def test_permanent_failure_exits_immediately(self) -> None:
        client = _FakeClient(
            [
                PermanentBackendError(
                    "validation failed", status=400, body='{"error":"x"}'
                )
            ]
        )
        stop = asyncio.Event()

        await register_with_backoff(
            client=client, shelf_id=SHELF, user_id=USER, stop_event=stop
        )

        # Permanent → no retry.
        assert len(client.calls) == 1

    async def test_stop_event_breaks_retry_loop(self) -> None:
        # Always-transient: would retry forever without stop_event.
        client = _FakeClient(
            [TransientBackendError("dead", status=503) for _ in range(100)]
        )
        stop = asyncio.Event()

        async def trip_after_some_attempts() -> None:
            # Wait until at least one retry has happened.
            for _ in range(200):
                if len(client.calls) >= 2:
                    stop.set()
                    return
                await asyncio.sleep(0.001)
            stop.set()  # safety

        await asyncio.gather(
            register_with_backoff(
                client=client, shelf_id=SHELF, user_id=USER, stop_event=stop
            ),
            trip_after_some_attempts(),
        )

        # Should have stopped well before the 100 canned responses ran out.
        assert len(client.calls) < 100

    async def test_unexpected_exception_is_treated_as_transient(self) -> None:
        client = _FakeClient(
            [
                RuntimeError("something wild"),
                ShelfRegistrationResponse(shelf_id=SHELF),
            ]
        )
        stop = asyncio.Event()

        await register_with_backoff(
            client=client, shelf_id=SHELF, user_id=USER, stop_event=stop
        )

        assert len(client.calls) == 2
