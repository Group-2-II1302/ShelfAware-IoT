"""Tests for :mod:`process_b.db`.

These tests run against a real ``aiosqlite`` connection backed by ``:memory:``
— it's fast, hermetic, and exercises the actual SQL we ship. Mocking sqlite
itself would just test our mocks.

Each test gets its own connection via the ``db_conn`` fixture; nothing is
shared, so order is irrelevant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
import pytest
import pytest_asyncio

from process_b import db


SHELF_A = "550e8400-e29b-41d4-a716-446655440000"
SHELF_B = "660e8400-e29b-41d4-a716-446655440001"


@pytest_asyncio.fixture
async def db_conn() -> AsyncIterator[aiosqlite.Connection]:
    """Yield an initialized in-memory connection. Closed at teardown."""
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
    """Test helper: insert and return reading_id."""
    return await db.insert_reading(
        conn,
        shelf_id=shelf_id,
        scale_index=scale_index,
        est_grams=est_grams,
        sampled_at=sampled_at,
        metadata=metadata,
    )


class TestInit:
    async def test_creates_table_and_index(self, db_conn: aiosqlite.Connection) -> None:
        cursor = await db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_readings'"
        )
        assert await cursor.fetchone() is not None
        await cursor.close()

        cursor = await db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_pending_readings_shelf_sampled'"
        )
        assert await cursor.fetchone() is not None
        await cursor.close()

    async def test_init_is_idempotent(self, db_conn: aiosqlite.Connection) -> None:
        await db.init(db_conn)
        await db.init(db_conn)

        reading_id = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        assert reading_id


class TestInsertReading:
    async def test_returns_uuid_v4_string(self, db_conn: aiosqlite.Connection) -> None:
        reading_id = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")

        import uuid as _uuid

        parsed = _uuid.UUID(reading_id)
        assert parsed.version == 4

    async def test_persists_all_fields(self, db_conn: aiosqlite.Connection) -> None:
        reading_id = await _insert(
            db_conn,
            SHELF_A,
            scale_index=2,
            sampled_at="2026-04-21T08:30:00Z",
            est_grams=750.2,
            metadata={"battery": 88, "rssi": -65},
        )

        cursor = await db_conn.execute(
            "SELECT shelf_id, scale_index, est_grams, sampled_at, metadata, reject_count "
            "FROM pending_readings WHERE reading_id = ?",
            (reading_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        assert row is not None
        assert row[0] == SHELF_A
        assert row[1] == 2
        assert row[2] == pytest.approx(750.2)
        assert row[3] == "2026-04-21T08:30:00Z"
        assert row[4] is not None and "battery" in row[4]  # JSON-encoded
        assert row[5] == 0  # reject_count default

    async def test_metadata_none_is_stored_as_null(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        reading_id = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z", metadata=None)

        cursor = await db_conn.execute(
            "SELECT metadata FROM pending_readings WHERE reading_id = ?", (reading_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()

        assert row is not None
        assert row[0] is None

    async def test_each_call_yields_unique_id(self, db_conn: aiosqlite.Connection) -> None:
        ids = {await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z") for _ in range(5)}
        assert len(ids) == 5


class TestClaimBatch:
    async def test_returns_empty_when_outbox_empty(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        assert await db.claim_batch(db_conn, batch_size=10) == []

    async def test_zero_or_negative_batch_size_returns_empty(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        assert await db.claim_batch(db_conn, batch_size=0) == []
        assert await db.claim_batch(db_conn, batch_size=-1) == []

    async def test_respects_batch_size(self, db_conn: aiosqlite.Connection) -> None:
        for i in range(5):
            await _insert(db_conn, SHELF_A, i, f"2026-04-21T08:30:0{i}Z")

        batch = await db.claim_batch(db_conn, batch_size=3)
        assert len(batch) == 3

    async def test_orders_by_shelf_id_then_sampled_at(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        await _insert(db_conn, SHELF_B, 0, "2026-04-21T08:30:00Z")
        await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:02Z")
        await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:01Z")
        await _insert(db_conn, SHELF_B, 0, "2026-04-21T08:29:00Z")

        batch = await db.claim_batch(db_conn, batch_size=10)
        ordering = [(r.shelf_id, r.sampled_at) for r in batch]
        assert ordering == [
            (SHELF_A, "2026-04-21T08:30:01Z"),
            (SHELF_A, "2026-04-21T08:30:02Z"),
            (SHELF_B, "2026-04-21T08:29:00Z"),
            (SHELF_B, "2026-04-21T08:30:00Z"),
        ]

    async def test_skips_quarantined_rows(self, db_conn: aiosqlite.Connection) -> None:
        rid_active = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        rid_quarantined = await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z")

        await db_conn.execute(
            "UPDATE pending_readings SET reject_count = ? WHERE reading_id = ?",
            (db.QUARANTINE_THRESHOLD, rid_quarantined),
        )
        await db_conn.commit()

        batch = await db.claim_batch(db_conn, batch_size=10)
        assert [r.reading_id for r in batch] == [rid_active]

    async def test_metadata_round_trip(self, db_conn: aiosqlite.Connection) -> None:
        await _insert(
            db_conn,
            SHELF_A,
            0,
            "2026-04-21T08:30:00Z",
            metadata={"battery": 88, "rssi": -65},
        )
        await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z", metadata=None)

        batch = await db.claim_batch(db_conn, batch_size=10)
        assert batch[0].metadata == {"battery": 88, "rssi": -65}
        assert batch[1].metadata is None

    async def test_returned_reading_has_all_fields(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        rid = await _insert(
            db_conn,
            SHELF_A,
            scale_index=3,
            sampled_at="2026-04-21T08:30:00Z",
            est_grams=420.5,
            metadata={"battery": 50},
        )

        (reading,) = await db.claim_batch(db_conn, batch_size=10)
        assert reading.reading_id == rid
        assert reading.shelf_id == SHELF_A
        assert reading.scale_index == 3
        assert reading.est_grams == pytest.approx(420.5)
        assert reading.sampled_at == "2026-04-21T08:30:00Z"
        assert reading.metadata == {"battery": 50}
        assert reading.reject_count == 0


class TestDeleteReadings:
    async def test_empty_input_is_noop(self, db_conn: aiosqlite.Connection) -> None:
        assert await db.delete_readings(db_conn, []) == 0

    async def test_deletes_only_named_rows(self, db_conn: aiosqlite.Connection) -> None:
        rid_a = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        rid_b = await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z")
        rid_c = await _insert(db_conn, SHELF_A, 2, "2026-04-21T08:30:02Z")

        deleted = await db.delete_readings(db_conn, [rid_a, rid_c])
        assert deleted == 2

        remaining = await db.claim_batch(db_conn, batch_size=10)
        assert [r.reading_id for r in remaining] == [rid_b]

    async def test_unknown_ids_silently_skip(self, db_conn: aiosqlite.Connection) -> None:
        rid = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")

        deleted = await db.delete_readings(db_conn, [rid, "nope-not-a-real-id"])
        assert deleted == 1

    async def test_accepts_generator(self, db_conn: aiosqlite.Connection) -> None:
        rid_a = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        rid_b = await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z")

        deleted = await db.delete_readings(db_conn, (r for r in [rid_a, rid_b]))
        assert deleted == 2


class TestBumpRejectCount:
    async def test_empty_input_is_noop(self, db_conn: aiosqlite.Connection) -> None:
        assert await db.bump_reject_count(db_conn, []) == 0

    async def test_bumps_only_named_rows(self, db_conn: aiosqlite.Connection) -> None:
        rid_a = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")
        rid_b = await _insert(db_conn, SHELF_A, 1, "2026-04-21T08:30:01Z")

        affected = await db.bump_reject_count(db_conn, [rid_a])
        assert affected == 1

        batch = await db.claim_batch(db_conn, batch_size=10)
        by_id = {r.reading_id: r.reject_count for r in batch}
        assert by_id[rid_a] == 1
        assert by_id[rid_b] == 0

    async def test_three_bumps_quarantine_a_row(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        rid = await _insert(db_conn, SHELF_A, 0, "2026-04-21T08:30:00Z")

        for _ in range(db.QUARANTINE_THRESHOLD):
            await db.bump_reject_count(db_conn, [rid])

        assert await db.claim_batch(db_conn, batch_size=10) == []

    async def test_unknown_ids_affect_zero_rows(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        affected = await db.bump_reject_count(db_conn, ["does-not-exist"])
        assert affected == 0


class TestConnect:
    async def test_connect_then_init_then_use(self, tmp_path) -> None:  # noqa: ANN001
        db_path = str(tmp_path / "outbox.db")
        conn = await db.connect(db_path)
        try:
            await db.init(conn)
            rid = await db.insert_reading(
                conn,
                shelf_id=SHELF_A,
                scale_index=0,
                est_grams=42.0,
                sampled_at="2026-04-21T08:30:00Z",
                metadata=None,
            )
            (reading,) = await db.claim_batch(conn, batch_size=10)
            assert reading.reading_id == rid
        finally:
            await conn.close()

    async def test_connect_sets_wal_journal_mode(self, tmp_path) -> None:  # noqa: ANN001
        db_path = str(tmp_path / "outbox.db")
        conn = await db.connect(db_path)
        try:
            cursor = await conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None
            assert row[0].lower() == "wal"
        finally:
            await conn.close()
