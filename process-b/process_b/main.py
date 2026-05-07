"""Process B entrypoint: wire config, DB, HTTP, and the three workers.

Lifecycle
---------
1. Load :class:`process_b.config.Config` from the environment.
2. Configure JSON logging.
3. Open the SQLite outbox connection and assert the schema.
4. Open the backend HTTP client.
5. Start three independent asyncio tasks under one ``TaskGroup``:

   - the UDP listener (binds first so datagrams aren't dropped),
   - the drainer,
   - the poller.

6. Install ``SIGTERM`` / ``SIGINT`` handlers (where the platform supports
   it) that set a shared ``stop_event``.
7. ``TaskGroup.__aexit__`` waits for all three workers to wind down. Then
   the HTTP client and DB connection are closed.

The shutdown order matters: workers stop accepting new work *before* the
DB and HTTP clients close out. ``TaskGroup`` cooperatively coordinates
this; we don't need explicit barriers.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import NoReturn

import aiosqlite

from process_b import db, drainer, logging_setup, poller, registrar, udp_listener
from process_b.backend_client import BackendClient
from process_b.config import Config, ConfigError

logger = logging.getLogger(__name__)


def run() -> NoReturn:
    """Console-script entrypoint declared in ``pyproject.toml``.

    Loads config, configures logging, then hands off to :func:`main`. Exits
    with code 2 on configuration errors so systemd's ``Restart=on-failure``
    can distinguish bad config (don't keep restarting) from runtime issues.
    """
    try:
        config = Config.from_env()
    except ConfigError as exc:
        # Use stderr directly: logging isn't configured yet.
        sys.stderr.write(f"{exc}\n")
        sys.exit(2)

    logging_setup.configure(config.log_level)

    try:
        asyncio.run(main(config))
    except KeyboardInterrupt:
        logger.info("interrupted by user")
        sys.exit(0)
    sys.exit(0)

async def main(config: Config) -> None:
    """Open resources, run the three worker tasks, clean up on exit."""
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    db_conn = await db.connect(config.db_path)
    try:
        await db.init(db_conn)

        async with BackendClient(
            base_url=config.backend_url,
            api_key=config.pi_api_key,
        ) as client:
            await _run_workers(config, db_conn, client, stop_event)
    finally:
        await db_conn.close()
        logger.info("process B exited cleanly")


async def _run_workers(
    config: Config,
    db_conn: aiosqlite.Connection,
    client: BackendClient,
    stop_event: asyncio.Event,
) -> None:
    """Run the listener, drainer, and poller concurrently until stop_event.

    Bind the UDP listener *first*, before the drainer or poller, so
    datagrams from Process A are buffered into the outbox even while the
    network side is still warming up.
    """
    listener_protocol = await udp_listener.start_listener(
        db_conn=db_conn,
        host=config.udp_listen_host,
        port=config.udp_listen_port,
    )

    async def run_listener() -> None:
        try:
            await stop_event.wait()
        finally:
            await listener_protocol.aclose()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(run_listener(), name="udp_listener")
        tg.create_task(
            drainer.run_drainer(
                db_conn=db_conn,
                client=client,
                interval=config.drain_interval_sec,
                batch_size=config.drain_batch_size,
                stop_event=stop_event,
            ),
            name="drainer",
        )
        tg.create_task(
            poller.run_poller(
                client=client,
                shelf_ids=config.shelf_ids,
                interval=config.poll_interval_sec,
                ipc_host=config.proc_a_control_host,
                ipc_port=config.proc_a_control_port,
                stop_event=stop_event,
            ),
            name="poller",
        )
        # Best-effort shelf registration. Only runs when device.json was
        # present (i.e. Process C provisioned this Pi); pre-provisioning
        # / dev runs use SHELF_IDS env var with no user_id and skip this.
        if config.user_id is not None:
            for shelf_id in config.shelf_ids:
                tg.create_task(
                    registrar.register_with_backoff(
                        client=client,
                        shelf_id=shelf_id,
                        user_id=config.user_id,
                        stop_event=stop_event,
                    ),
                    name=f"registrar:{shelf_id[:8]}",
                )


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGTERM/SIGINT to ``stop_event``. Best-effort on Windows.

    ``loop.add_signal_handler`` is Unix-only. On Windows the daemon is
    expected to be stopped via Ctrl+C, which raises ``KeyboardInterrupt``
    out of ``asyncio.run`` — handled in :func:`run`.
    """
    loop = asyncio.get_running_loop()

    def _handle(signame: str) -> None:
        logger.info("received signal; shutting down", extra={"signal": signame})
        stop_event.set()

    if sys.platform == "win32":
        return

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle, sig.name)
        except NotImplementedError:
            # Some non-Windows platforms (e.g. inside certain test runners)
            # also lack add_signal_handler. Fall through silently — Ctrl+C
            # still works via KeyboardInterrupt.
            pass

if __name__ == "__main__":
    run()
