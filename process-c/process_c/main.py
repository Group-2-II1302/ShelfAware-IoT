"""Process C entrypoint.

Lifecycle::

    1. Load Config from env.
    2. Configure JSON logging.
    3. Build the WifiBackend (FakeWifiBackend in dev mode, RealWifiBackend
       in production).
    4. Build the aiohttp Application with the three (or two) endpoints.
    5. Start the HTTP server.
    6. On SIGTERM/SIGINT: stop accepting new requests, let in-flight
       requests drain, then exit.

Configuration errors exit with code 2 so systemd's ``Restart=on-failure``
distinguishes them from runtime issues (don't hot-loop on bad config).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import NoReturn

from aiohttp import web

from process_c import logging_setup
from process_c.config import Config, ConfigError
from process_c.handlers import (
    cors_middleware,
    make_health_handler,
    make_provision_handler,
    make_reset_handler,
)
from process_c.wifi_backend import FakeWifiBackend, RealWifiBackend, WifiBackend

logger = logging.getLogger(__name__)


def run() -> NoReturn:
    """Console-script entrypoint declared in ``pyproject.toml``."""
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(2)

    logging_setup.configure(cfg.log_level)

    try:
        asyncio.run(_serve(cfg))
    except KeyboardInterrupt:
        logger.info("interrupted by user")
        sys.exit(0)
    sys.exit(0)


async def _serve(cfg: Config) -> None:
    wifi = _build_wifi_backend(cfg)
    app = build_app(cfg, wifi)

    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()
    site = web.TCPSite(runner, cfg.bind_host, cfg.bind_port)
    await site.start()

    logger.info(
        "process-c listening",
        extra={
            "bind_host": cfg.bind_host,
            "bind_port": cfg.bind_port,
            "device_file_path": cfg.device_file_path,
            "dev_mode": cfg.dev_mode,
            "reset_endpoint": cfg.allow_reset_endpoint,
        },
    )

    stop_event = _install_stop_event()
    await stop_event.wait()

    logger.info("process-c shutting down")
    await runner.cleanup()


def build_app(cfg: Config, wifi: WifiBackend) -> web.Application:
    """Construct the aiohttp Application. Exposed for tests."""
    app = web.Application(middlewares=[cors_middleware])

    app.router.add_get("/health", make_health_handler(cfg))
    app.router.add_post("/provision", make_provision_handler(cfg, wifi))

    if cfg.allow_reset_endpoint:
        app.router.add_post("/reset", make_reset_handler(cfg))
        logger.warning(
            "POST /reset endpoint is ENABLED — this should be off in production",
            extra={"path": cfg.device_file_path},
        )

    return app


def _build_wifi_backend(cfg: Config) -> WifiBackend:
    if cfg.dev_mode:
        logger.warning(
            "[DEV MODE] using FakeWifiBackend — no real nmcli calls will be made"
        )
        return FakeWifiBackend()
    return RealWifiBackend()


def _install_stop_event() -> asyncio.Event:
    """Set the returned event on SIGTERM/SIGINT so :func:`_serve` can exit."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handler() -> None:
        if not stop_event.is_set():
            logger.info("signal received; initiating shutdown")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler; tests/dev use
            # KeyboardInterrupt-via-asyncio.run instead. Production runs
            # on Linux where this works.
            pass

    return stop_event


if __name__ == "__main__":
    # Allows `python3 main.py` (orchestrator) and `process-c` (console script).
    run()
