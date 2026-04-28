"""Outbox drainer: claim → POST /telemetry → delete (or bump, or back off).

The drainer is one of three independent asyncio tasks. It runs a periodic
loop that:

1. Claims a batch of non-quarantined readings from the SQLite outbox.
2. Groups them by ``shelf_id`` (one HTTP request per shelf).
3. POSTs each group to ``/telemetry``.

Per-group outcomes
------------------
- **2xx** — Delete every reading in the group, including those returned in
  ``skipped[]`` (the backend has terminally handled them; they're typically
  ``unknown_scale`` config-issue rejections, logged at WARN). ``reject_count``
  is **not** bumped on the skipped path — it's reserved for whole-batch 4xx.
- **PermanentBackendError (4xx, malformed-200)** — Bump ``reject_count`` for
  every reading in the group. Three such bumps quarantines a row; the
  drainer skips quarantined rows on subsequent ticks.
- **TransientBackendError (5xx, network, timeout)** — Don't touch the rows.
  They'll be reclaimed on the next tick. The drainer's outer sleep grows
  exponentially (1s → 2s → 4s → ... cap 30s) until the next *successful*
  tick, then resets to ``interval``.

Single drainer per process. No cross-task coordination needed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

import aiosqlite

from process_b import db
from process_b.backend_client import (
    BackendClient,
    PermanentBackendError,
    ReadingPayload,
    TransientBackendError,
)

logger = logging.getLogger(__name__)


_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_CAP_SEC = 30.0
_BACKOFF_FACTOR = 2.0


@dataclass(slots=True)
class _BackoffState:
    """Exponential backoff with a cap. Resettable.

    Kept as an explicit object rather than locals so tests can drive it
    deterministically without mocking time.
    """

    initial: float = _BACKOFF_INITIAL_SEC
    cap: float = _BACKOFF_CAP_SEC
    factor: float = _BACKOFF_FACTOR
    _current: float = 0.0

    def next_delay(self) -> float:
        """Return the next backoff delay and advance the state."""
        if self._current <= 0:
            self._current = self.initial
        else:
            self._current = min(self._current * self.factor, self.cap)
        return self._current

    def reset(self) -> None:
        self._current = 0.0


async def run_drainer(
    *,
    db_conn: aiosqlite.Connection,
    client: BackendClient,
    interval: float,
    batch_size: int,
    stop_event: asyncio.Event,
) -> None:
    """Drain the outbox until ``stop_event`` is set.

    Parameters
    ----------
    db_conn:
        Open aiosqlite connection. Caller owns its lifecycle.
    client:
        Open :class:`BackendClient`. Caller owns its lifecycle.
    interval:
        Seconds between ticks on the success path. On transient failure the
        next sleep is taken from ``_BackoffState`` instead, which grows
        until the next successful (or no-op) tick.
    batch_size:
        Maximum number of rows claimed per tick (across all shelves).
    stop_event:
        Cleared at startup, set on shutdown. The loop checks it before each
        sleep and after each wake so shutdown is prompt.

    Errors
    ------
    All non-cancellation errors are caught, logged at ERROR, and the loop
    continues on the next tick. ``asyncio.CancelledError`` propagates so
    ``TaskGroup`` shutdown semantics work.
    """
    backoff = _BackoffState()
    logger.info(
        "drainer started",
        extra={"interval": interval, "batch_size": batch_size},
    )

    while not stop_event.is_set():
        had_transient_failure = False

        try:
            had_transient_failure = await _drain_once(db_conn, client, batch_size)
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            logger.exception("drainer tick failed unexpectedly")

        if had_transient_failure:
            delay = backoff.next_delay()
            logger.warning(
                "drainer backing off after transient failure",
                extra={"delay_sec": delay},
            )
        else:
            backoff.reset()
            delay = interval

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    logger.info("drainer stopped")


async def _drain_once(
    db_conn: aiosqlite.Connection,
    client: BackendClient,
    batch_size: int,
) -> bool:
    """Run one drain tick. Returns True iff any group hit a transient failure.

    Transient failures elsewhere in the tick do not affect groups that
    already succeeded — those rows are deleted before the failing group's
    error propagates to the caller.
    """
    batch = await db.claim_batch(db_conn, batch_size=batch_size)
    if not batch:
        return False

    grouped: dict[str, list[db.Reading]] = defaultdict(list)
    for reading in batch:
        grouped[reading.shelf_id].append(reading)

    transient_seen = False
    for shelf_id, readings in grouped.items():
        try:
            await _post_group(db_conn, client, shelf_id, readings)
        except TransientBackendError as exc:
            transient_seen = True
            logger.warning(
                "telemetry POST transient failure; rows retained",
                extra={
                    "shelf_id": shelf_id,
                    "count": len(readings),
                    "status": exc.status,
                    "error": str(exc),
                },
            )

    return transient_seen


async def _post_group(
    db_conn: aiosqlite.Connection,
    client: BackendClient,
    shelf_id: str,
    readings: list[db.Reading],
) -> None:
    """POST one shelf's readings and apply the outcome to the DB.

    Per-batch ``sampled_at`` and ``metadata`` come from the most recent
    reading in the group (by ``sampled_at`` lexicographic order; valid for
    ISO 8601 UTC strings, which is what the contract requires).

    Raises
    ------
    TransientBackendError
        Re-raised after logging so the caller can mark this tick as a
        transient failure and apply backoff.
    """
    most_recent = max(readings, key=lambda r: r.sampled_at)
    envelope_metadata = _pick_metadata(readings)

    payload_readings = [
        ReadingPayload(
            reading_id=r.reading_id,
            scale_index=r.scale_index,
            est_grams=r.est_grams,
        )
        for r in readings
    ]

    try:
        response = await client.post_telemetry(
            shelf_id=shelf_id,
            sampled_at=most_recent.sampled_at,
            metadata=envelope_metadata,
            readings=payload_readings,
        )
    except PermanentBackendError as exc:
        logger.error(
            "telemetry POST permanent failure; bumping reject_count for whole batch",
            extra={
                "shelf_id": shelf_id,
                "count": len(readings),
                "status": exc.status,
                "body": exc.body,
            },
        )
        await db.bump_reject_count(db_conn, [r.reading_id for r in readings])
        return

    skipped_ids = {s.reading_id for s in response.skipped}
    if skipped_ids:
        for skipped in response.skipped:
            logger.warning(
                "telemetry reading skipped by backend",
                extra={
                    "shelf_id": shelf_id,
                    "reading_id": skipped.reading_id,
                    "reason": skipped.reason,
                },
            )

    all_ids = [r.reading_id for r in readings]
    deleted = await db.delete_readings(db_conn, all_ids)
    logger.info(
        "telemetry POST accepted",
        extra={
            "shelf_id": shelf_id,
            "accepted": response.accepted,
            "skipped": len(skipped_ids),
            "deleted": deleted,
        },
    )


def _pick_metadata(readings: list[db.Reading]) -> dict[str, object]:
    """Pick the most-recent non-null metadata from a shelf's batch.

    Falls back to an empty dict (``{}``) if no reading carries metadata.
    The backend's request schema requires ``metadata`` to be a JSON object;
    sending ``null`` produces a 400 (``Invalid input: expected object,
    received null``). When Process A doesn't supply battery/rssi we have no
    real data to send, but we still need a valid object on the wire.

    Ordering by ``sampled_at`` is lexicographic — valid for ISO 8601 UTC
    strings, which the contract requires.
    """
    candidates = [r for r in readings if r.metadata is not None]
    if not candidates:
        return {}
    chosen = max(candidates, key=lambda r: r.sampled_at)
    return chosen.metadata
