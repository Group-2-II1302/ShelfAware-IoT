"""IPC: Process B → Process A control channel.

Process A listens on a UDP control port for wake commands. Process B sends
``{"type": "wake"}`` as a single JSON UTF-8 datagram. Fire-and-forget: no
ack, no retry. The backend's command stream is at-most-once by design, so
one missed wake is an accepted failure mode rather than something to engineer
around.

The wire payload is tentative — finalize with Process A's owner.
"""

from __future__ import annotations

import json
import logging
import socket

logger = logging.getLogger(__name__)


_WAKE_PAYLOAD = json.dumps({"type": "wake"}).encode("utf-8")


def send_wake(host: str, port: int) -> None:
    """Send one wake datagram to ``host:port``. Logs and swallows errors.

    Sync because UDP ``sendto`` is non-blocking and immediate; wrapping in
    ``asyncio.to_thread`` would add overhead for no benefit. Safe to call
    from inside an async function.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(_WAKE_PAYLOAD, (host, port))
    except OSError as exc:
        logger.warning(
            "ipc send_wake failed",
            extra={"host": host, "port": port, "error": str(exc)},
        )
        return

    logger.debug("ipc wake sent", extra={"host": host, "port": port})
