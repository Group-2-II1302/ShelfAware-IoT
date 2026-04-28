"""Async SQLite outbox for Process B.

This module is the single seam through which every other module reads and
writes ``pending_readings``. The schema is created and maintained by another
teammate's provisioning script on the Pi; we only *assert* the schema
(idempotent ``CREATE IF NOT EXISTS``) on startup so local development and
tests work without that setup step.

Concurrency model
-----------------
One daemon process, one writer task (the UDP listener), one reader/deleter
task (the drainer). No ``SELECT FOR UPDATE``, no row-level locking. "Claim"
is read-only — rows transition to "drained" only via ``DELETE`` on a
successful POST, which makes the workflow naturally idempotent on retry.

Boundary types
--------------
:class:`Reading` mirrors the on-disk row but exposes ``metadata`` as a parsed
``dict`` (or ``None``), not the JSON string that lives in the column. The
JSON encoding/decoding is a storage detail and lives entirely in this
module; every other layer sees structured values.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

import aiosqlite

QUARANTINE_THRESHOLD = 3
"""Rows with ``reject_count >= QUARANTINE_THRESHOLD`` are skipped by the drainer."""


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_readings (
  reading_id   TEXT PRIMARY KEY,
  shelf_id     TEXT NOT NULL,
  scale_index  INTEGER NOT NULL,
  est_grams    REAL NOT NULL,
  sampled_at   TEXT NOT NULL,
  metadata     TEXT,
  reject_count INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pending_readings_shelf_sampled
  ON pending_readings (shelf_id, sampled_at)
"""


@dataclass(frozen=True, slots=True)
class Reading:
    """One row of ``pending_readings``, as returned by :func:`claim_batch`."""

    reading_id: str
    shelf_id: str
    scale_index: int
    est_grams: float
    sampled_at: str
    metadata: dict[str, object] | None
    reject_count: int


async def connect(db_path: str) -> aiosqlite.Connection:
    """Open an aiosqlite connection with sensible PRAGMAs.

    Sets WAL journal mode (concurrent reads while a writer is active) and
    ``foreign_keys=ON``. Caller owns the connection's lifecycle and must
    ``await conn.close()`` on shutdown.
    """
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.commit()
    return conn


async def init(conn: aiosqlite.Connection) -> None:
    """Idempotently assert the ``pending_readings`` schema.

    Runs ``CREATE TABLE IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS``.
    Safe to call whether the schema was pre-created by the provisioning
    script or not. We trust the existing schema; we do not verify column
    names/types — schema drift will surface as an ``OperationalError`` on the
    first INSERT, which is loud enough.
    """
    await conn.execute(_CREATE_TABLE_SQL)
    await conn.execute(_CREATE_INDEX_SQL)
    await conn.commit()


async def insert_reading(
    conn: aiosqlite.Connection,
    *,
    shelf_id: str,
    scale_index: int,
    est_grams: float,
    sampled_at: str,
    metadata: dict[str, object] | None,
) -> str:
    """Insert one reading. Returns the freshly minted ``reading_id`` (UUIDv4).

    ``metadata``, if provided, is JSON-serialized into the ``metadata``
    column. Commits before returning so the UDP listener does not need to
    manage transactions; durability matters more here than throughput
    (datagrams arrive at human-scale rates).
    """
    reading_id = str(uuid.uuid4())
    metadata_json = json.dumps(metadata, separators=(",", ":")) if metadata is not None else None
    await conn.execute(
        """
        INSERT INTO pending_readings
            (reading_id, shelf_id, scale_index, est_grams, sampled_at, metadata)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (reading_id, shelf_id, scale_index, est_grams, sampled_at, metadata_json),
    )
    await conn.commit()
    return reading_id


async def claim_batch(
    conn: aiosqlite.Connection,
    *,
    batch_size: int,
) -> list[Reading]:
    """Return up to ``batch_size`` non-quarantined rows, oldest first per shelf.

    "Claim" is a misnomer kept for readability: there is no row-level lock
    and no in-flight flag. Rows leave ``pending_readings`` only via
    :func:`delete_readings`; until then a subsequent ``claim_batch`` will
    return them again, which is the correct behavior on retry.

    Filters
    -------
    ``WHERE reject_count < QUARANTINE_THRESHOLD`` — quarantined rows skipped.

    Order
    -----
    ``ORDER BY shelf_id, sampled_at`` — keeps per-shelf time order intact
    and lets the drainer scan a shelf's rows contiguously when grouping.
    """
    if batch_size <= 0:
        return []

    cursor = await conn.execute(
        """
        SELECT reading_id, shelf_id, scale_index, est_grams,
               sampled_at, metadata, reject_count
          FROM pending_readings
         WHERE reject_count < ?
         ORDER BY shelf_id, sampled_at
         LIMIT ?
        """,
        (QUARANTINE_THRESHOLD, batch_size),
    )
    rows = await cursor.fetchall()
    await cursor.close()

    return [
        Reading(
            reading_id=row[0],
            shelf_id=row[1],
            scale_index=row[2],
            est_grams=row[3],
            sampled_at=row[4],
            metadata=json.loads(row[5]) if row[5] is not None else None,
            reject_count=row[6],
        )
        for row in rows
    ]


async def delete_readings(
    conn: aiosqlite.Connection,
    reading_ids: Iterable[str],
) -> int:
    """Delete the named rows. Returns the number of rows actually deleted.

    Empty input is a no-op returning 0. Commits before returning. The drainer
    calls this on 2xx for both accepted and ``skipped[]`` reading_ids, since
    both are terminally handled by the backend.
    """
    ids = list(reading_ids)
    if not ids:
        return 0

    placeholders = ",".join("?" for _ in ids)
    cursor = await conn.execute(
        f"DELETE FROM pending_readings WHERE reading_id IN ({placeholders})",
        ids,
    )
    affected = cursor.rowcount
    await cursor.close()
    await conn.commit()
    return affected if affected is not None else 0


async def bump_reject_count(
    conn: aiosqlite.Connection,
    reading_ids: Iterable[str],
) -> int:
    """Increment ``reject_count`` for the named rows. Returns rows affected.

    Reserved for the rare whole-batch 4xx case. Never called on transient
    failures (5xx, network, timeout) — those would falsely poison rows
    during long offline periods. Rows that cross ``reject_count >= 3`` are
    quarantined: :func:`claim_batch` will skip them on subsequent calls.
    """
    ids = list(reading_ids)
    if not ids:
        return 0

    placeholders = ",".join("?" for _ in ids)
    cursor = await conn.execute(
        f"""
        UPDATE pending_readings
           SET reject_count = reject_count + 1
         WHERE reading_id IN ({placeholders})
        """,
        ids,
    )
    affected = cursor.rowcount
    await cursor.close()
    await conn.commit()
    return affected if affected is not None else 0
