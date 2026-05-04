"""Tests for :mod:`process_b.udp_listener`.

Two layers of testing:

1. **Protocol-level**: instantiate :class:`TelemetryProtocol` directly and
   call ``datagram_received`` with raw bytes. No socket. Asserts validation,
   error paths, and that valid input ends up in the DB.

2. **End-to-end**: ``start_listener`` against a real loopback socket, send
   a datagram via ``asyncio.DatagramTransport``, then wait for the row to
   land. One test is enough here — it covers the binding + protocol-wiring
   path that the protocol-level tests bypass.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator

import aiosqlite
import pytest
import pytest_asyncio

from process_b import db
from process_b.udp_listener import (
    IncomingReading,
    TelemetryProtocol,
    _parse_datagram,
    start_listener,
)


SHELF_A = "550e8400-e29b-41d4-a716-446655440000"
LOCAL_ADDR = ("127.0.0.1", 65000)


@pytest_asyncio.fixture
async def db_conn() -> AsyncIterator[aiosqlite.Connection]:
    conn = await aiosqlite.connect(":memory:")
    await db.init(conn)
    try:
        yield conn
    finally:
        await conn.close()


def _datagram(
    *,
    shelf_id: str = SHELF_A,
    scale_index: int = 0,
    est_grams: float | None = 750.2,
    sampled_at: str = "2026-04-21T08:30:00Z",
    extra: dict | None = None,
) -> bytes:
    payload = {
        "shelf_id": shelf_id,
        "scale_index": scale_index,
        "est_grams": est_grams,
        "sampled_at": sampled_at,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload).encode("utf-8")


async def _wait_for_rows(
    conn: aiosqlite.Connection, expected: int, timeout: float = 1.0
) -> list[db.Reading]:
    """Poll the outbox until ``expected`` rows are visible or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        rows = await db.claim_batch(conn, batch_size=1000)
        if len(rows) >= expected:
            return rows
        if asyncio.get_running_loop().time() >= deadline:
            return rows
        await asyncio.sleep(0.005)


async def _drain_protocol_tasks(protocol: TelemetryProtocol) -> None:
    """Wait for in-flight insert tasks to finish without closing the transport."""
    if protocol._tasks:  # type: ignore[reportPrivateUsage]
        await asyncio.gather(
            *protocol._tasks,  # type: ignore[reportPrivateUsage]
            return_exceptions=True,
        )


class TestParseDatagram:
    def test_valid_payload_parses(self) -> None:
        result = _parse_datagram(_datagram())
        assert isinstance(result, IncomingReading)
        assert result.shelf_id == SHELF_A
        assert result.scale_index == 0
        assert result.est_grams == pytest.approx(750.2)
        assert result.sampled_at == "2026-04-21T08:30:00Z"

    def test_invalid_utf8_is_rejected(self) -> None:
        with pytest.raises(Exception) as exc:
            _parse_datagram(b"\xff\xfe\xfd not utf-8")
        assert "utf-8" in str(exc.value)

    def test_invalid_json_is_rejected(self) -> None:
        with pytest.raises(Exception) as exc:
            _parse_datagram(b"{not valid json")
        assert "json" in str(exc.value).lower()

    def test_json_array_root_is_rejected(self) -> None:
        with pytest.raises(Exception) as exc:
            _parse_datagram(b"[1, 2, 3]")
        assert "object" in str(exc.value).lower()

    def test_missing_field_is_rejected(self) -> None:
        bad = json.dumps({"shelf_id": SHELF_A, "scale_index": 0}).encode("utf-8")
        with pytest.raises(Exception):
            _parse_datagram(bad)

    def test_unknown_field_is_ignored(self) -> None:
        # Schema is intentionally permissive (extra="ignore") so that new
        # diagnostic fields from Process A (battery, sensor_id, ...) don't
        # require a coordinated schema bump on every change.
        result = _parse_datagram(_datagram(extra={"surprise": "field"}))
        assert isinstance(result, IncomingReading)
        assert result.shelf_id == SHELF_A

    def test_adc_fault_field_is_parsed(self) -> None:
        result = _parse_datagram(
            _datagram(est_grams=None, extra={"adc_fault": True})  # type: ignore[arg-type]
        )
        assert result.adc_fault is True
        assert result.est_grams is None

    def test_adc_fault_defaults_false_when_omitted(self) -> None:
        result = _parse_datagram(_datagram())
        assert result.adc_fault is False

    def test_null_est_grams_is_accepted_when_fault(self) -> None:
        # est_grams: None is allowed at the parse layer; the persister is
        # responsible for dropping fault rows. Keeping parse permissive lets
        # us log/diagnose them rather than treating them as malformed.
        result = _parse_datagram(
            _datagram(est_grams=None, extra={"adc_fault": True})  # type: ignore[arg-type]
        )
        assert result.est_grams is None

    def test_negative_scale_index_is_rejected(self) -> None:
        with pytest.raises(Exception):
            _parse_datagram(_datagram(scale_index=-1))

    def test_nan_est_grams_is_rejected(self) -> None:
        # JSON spec doesn't allow NaN, but Python's json module produces
        # "NaN" which other parsers accept; pydantic validates after decode.
        bad = b'{"shelf_id": "%s", "scale_index": 0, "est_grams": NaN, "sampled_at": "2026-04-21T08:30:00Z"}' % SHELF_A.encode()
        with pytest.raises(Exception):
            _parse_datagram(bad)

    def test_infinity_est_grams_is_rejected(self) -> None:
        bad = b'{"shelf_id": "%s", "scale_index": 0, "est_grams": Infinity, "sampled_at": "2026-04-21T08:30:00Z"}' % SHELF_A.encode()
        with pytest.raises(Exception):
            _parse_datagram(bad)

    def test_empty_shelf_id_is_rejected(self) -> None:
        with pytest.raises(Exception):
            _parse_datagram(_datagram(shelf_id=""))

    def test_empty_sampled_at_is_rejected(self) -> None:
        with pytest.raises(Exception):
            _parse_datagram(_datagram(sampled_at=""))


