# ShelfAware — Process B

Process B is the **broker / persister daemon** that runs on a Raspberry Pi as part of the ShelfAware smart-shelf inventory system. It bridges hardware (load-cell readings from Process A) and the network (the Cloudflare Worker backend).

## Where this fits

ShelfAware has three pieces on the Pi and one piece in the cloud:

| Component   | Owner       | Role                                                                  |
| ----------- | ----------- | --------------------------------------------------------------------- |
| Process A   | Teammate    | Reads ADCs, emits weight readings as UDP datagrams. Owns sleep/wake.  |
| **Process B** | **This repo** | UDP listener → SQLite outbox → HTTP client + command poller.          |
| Backend     | Deployed    | Cloudflare Worker (Hono + TS). `POST /telemetry`, `GET /commands`.    |
| Frontend    | Teammate    | SvelteKit app reading shelf state from Supabase directly.             |

Process B never computes shelf state — that's the backend's job. Process B's only contracts are: receive UDP, persist, drain, poll for wakes.

## Responsibilities

1. **Listen** for UDP datagrams from Process A on a fixed port. Each datagram is one weight reading.
2. **Persist** each reading to SQLite (`pending_readings`) immediately. SQLite is a transient outbox — readings buffer there only until the backend accepts them.
3. **Drain** the outbox every ~5s: claim a batch (≤50 readings, grouped by `shelf_id`), POST to `/telemetry`, `DELETE` on 2xx, bump `reject_count` on rare whole-batch 4xx, retry with exponential backoff (cap 30s) on 5xx / network errors.
4. **Poll** `GET /commands` every ~3s. Forward any `wake` command to Process A's UDP control port as `{"type": "wake"}`. Drop expired commands.

These are three independent `asyncio` tasks. The UDP listener binds and starts receiving immediately on startup; the drainer and poller run independently.

## Architecture

```
Process A ──UDP──► udp_listener.py ──► db.insert_reading ──► SQLite (pending_readings)
                                                                  │
                                                                  ▼
                                                          drainer.py ──HTTP──► POST /telemetry
                                                                                    │
                                                                          (2xx) DELETE rows
                                                                          (skipped[]) DELETE + WARN log
                                                                          (4xx whole batch) bump reject_count
                                                                          (5xx / network) backoff, retry

                                              poller.py ──HTTP──► GET /commands
                                                  │
                                                  └─wake──► ipc.py ──UDP──► Process A control port
```

## Dependencies

- Python **3.11+**
- [`httpx`](https://www.python-httpx.org/) (async) — backend HTTP client
- [`aiosqlite`](https://github.com/omnilib/aiosqlite) — async SQLite
- [`pydantic`](https://docs.pydantic.dev/) v2 — datagram + payload validation

Dev: `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`.

## Configuration

All config via environment variables. No config files. The daemon is supervised by `systemd` in production.

| Variable                | Required | Default      | Description                                          |
| ----------------------- | -------- | ------------ | ---------------------------------------------------- |
| `PI_API_KEY`            | yes      | —            | Bearer token for the backend.                        |
| `BACKEND_URL`           | yes      | —            | Base URL of the Cloudflare Worker.                   |
| `DB_PATH`               | yes      | —            | Path to the SQLite file on the Pi.                   |
| `UDP_LISTEN_HOST`       | no       | `0.0.0.0`    | Bind address for the listener.                       |
| `UDP_LISTEN_PORT`       | yes      | —            | Port Process A sends readings to.                    |
| `PROC_A_CONTROL_HOST`   | no       | `127.0.0.1`  | Where Process A listens for control datagrams.       |
| `PROC_A_CONTROL_PORT`   | yes      | —            | Process A's control port.                            |
| `DRAIN_INTERVAL_SEC`    | no       | `5`          | Seconds between drain ticks.                         |
| `DRAIN_BATCH_SIZE`      | no       | `50`         | Max readings per `/telemetry` request.               |
| `POLL_INTERVAL_SEC`     | no       | `3`          | Seconds between `/commands` polls.                   |
| `LOG_LEVEL`             | no       | `INFO`       | Stdlib logging level.                                |

## SQLite schema

`db.py` runs this idempotently on startup (`CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`):

```sql
CREATE TABLE pending_readings (
  reading_id   TEXT PRIMARY KEY,           -- UUIDv4, generated by Process B at insert time
  shelf_id     TEXT NOT NULL,              -- UUIDv4
  scale_index  INTEGER NOT NULL,
  est_grams    REAL NOT NULL,
  sampled_at   TEXT NOT NULL,              -- ISO 8601, from Process A
  metadata     TEXT,                       -- optional JSON: {"battery": 88, "rssi": -65}
  reject_count INTEGER NOT NULL DEFAULT 0  -- bumped on rare whole-batch 4xx; quarantine after 3
);

CREATE INDEX idx_pending_readings_shelf_sampled
  ON pending_readings (shelf_id, sampled_at);
```

Design notes:

- The table is a **transient outbox**, not an audit log. Drained rows are `DELETE`d.
- `reject_count` is reserved for whole-batch 4xx (malformed payload, auth failure, etc.). Per-row rejections come back as `200 + skipped[]` and result in a plain `DELETE` plus a `WARN` log — they do **not** bump `reject_count`.
- 5xx and network errors never bump `reject_count` — they would falsely poison rows during long offline periods.
- Rows with `reject_count >= 3` are **quarantined**: the drainer skips them (`WHERE reject_count < 3`). They are left in the table for operator inspection.

## UDP datagram format (Process A → Process B)

JSON UTF-8, one reading per datagram:

```json
{
  "shelf_id": "550e8400-e29b-41d4-a716-446655440000",
  "scale_index": 0,
  "est_grams": 750.2,
  "sampled_at": "2026-04-21T08:30:00Z"
}
```

`metadata` (battery, rssi) is not in the current datagram; the column is wired through end-to-end so Process A can start emitting it without changes here beyond the listener.

## Wake datagram (Process B → Process A)

JSON UTF-8 to `PROC_A_CONTROL_HOST:PROC_A_CONTROL_PORT`:

```json
{ "type": "wake" }
```

Tentative — finalize with Process A's owner. `id` and `expires_at` are not propagated; wake is fire-and-forget over IPC.

## Development

All commands below assume the current directory is this one (`process-b/`).

```bash
python -m venv .venv
.venv\Scripts\activate                # Windows
# source .venv/bin/activate           # macOS/Linux
pip install -e ".[dev]"

pytest
ruff check .
ruff format --check .
mypy
```

Local development uses mocked I/O (no real Pi, no real Process A). Real-Pi integration happens after the modules are unit-tested.

## Running

For local/manual runs:

```bash
process-b
```

Graceful shutdown on `SIGTERM`: stop accepting UDP, drain in-flight HTTP, close the DB.

For Raspberry Pi production deployment as a managed `systemd` service, see [`deploy/README.md`](deploy/README.md).

## Project layout

```
process_b/
  main.py              # entrypoint: load config, start tasks, graceful shutdown
  config.py            # env-var loader
  db.py                # aiosqlite wrapper (insert, claim, delete, bump)
  udp_listener.py      # asyncio.DatagramProtocol → db.insert_reading
  drainer.py           # claim → POST /telemetry → delete or bump
  poller.py            # GET /commands → ipc.send_wake
  backend_client.py    # httpx.AsyncClient wrapper
  ipc.py               # UDP send to Process A
  logging_setup.py     # JSON formatter for stdlib logging
tests/
  test_db.py
  test_drainer.py
  test_poller.py
  test_udp_listener.py
```
