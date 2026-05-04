"""Background provisioning task.

POST /provision returns 202 immediately; the actual WiFi switch happens
here, in a task spawned onto the event loop. This split is essential
because :meth:`WifiBackend.apply_wifi_credentials` blocks for up to
60 seconds, and once it succeeds the Pi has already left the AP — the
HTTP response can't reach the phone any more anyway.

Sequence
--------
1. Persist ``device.json`` (the bit Process B will read on its next start).
2. Schedule the WiFi switch in a background task.
3. Return control to the HTTP handler so it can send 202.
4. The background task calls ``wifi_backend.apply_wifi_credentials`` in a
   thread (it's a blocking, sync call) and logs the outcome.

Errors during the background switch are logged loudly but never re-raised
— at the point we're switching networks, there's nobody on the other end
of the AP-mode HTTP connection to tell.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from process_c.wifi_backend import ConnectResult, WifiBackend

logger = logging.getLogger(__name__)

# Type alias for the post-success hook (see schedule_provision below).
PostSuccessHook = Callable[[], Awaitable[None]]


async def perform_wifi_switch(
    wifi: WifiBackend,
    ssid: str,
    password: str,
    *,
    timeout: int = 60,
    on_success: PostSuccessHook | None = None,
) -> ConnectResult:
    """Run the (blocking) WiFi switch off-loop and log the outcome.

    Returns the :class:`ConnectResult` so callers/tests can assert.
    """
    logger.info(
        "wifi switch starting", extra={"ssid": ssid, "timeout": timeout}
    )

    try:
        # apply_wifi_credentials blocks; offload to a thread.
        result = await asyncio.to_thread(
            wifi.apply_wifi_credentials, ssid, password, timeout
        )
    except Exception:
        logger.exception("wifi switch raised; attempting rollback")
        try:
            await asyncio.to_thread(wifi.rollback_to_ap)
        except Exception:
            logger.exception("rollback also failed")
        return ConnectResult.ERROR

    if result == ConnectResult.SUCCESS:
        logger.info("wifi switch succeeded", extra={"ssid": ssid})
        if on_success is not None:
            try:
                await on_success()
            except Exception:
                logger.exception("post-success hook raised; ignoring")
    else:
        logger.warning(
            "wifi switch did not succeed; backend has rolled back to AP",
            extra={"ssid": ssid, "result": result.name},
        )

    return result


def schedule_provision(
    wifi: WifiBackend,
    ssid: str,
    password: str,
    *,
    timeout: int = 60,
    on_success: PostSuccessHook | None = None,
) -> asyncio.Task[ConnectResult]:
    """Spawn :func:`perform_wifi_switch` as a fire-and-forget task.

    Returns the :class:`asyncio.Task` so tests can await it. Production
    callers (the HTTP handler) discard it.
    """
    task = asyncio.create_task(
        perform_wifi_switch(
            wifi, ssid, password, timeout=timeout, on_success=on_success
        ),
        name=f"wifi-switch-{ssid[:16]}",
    )
    return task