class TestProtocolPersistence:
    async def test_valid_datagram_persists_to_db(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        protocol = TelemetryProtocol(db_conn)
        protocol.datagram_received(_datagram(), LOCAL_ADDR)

        rows = await _wait_for_rows(db_conn, expected=1)
        assert len(rows) == 1
        row = rows[0]
        assert row.shelf_id == SHELF_A
        assert row.scale_index == 0
        assert row.est_grams == pytest.approx(750.2)
        assert row.sampled_at == "2026-04-21T08:30:00Z"
        assert row.metadata is None
        assert row.reject_count == 0

    async def test_invalid_datagram_does_not_raise_or_persist(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        protocol = TelemetryProtocol(db_conn)

        protocol.datagram_received(b"not json at all", LOCAL_ADDR)
        protocol.datagram_received(b"[]", LOCAL_ADDR)
        protocol.datagram_received(b"\xff\xfe", LOCAL_ADDR)

        await _drain_protocol_tasks(protocol)

        rows = await db.claim_batch(db_conn, batch_size=1000)
        assert rows == []

    async def test_each_valid_datagram_yields_one_row(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        protocol = TelemetryProtocol(db_conn)

        for i in range(5):
            protocol.datagram_received(
                _datagram(scale_index=i, sampled_at=f"2026-04-21T08:30:0{i}Z"),
                LOCAL_ADDR,
            )

        rows = await _wait_for_rows(db_conn, expected=5)
        assert len(rows) == 5
        assert {r.scale_index for r in rows} == {0, 1, 2, 3, 4}

    async def test_mixed_valid_and_invalid_only_persists_valid(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        protocol = TelemetryProtocol(db_conn)

        protocol.datagram_received(_datagram(scale_index=0), LOCAL_ADDR)
        protocol.datagram_received(b"garbage", LOCAL_ADDR)
        protocol.datagram_received(_datagram(scale_index=1, sampled_at="2026-04-21T08:30:01Z"), LOCAL_ADDR)
        protocol.datagram_received(b"\xff\xfe not utf-8", LOCAL_ADDR)
        protocol.datagram_received(_datagram(scale_index=2, sampled_at="2026-04-21T08:30:02Z"), LOCAL_ADDR)

        rows = await _wait_for_rows(db_conn, expected=3)
        await _drain_protocol_tasks(protocol)
        rows = await db.claim_batch(db_conn, batch_size=1000)
        assert {r.scale_index for r in rows} == {0, 1, 2}

    async def test_adc_fault_datagram_is_skipped_not_persisted(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        # adc_fault=True means the sensor was unreadable this cycle. The
        # backend's telemetry schema requires numeric est_grams so we can't
        # forward it; drop at the listener instead of persisting an
        # un-drainable row.
        protocol = TelemetryProtocol(db_conn)

        protocol.datagram_received(
            _datagram(scale_index=0, est_grams=None, extra={"adc_fault": True}),  # type: ignore[arg-type]
            LOCAL_ADDR,
        )
        protocol.datagram_received(_datagram(scale_index=1), LOCAL_ADDR)
        protocol.datagram_received(
            _datagram(scale_index=2, est_grams=None, extra={"adc_fault": True}),  # type: ignore[arg-type]
            LOCAL_ADDR,
        )

        rows = await _wait_for_rows(db_conn, expected=1)
        await _drain_protocol_tasks(protocol)
        rows = await db.claim_batch(db_conn, batch_size=1000)
        assert len(rows) == 1
        assert rows[0].scale_index == 1

    async def test_aclose_drains_in_flight_tasks(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        protocol = TelemetryProtocol(db_conn)

        for i in range(3):
            protocol.datagram_received(
                _datagram(scale_index=i, sampled_at=f"2026-04-21T08:30:0{i}Z"),
                LOCAL_ADDR,
            )

        await protocol.aclose()

        rows = await db.claim_batch(db_conn, batch_size=1000)
        assert len(rows) == 3


class TestStartListenerEndToEnd:
    async def test_real_socket_receives_and_persists(
        self, db_conn: aiosqlite.Connection
    ) -> None:
        # Pick a free ephemeral port deterministically by binding/closing once.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        protocol = await start_listener(
            db_conn=db_conn, host="127.0.0.1", port=free_port
        )
        try:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(_datagram(), ("127.0.0.1", free_port))
            finally:
                sender.close()

            rows = await _wait_for_rows(db_conn, expected=1, timeout=2.0)
            assert len(rows) == 1
            assert rows[0].shelf_id == SHELF_A
        finally:
            await protocol.aclose()
