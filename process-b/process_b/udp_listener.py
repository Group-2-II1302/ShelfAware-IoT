"""UDP listener: Process A → SQLite outbox.

One ``asyncio.DatagramProtocol`` per listener. Each datagram is a JSON UTF-8
encoded :class:`IncomingReading`. On valid input, schedule an
``insert_reading`` task and return immediately so the event loop can keep
servicing the socket. On invalid input, log at WARN and drop the datagram —
the listener never crashes on bad data.

Why ``create_task`` instead of awaiting
---------------------------------------
``DatagramProtocol.datagram_received`` is a sync callback. Doing real work
inline would block the event loop and risk dropped datagrams (UDP gives no
backpressure). Spawning a task lets the loop return to the socket
immediately.

Tasks are tracked in a set with a ``done_callback`` so they're not
garbage-collected mid-flight (CPython's documented gotcha for
``asyncio.create_task``).

The listener's two contracts
----------------------------
- :func:`start_listener` opens the socket and returns the transport plus a
  way to stop. ``main.py`` calls it during boot, before the drainer/poller,
  so datagrams are buffered into the outbox even while the network side is
  warming up.
- The :class:`TelemetryProtocol` class is exposed for tests, which inject
  raw bytes via ``datagram_received`` without binding a real socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import cast

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from process_b import db

logger = logging.getLogger(__name__)


class IncomingReading(BaseModel):
    """Wire-format datagram from Process A.

    JSON UTF-8::

        {
          "shelf_id": "<UUIDv4>",
          "scale_index": <int >= 0>,
          "est_grams": <finite float>,
          "sampled_at": "<ISO 8601 UTC string>"
        }

    ``sampled_at`` is kept as a string — the contract specifies ISO 8601
    UTC, and Process B forwards it verbatim to the backend. Round-tripping
    through a ``datetime`` would risk timezone-stripping bugs.
    """

    model_config = ConfigDict(extra="forbid")

    shelf_id: str = Field(min_length=1)
    scale_index: int = Field(ge=0)
    est_grams: float
    sampled_at: str = Field(min_length=1)

    @field_validator("est_grams")
    @classmethod
    def _est_grams_must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("est_grams must be finite (no NaN, no infinity)")
        return v


class TelemetryProtocol(asyncio.DatagramProtocol):
    """Receives one datagram at a time and persists it asynchronously.

    Exposed for tests. Production code uses :func:`start_listener`, which
    binds a real socket and returns the underlying transport.
    """

    def __init__(self, db_conn: aiosqlite.Connection) -> None:
        self._db = db_conn
        self._tasks: set[asyncio.Task[object]] = set()
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast("asyncio.DatagramTransport", transport)
        sock = transport.get_extra_info("sockname")
        logger.info("udp listener bound", extra={"sockname": sock})

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            reading = _parse_datagram(data)
        except _DatagramError as exc:
            logger.warning(
                "udp datagram dropped",
                extra={
                    "addr": addr,
                    "reason": exc.reason,
                    "size": len(data),
                },
            )
            return

        task = asyncio.create_task(self._persist(reading, addr))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def error_received(self, exc: Exception) -> None:
        logger.warning("udp listener error_received", extra={"error": str(exc)})

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            logger.warning("udp listener connection_lost", extra={"error": str(exc)})
        else:
            logger.info("udp listener connection_lost")

    async def aclose(self) -> None:
        """Stop accepting datagrams and wait for in-flight inserts to finish.

        Called on shutdown by ``main.py``. Closing the transport first
        guarantees no new ``datagram_received`` callbacks fire; awaiting
        in-flight tasks guarantees no row is dropped.
        """
        if self._transport is not None:
            self._transport.close()
            self._transport = None

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _persist(self, reading: IncomingReading, addr: tuple[str, int]) -> None:
        """Insert one validated reading. Errors are logged, never raised.

        Raising out of a background task would only be visible at task GC
        time and would not crash the listener — but we'd lose the row's
        context. Logging here keeps the diagnostic close to the cause.
        """
        try:
            reading_id = await db.insert_reading(
                self._db,
                shelf_id=reading.shelf_id,
                scale_index=reading.scale_index,
                est_grams=reading.est_grams,
                sampled_at=reading.sampled_at,
                metadata=None,
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "failed to persist incoming reading",
                extra={
                    "addr": addr,
                    "shelf_id": reading.shelf_id,
                    "scale_index": reading.scale_index,
                },
            )
            return

        logger.debug(
            "udp reading persisted",
            extra={
                "addr": addr,
                "reading_id": reading_id,
                "shelf_id": reading.shelf_id,
                "scale_index": reading.scale_index,
            },
        )


class _DatagramError(Exception):
    """Internal: parse/validate failure with a short reason string for logs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse_datagram(data: bytes) -> IncomingReading:
    """Decode UTF-8 + JSON + pydantic. Raises :class:`_DatagramError` on any failure."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _DatagramError(f"invalid utf-8: {exc.reason}") from exc

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _DatagramError(f"invalid json: {exc.msg}") from exc

    if not isinstance(obj, dict):
        raise _DatagramError("json root is not an object")

    try:
        return IncomingReading.model_validate(obj)
    except ValidationError as exc:
        raise _DatagramError(f"schema mismatch: {exc.errors(include_url=False)}") from exc


async def start_listener(
    *,
    db_conn: aiosqlite.Connection,
    host: str,
    port: int,
) -> TelemetryProtocol:
    """Bind the listener and return the protocol instance.

    ``main.py`` keeps the returned protocol so it can call ``aclose()`` on
    shutdown. The listener starts receiving datagrams as soon as this
    coroutine returns.
    """
    loop = asyncio.get_running_loop()
    protocol = TelemetryProtocol(db_conn)
    await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=(host, port),
    )
    return protocol
