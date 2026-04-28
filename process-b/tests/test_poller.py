"""Tests for :mod:`process_b.poller`.

Strategy
--------
- A fake :class:`BackendClient` returns programmable responses per shelf.
- A fake ``send_wake`` records calls.
- The clock is injected via ``now_fn`` so expiry tests are deterministic.
- The loop is exercised end-to-end against a ``stop_event`` we set after
  one or two ticks.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from process_b.backend_client import (
    Command,
    PermanentBackendError,
    TransientBackendError,
)
from process_b.poller import _handle_command, _is_expired, run_poller


SHELF_A = "550e8400-e29b-41d4-a716-446655440000"
SHELF_B = "660e8400-e29b-41d4-a716-446655440001"
IPC_HOST = "127.0.0.1"
IPC_PORT = 6001
NOW = datetime(2026, 4, 21, 8, 30, 0, tzinfo=UTC)


def _wake(
    *,
    cmd_id: str = "cmd-1",
    expires_at: str = "2026-04-21T08:35:00Z",
    payload: object | None = None,
) -> Command:
    return Command(id=cmd_id, command="wake", payload=payload, expires_at=expires_at)


def _fixed_now() -> datetime:
    return NOW


@dataclass
class FakeBackendClient:
    """Programmable :class:`BackendClient` stand-in.

    ``commands_by_shelf`` is a per-shelf queue of *outcomes*. Each outcome
    is either:

    - ``list[Command]`` — returned to the caller.
    - an exception instance — raised.

    A shelf with an empty queue returns ``[]`` (no commands).
    """

    commands_by_shelf: dict[str, deque[object]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    calls_by_shelf: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def queue(self, shelf_id: str, outcomes: list[object]) -> None:
        self.commands_by_shelf[shelf_id].extend(outcomes)

    async def get_commands(self, *, shelf_id: str) -> list[Command]:
        self.calls_by_shelf[shelf_id] += 1
        q = self.commands_by_shelf.get(shelf_id)
        if not q:
            return []
        outcome = q.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, list)
        return outcome


@dataclass
class FakeIPC:
    calls: list[tuple[str, int]] = field(default_factory=list)

    def __call__(self, host: str, port: int) -> None:
        self.calls.append((host, port))


class TestIsExpired:
    def test_future_timestamp_is_live(self) -> None:
        future = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        assert _is_expired(future, _fixed_now) is False

    def test_past_timestamp_is_expired(self) -> None:
        past = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        assert _is_expired(past, _fixed_now) is True

    def test_exactly_now_is_expired(self) -> None:
        # Boundary: an expires_at equal to "now" is treated as expired.
        same = NOW.isoformat().replace("+00:00", "Z")
        assert _is_expired(same, _fixed_now) is True

    def test_unparseable_timestamp_treated_as_live(self) -> None:
        assert _is_expired("not a date", _fixed_now) is False

    def test_naive_timestamp_assumed_utc(self) -> None:
        # Without offset/Z; ISO 8601 says ambiguous, we assume UTC.
        future_naive = (NOW + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        assert _is_expired(future_naive, _fixed_now) is False


class TestHandleCommand:
    def test_live_wake_calls_send_wake(self) -> None:
        ipc = FakeIPC()
        cmd = _wake(expires_at=(NOW + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"))
        _handle_command(cmd, SHELF_A, IPC_HOST, IPC_PORT, ipc, _fixed_now)
        assert ipc.calls == [(IPC_HOST, IPC_PORT)]

    def test_expired_wake_does_not_call_send_wake(self) -> None:
        ipc = FakeIPC()
        cmd = _wake(expires_at=(NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"))
        _handle_command(cmd, SHELF_A, IPC_HOST, IPC_PORT, ipc, _fixed_now)
        assert ipc.calls == []

    def test_unsupported_command_is_ignored(self) -> None:
        ipc = FakeIPC()
        cmd = Command(
            id="cmd-x",
            command="reboot",  # not "wake"
            payload=None,
            expires_at=(NOW + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        )
        _handle_command(cmd, SHELF_A, IPC_HOST, IPC_PORT, ipc, _fixed_now)
        assert ipc.calls == []


class TestPollerLoop:
    async def test_stop_event_already_set_exits_immediately(self) -> None:
        client = FakeBackendClient()
        ipc = FakeIPC()
        stop = asyncio.Event()
        stop.set()

        await asyncio.wait_for(
            run_poller(
                client=client,  # type: ignore[arg-type]
                shelf_ids=[SHELF_A],
                interval=0.01,
                ipc_host=IPC_HOST,
                ipc_port=IPC_PORT,
                stop_event=stop,
                send_wake=ipc,
                now_fn=_fixed_now,
            ),
            timeout=1.0,
        )
        assert client.calls_by_shelf == {}
        assert ipc.calls == []

    async def test_one_tick_polls_each_shelf_once(self) -> None:
        client = FakeBackendClient()
        ipc = FakeIPC()
        stop = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(
            run_poller(
                client=client,  # type: ignore[arg-type]
                shelf_ids=[SHELF_A, SHELF_B],
                interval=10.0,  # long; we'll stop before second tick
                ipc_host=IPC_HOST,
                ipc_port=IPC_PORT,
                stop_event=stop,
                send_wake=ipc,
                now_fn=_fixed_now,
            ),
            stopper(),
        )

        assert client.calls_by_shelf[SHELF_A] == 1
        assert client.calls_by_shelf[SHELF_B] == 1

    async def test_live_wake_is_forwarded(self) -> None:
        client = FakeBackendClient()
        future = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        client.queue(SHELF_A, [[_wake(cmd_id="cmd-A", expires_at=future)]])

        ipc = FakeIPC()
        stop = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(
            run_poller(
                client=client,  # type: ignore[arg-type]
                shelf_ids=[SHELF_A],
                interval=10.0,
                ipc_host=IPC_HOST,
                ipc_port=IPC_PORT,
                stop_event=stop,
                send_wake=ipc,
                now_fn=_fixed_now,
            ),
            stopper(),
        )

        assert ipc.calls == [(IPC_HOST, IPC_PORT)]

    async def test_expired_wake_is_dropped(self) -> None:
        client = FakeBackendClient()
        past = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        client.queue(SHELF_A, [[_wake(cmd_id="cmd-A", expires_at=past)]])

        ipc = FakeIPC()
        stop = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(
            run_poller(
                client=client,  # type: ignore[arg-type]
                shelf_ids=[SHELF_A],
                interval=10.0,
                ipc_host=IPC_HOST,
                ipc_port=IPC_PORT,
                stop_event=stop,
                send_wake=ipc,
                now_fn=_fixed_now,
            ),
            stopper(),
        )

        assert ipc.calls == []

    async def test_transient_failure_one_shelf_does_not_block_other(self) -> None:
        client = FakeBackendClient()
        future = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        client.queue(SHELF_A, [TransientBackendError("upstream down", status=503)])
        client.queue(SHELF_B, [[_wake(cmd_id="cmd-B", expires_at=future)]])

        ipc = FakeIPC()
        stop = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(
            run_poller(
                client=client,  # type: ignore[arg-type]
                shelf_ids=[SHELF_A, SHELF_B],
                interval=10.0,
                ipc_host=IPC_HOST,
                ipc_port=IPC_PORT,
                stop_event=stop,
                send_wake=ipc,
                now_fn=_fixed_now,
            ),
            stopper(),
        )

        # Both shelves were polled, and the live wake on B was forwarded.
        assert client.calls_by_shelf[SHELF_A] == 1
        assert client.calls_by_shelf[SHELF_B] == 1
        assert ipc.calls == [(IPC_HOST, IPC_PORT)]

    async def test_permanent_failure_does_not_kill_loop(self) -> None:
        client = FakeBackendClient()
        client.queue(SHELF_A, [PermanentBackendError("bad token", status=401, body="x")])
        # Second tick should still happen.
        future = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        client.queue(SHELF_A, [[_wake(cmd_id="cmd-A", expires_at=future)]])

        ipc = FakeIPC()
        stop = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(0.05)  # let two ticks happen at interval=0.01
            stop.set()

        await asyncio.gather(
            run_poller(
                client=client,  # type: ignore[arg-type]
                shelf_ids=[SHELF_A],
                interval=0.01,
                ipc_host=IPC_HOST,
                ipc_port=IPC_PORT,
                stop_event=stop,
                send_wake=ipc,
                now_fn=_fixed_now,
            ),
            stopper(),
        )

        assert client.calls_by_shelf[SHELF_A] >= 2
        assert ipc.calls == [(IPC_HOST, IPC_PORT)]

    async def test_unsupported_command_does_not_call_ipc(self) -> None:
        client = FakeBackendClient()
        future = (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        weird = Command(id="x", command="reboot", payload=None, expires_at=future)
        client.queue(SHELF_A, [[weird]])

        ipc = FakeIPC()
        stop = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(
            run_poller(
                client=client,  # type: ignore[arg-type]
                shelf_ids=[SHELF_A],
                interval=10.0,
                ipc_host=IPC_HOST,
                ipc_port=IPC_PORT,
                stop_event=stop,
                send_wake=ipc,
                now_fn=_fixed_now,
            ),
            stopper(),
        )

        assert ipc.calls == []

    async def test_empty_shelf_ids_loop_idles_then_exits(self) -> None:
        client = FakeBackendClient()
        ipc = FakeIPC()
        stop = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(
            run_poller(
                client=client,  # type: ignore[arg-type]
                shelf_ids=[],
                interval=0.01,
                ipc_host=IPC_HOST,
                ipc_port=IPC_PORT,
                stop_event=stop,
                send_wake=ipc,
                now_fn=_fixed_now,
            ),
            stopper(),
        )

        assert client.calls_by_shelf == {}
        assert ipc.calls == []
