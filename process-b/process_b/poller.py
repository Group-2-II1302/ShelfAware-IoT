"""Command poller: backend → Process B → Process A.

Polls ``GET /commands`` for each configured shelf and forwards live ``wake``
commands to Process A via :mod:`ipc`. One of three independent asyncio
tasks; runs until ``stop_event`` is set.

No backoff state
----------------
Unlike the drainer, the poller has no retry loop and no exponential backoff.
The poller's contract is *at-most-once* delivery on a fixed cadence: if
``/commands`` is unreachable for a tick, we miss whatever wakes were live
during that window, and the next successful poll picks up whatever is still
pending. Adding backoff would *delay* recovery, which is the opposite of
what we want.

Per-shelf isolation
-------------------
A failure polling shelf A in a tick must not prevent us from polling shelf
B in the same tick. Each shelf gets its own try/except inside the loop body.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from process_b.backend_client import (
    BackendClient,
    Command,
    PermanentBackendError,
    TransientBackendError,
)
from process_b.ipc import send_wake as default_send_wake

logger = logging.getLogger(__name__)


SendWakeFn = Callable[[str, int], None]
"""Type alias for the wake-sender. Production uses :func:`process_b.ipc.send_wake`;
tests inject a fake to assert calls without needing a real UDP listener."""


async def run_poller(
    *,
    client: BackendClient,
    shelf_ids: Sequence[str],
    interval: float,
    ipc_host: str,
    ipc_port: int,
    stop_event: asyncio.Event,
    send_wake: SendWakeFn = default_send_wake,
    now_fn: Callable[[], datetime] | None = None,
) -> None:
    """Poll the backend until ``stop_event`` is set.

    Parameters
    ----------
    client:
        Open :class:`BackendClient`. Caller owns its lifecycle.
    shelf_ids:
        Configured set of shelves served by this Pi (from
        ``cfg.shelf_ids``). The poller is the only place this set matters;
        the listener and drainer derive ``shelf_id`` from data, not config.
    interval:
        Seconds between ticks. The current sleep is interruptible via
        ``stop_event`` so shutdown is prompt.
    ipc_host, ipc_port:
        Process A's control endpoint. Closed-over so the per-tick code path
        is a single positional call to ``send_wake``.
    stop_event:
        Cleared at startup, set on shutdown.
    send_wake:
        Injectable wake-sender. Production path is the module-level
        :func:`process_b.ipc.send_wake`; tests substitute a recorder.
    now_fn:
        Injectable clock. Defaults to ``datetime.now(UTC)``. Tests use a
        fixed clock to make ``expires_at`` checks deterministic.

    Errors
    ------
    Per-shelf errors are logged and the loop continues. ``CancelledError``
    propagates so ``TaskGroup`` shutdown semantics work.
    """
    clock = now_fn or (lambda: datetime.now(UTC))

    if not shelf_ids:
        logger.warning("poller starting with no configured shelves; loop will idle")

    logger.info(
        "poller started",
        extra={"interval": interval, "shelves": list(shelf_ids)},
    )

    while not stop_event.is_set():
        try:
            await _poll_once(client, shelf_ids, ipc_host, ipc_port, send_wake, clock)
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            logger.exception("poller tick failed unexpectedly")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("poller stopped")


async def _poll_once(
    client: BackendClient,
    shelf_ids: Sequence[str],
    ipc_host: str,
    ipc_port: int,
    send_wake: SendWakeFn,
    clock: Callable[[], datetime],
) -> None:
    """One tick: poll every shelf in turn, forwarding live wakes."""
    for shelf_id in shelf_ids:
        try:
            commands = await client.get_commands(shelf_id=shelf_id)
        except TransientBackendError as exc:
            logger.warning(
                "commands GET transient failure",
                extra={"shelf_id": shelf_id, "status": exc.status, "error": str(exc)},
            )
            continue
        except PermanentBackendError as exc:
            logger.error(
                "commands GET permanent failure",
                extra={"shelf_id": shelf_id, "status": exc.status, "body": exc.body},
            )
            continue

        for cmd in commands:
            _handle_command(cmd, shelf_id, ipc_host, ipc_port, send_wake, clock)


def _handle_command(
    cmd: Command,
    shelf_id: str,
    ipc_host: str,
    ipc_port: int,
    send_wake: SendWakeFn,
    clock: Callable[[], datetime],
) -> None:
    """Decide what to do with one command. Wake commands are forwarded;
    everything else is logged and dropped.
    """
    if cmd.command != "wake":
        logger.info(
            "unsupported command type; ignored",
            extra={"shelf_id": shelf_id, "command_id": cmd.id, "command": cmd.command},
        )
        return

    if _is_expired(cmd.expires_at, clock):
        logger.info(
            "expired wake command; dropped",
            extra={
                "shelf_id": shelf_id,
                "command_id": cmd.id,
                "expires_at": cmd.expires_at,
            },
        )
        return

    logger.info(
        "forwarding wake to process A",
        extra={
            "shelf_id": shelf_id,
            "command_id": cmd.id,
            "ipc_host": ipc_host,
            "ipc_port": ipc_port,
        },
    )
    send_wake(ipc_host, ipc_port)


def _is_expired(expires_at: str, clock: Callable[[], datetime]) -> bool:
    """Return True iff ``expires_at`` is at or before "now" in UTC.

    The backend pre-filters expired commands per the contract, so this is
    defense in depth. If the timestamp is unparseable we treat the command
    as live — better to err toward forwarding than to silently drop on a
    backend bug we don't understand yet. The forwarded wake is still safe
    because Process A treats wakes idempotently.
    """
    try:
        ts = _parse_iso8601(expires_at)
    except ValueError:
        logger.warning(
            "could not parse expires_at; treating as live",
            extra={"expires_at": expires_at},
        )
        return False

    return ts <= clock()


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO 8601 string into a timezone-aware UTC ``datetime``.

    Handles ``Z`` suffix (Python's ``fromisoformat`` accepts it natively
    only from 3.11+, which we require). Naive datetimes are assumed UTC —
    the contract requires UTC; a naive timestamp from the backend would be
    a bug, but treating it as UTC is the least-surprising recovery.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
