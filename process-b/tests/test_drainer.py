"""Tests for :mod:`process_b.drainer`.

Strategy
--------
- Real aiosqlite ``:memory:`` DB (we already trust :mod:`db`; mocking it
  would mean writing a fake that's strictly worse than the real thing).
- Mocked :class:`BackendClient` via a small fake. We're testing the
  drainer's *decisions*, not httpx; mocking the client surface keeps each
  test focused on one branch of the state machine.
- The main loop is exercised end-to-end against the fake client + a
  ``stop_event`` we set after a controlled number of ticks. We don't test
  real wall-clock timing — backoff math is unit-tested separately.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import aiosqlite
import pytest
import pytest_asyncio

from process_b import db
from process_b.backend_client import (
    PermanentBackendError,
    SkippedReading,
    TelemetryResponse,
    TransientBackendError,
)
from process_b.drainer import _BackoffState, _drain_once, _pick_metadata, run_drainer


SHELF_A = "550e8400-e29b-41d4-a716-446655440000"
SHELF_B = "660e8400-e29b-41d4-a716-446655440001"


@dataclass
class _RecordedPost:
    shelf_id: str
    sampled_at: str
    metadata: dict[str, object] | None
    reading_ids: list[str]


@dataclass
class FakeBackendClient:
    """Stand-in for :class:`BackendClient` with programmable responses.

    Each call to :meth:`post_telemetry` consumes one entry from
    ``responses``. Entries are either:

    - a :class:`TelemetryResponse` — returned to the caller.
    - a :class:`BackendError` subclass — raised.

    Empty queue defaults to ``TelemetryResponse(accepted=N, skipped=[])``,
    where N is the size of the request being served. Lets simple tests
    omit setup entirely.
    """

    responses: list[object] = field(default_factory=list)
    posts: list[_RecordedPost] = field(default_factory=list)
    get_commands_responses: list[object] = field(default_factory=list)

    async def post_telemetry(
        self,
        *,
        shelf_id: str,
        sampled_at: str,
        metadata: dict[str, object] | None,
        readings: list,  # noqa: ANN001 — pydantic models, type checked at use site
    ) -> TelemetryResponse:
        self.posts.append(
            _RecordedPost(
                shelf_id=shelf_id,
                sampled_at=sampled_at,
                metadata=metadata,
                reading_ids=[r.reading_id for r in readings],
            )
        )
        if not self.responses:
            return TelemetryResponse(accepted=len(readings), skipped=[])
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, TelemetryResponse)
        return outcome


@pytest_asyncio.fixture
async def db_conn() -> AsyncIterator[aiosqlite.Connection]:
    conn = await aiosqlite.connect(":memory:")
    await db.init(conn)
    try:
        yield conn
    finally:
        await conn.close()


async def _insert(
    conn: aiosqlite.Connection,
    shelf_id: str,
    scale_index: int,
    sampled_at: str,
    *,
    est_grams: float = 100.0,
    metadata: dict[str, object] | None = None,
) -> str:
    return await db.insert_reading(
        conn,
        shelf_id=shelf_id,
        scale_index=scale_index,
        est_grams=est_grams,
        sampled_at=sampled_at,
        metadata=metadata,
    )


async def _outbox(conn: aiosqlite.Connection) -> list[db.Reading]:
    return await db.claim_batch(conn, batch_size=1000)


class TestPickMetadata:
    def test_returns_none_when_all_metadata_are_none(self) -> None:
        readings = [
            db.Reading(
                reading_id=f"r{i}",
                shelf_id=SHELF_A,
                scale_index=i,
                est_grams=10.0,
                sampled_at=f"2026-04-21T08:30:0{i}Z",
                metadata=None,
                reject_count=0,
            )
            for i in range(3)
        ]
        assert _pick_metadata(readings) is None

    def test_picks_most_recent_non_null_metadata(self) -> None:
        readings = [
            db.Reading(
                reading_id="r0",
                shelf_id=SHELF_A,
                scale_index=0,
                est_grams=10.0,
                sampled_at="2026-04-21T08:30:00Z",
                metadata={"battery": 90},
                reject_count=0,
            ),
            db.Reading(
                reading_id="r1",
                shelf_id=SHELF_A,
                scale_index=1,
                est_grams=10.0,
                sampled_at="2026-04-21T08:30:05Z",
                metadata=None,  # newer in time but null — must not be chosen
                reject_count=0,
            ),
            db.Reading(
                reading_id="r2",
                shelf_id=SHELF_A,
                scale_index=2,
                est_grams=10.0,
                sampled_at="2026-04-21T08:30:02Z",
                metadata={"battery": 88},
                reject_count=0,
            ),
        ]
        assert _pick_metadata(readings) == {"battery": 88}


class TestBackoffState:
    def test_starts_at_initial_then_doubles(self) -> None:
        bo = _BackoffState(initial=1.0, cap=30.0, factor=2.0)
        assert bo.next_delay() == 1.0
        assert bo.next_delay() == 2.0
        assert bo.next_delay() == 4.0

    def test_caps_at_max(self) -> None:
        bo = _BackoffState(initial=1.0, cap=30.0, factor=2.0)
        for _ in range(20):
            bo.next_delay()
        assert bo.next_delay() == 30.0

    def test_reset_returns_to_initial(self) -> None:
        bo = _BackoffState(initial=1.0, cap=30.0, factor=2.0)
        bo.next_delay()
        bo.next_delay()
        bo.reset()
        assert bo.next_delay() == 1.0


class TestDrainOnce:
    async def test_empty_outbox_is_noop(self, db_conn: aiosqlite.Connection) -> None:
        client = FakeBackendClient()
        had_transient = await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]
        assert had_transient is False
        assert client.posts == []

    async def test_2xx_deletes_all_rows_in_batch(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        for i in range(3):
            await _insert(db_conn, SHELF_A, i, f"2026-04-21T08:30:0{i}Z")

        client = FakeBackendClient()
        await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert len(client.posts) == 1
        assert await _outbox(db_conn) == []

    async def test_2xx_with_skipped_still_deletes_skipped_rows(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        rids = [
            await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z"),
            await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z"),
        ]

        client = FakeBackendClient(
            responses=[
                TelemetryResponse(
                    accepted=1,
                    skipped=[SkippedReading(reading_id=rids[1], reason="unknown_scale")],
                )
            ]
        )
        await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert await _outbox(db_conn) == []

    async def test_4xx_bumps_reject_count_for_whole_batch_no_delete(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        rids = [
            await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z"),
            await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z"),
        ]

        client = FakeBackendClient(
            responses=[PermanentBackendError("boom", status=400, body="bad payload")]
        )
        had_transient = await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert had_transient is False  # 4xx is not transient

        rows = await _outbox(db_conn)
        assert {r.reading_id for r in rows} == set(rids)
        assert all(r.reject_count == 1 for r in rows)

    async def test_5xx_does_not_touch_rows_and_signals_transient(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        rids = [
            await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z"),
            await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z"),
        ]

        client = FakeBackendClient(
            responses=[TransientBackendError("upstream down", status=503)]
        )
        had_transient = await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert had_transient is True

        rows = await _outbox(db_conn)
        assert {r.reading_id for r in rows} == set(rids)
        assert all(r.reject_count == 0 for r in rows)

    async def test_groups_by_shelf_one_request_per_shelf(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z")
        await _insert(db_conn, SHELF_B, 0, "2026-04-21T08:29:00Z")

        client = FakeBackendClient()
        await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert len(client.posts) == 2
        by_shelf = {p.shelf_id: p for p in client.posts}
        assert sorted(by_shelf.keys()) == sorted([SHELF_A, SHELF_B])
        assert len(by_shelf[SHELF_A].reading_ids) == 2
        assert len(by_shelf[SHELF_B].reading_ids) == 1

    async def test_envelope_uses_most_recent_sampled_at_per_group(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:05Z")
        await _insert(db_conn, SHELF_A, 2, "2026-04-21T08:30:02Z")

        client = FakeBackendClient()
        await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert client.posts[0].sampled_at == "2026-04-21T08:30:05Z"

    async def test_envelope_metadata_is_most_recent_non_null(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        await _insert(
            db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z", metadata={"battery": 90}
        )
        await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:05Z", metadata=None)
        await _insert(
            db_conn, SHELF_A, 2, "2026-04-21T08:30:03Z", metadata={"battery": 88}
        )

        client = FakeBackendClient()
        await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert client.posts[0].metadata == {"battery": 88}

    async def test_one_shelf_5xx_does_not_block_other_shelf_2xx(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        rid_a = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        rid_b = await _insert(db_conn, SHELF_B, 0, "2026-04-21T08:30:00Z")

        client = FakeBackendClient(
            responses=[
                TransientBackendError("down", status=503),
                TelemetryResponse(accepted=1, skipped=[]),
            ]
        )
        had_transient = await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert had_transient is True

        remaining = await _outbox(db_conn)
        remaining_ids = {r.reading_id for r in remaining}
        # Whichever shelf got the 5xx stays; the other was deleted. We can't
        # rely on dict ordering for which shelf hits 5xx first, so just
        # assert exactly one row remains and it's one of the originals.
        assert len(remaining) == 1
        assert remaining_ids.issubset({rid_a, rid_b})

    async def test_quarantined_rows_are_skipped(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        rid_q = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        rid_ok = await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z")

        await db_conn.execute(
            "UPDATE pending_readings SET reject_count = ? WHERE reading_id = ?",
            (db.QUARANTINE_THRESHOLD, rid_q),
        )
        await db_conn.commit()

        client = FakeBackendClient()
        await _drain_once(db_conn, client, batch_size=50)  # type: ignore[arg-type]

        assert len(client.posts) == 1
        assert client.posts[0].reading_ids == [rid_ok]

        # The quarantined row is still there; the OK row was deleted.
        remaining = await db_conn.execute_fetchall(
            "SELECT reading_id FROM pending_readings"
        )
        assert {r[0] for r in remaining} == {rid_q}


class TestRunDrainerLoop:
    async def test_stop_event_terminates_loop(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        client = FakeBackendClient()
        stop = asyncio.Event()
        stop.set()  # already set — loop should exit on first check

        await asyncio.wait_for(
            run_drainer(
                db_conn=db_conn,
                client=client,  # type: ignore[arg-type]
                interval=0.01,
                batch_size=50,
                stop_event=stop,
            ),
            timeout=1.0,
        )
        assert client.posts == []

    async def test_loop_drains_then_exits_on_stop(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        rid = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")

        client = FakeBackendClient()
        stop = asyncio.Event()

        async def stopper() -> None:
            # Give the drainer one tick to do its work, then signal stop.
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(
            run_drainer(
                db_conn=db_conn,
                client=client,  # type: ignore[arg-type]
                interval=0.01,
                batch_size=50,
                stop_event=stop,
            ),
            stopper(),
        )

        assert any(p.reading_ids == [rid] for p in client.posts)
        assert await _outbox(db_conn) == []

    async def test_loop_survives_unexpected_db_failure(
        self, db_conn: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If something blows up unexpectedly, the loop logs and keeps going.

        We patch ``db.claim_batch`` to fail twice then succeed, run a few
        ticks, and assert the loop didn't crash and eventually drained.
        """
        await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")

        original = db.claim_batch
        call_count = {"n": 0}

        async def flaky_claim(conn, *, batch_size):  # noqa: ANN001
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise RuntimeError("simulated DB hiccup")
            return await original(conn, batch_size=batch_size)

        monkeypatch.setattr("process_b.drainer.db.claim_batch", flaky_claim)

        client = FakeBackendClient()
        stop = asyncio.Event()

        async def stopper() -> None:
            await asyncio.sleep(0.2)
            stop.set()

        await asyncio.gather(
            run_drainer(
                db_conn=db_conn,
                client=client,  # type: ignore[arg-type]
                interval=0.01,
                batch_size=50,
                stop_event=stop,
            ),
            stopper(),
        )

        assert call_count["n"] >= 3
        assert len(client.posts) >= 1
