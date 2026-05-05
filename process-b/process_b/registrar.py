"""Best-effort shelf registration with the backend.

When ``device.json`` is present (i.e. Process C provisioned this Pi),
Process B should tell the backend "hey, this shelf_id is bound to this
user_id." We do this on **every startup** because the call is idempotent
on the backend side, which makes the system self-healing if a shelves
row is ever lost.

Design
------
- Runs as a background asyncio task spawned by :mod:`process_b.main`.
- Never blocks the rest of the daemon: UDP listener, drainer, and poller
  all start normally regardless of registration outcome.
- Retries transient failures with bounded exponential backoff.
- On 4xx (permanent failure): logs ERROR loudly but exits the loop. Most
  likely cause is a backend schema mismatch or a stale token; either way
  no amount of retry will fix it.
- On success: logs INFO and exits the loop.

This intentionally does not persist a "registered with backend" marker
to device.json. The backend is the source of truth for which shelves it
knows about; if registration succeeded once but the shelves row was
later dropped, we want the next restart to re-register.
"""

from __future__ import annotations

import asyncio
import logging

from process_b.backend_client import (
    BackendClient,
    PermanentBackendError,
    TransientBackendError,
)

logger = logging.getLogger(__name__)

_BACKOFF_INITIAL_SEC = 5.0
_BACKOFF_CAP_SEC = 120.0
_BACKOFF_FACTOR = 2.0


async def register_with_backoff(
    *,
    client: BackendClient,
    shelf_id: str,
    user_id: str,
    stop_event: asyncio.Event,
) -> None:
    """Loop until registration succeeds, fails permanently, or shutdown.

    Returns when one of:
    - Backend returns 2xx (success).
    - Backend returns 4xx (permanent — see module docstring).
    - ``stop_event`` is set.

    Never raises; all errors are logged.
    """
    delay = _BACKOFF_INITIAL_SEC
    attempt = 0

    while not stop_event.is_set():
        attempt += 1
        try:
            response = await client.register_shelf(
                shelf_id=shelf_id, user_id=user_id
            )
        except TransientBackendError as exc:
            logger.warning(
                "shelf registration transient failure; will retry",
                extra={
                    "shelf_id": shelf_id,
                    "attempt": attempt,
                    "delay_sec": delay,
                    "status": exc.status,
                },
            )
            if await _sleep_or_stop(delay, stop_event):
                return
            delay = min(delay * _BACKOFF_FACTOR, _BACKOFF_CAP_SEC)
            continue
        except PermanentBackendError as exc:
            logger.error(
                "shelf registration permanently rejected; giving up",
                extra={
                    "shelf_id": shelf_id,
                    "user_id": user_id,
                    "attempt": attempt,
                    "status": exc.status,
                    "body": exc.body,
                },
            )
            return
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "shelf registration raised unexpectedly; will retry",
                extra={"shelf_id": shelf_id, "attempt": attempt, "delay_sec": delay},
            )
            if await _sleep_or_stop(delay, stop_event):
                return
            delay = min(delay * _BACKOFF_FACTOR, _BACKOFF_CAP_SEC)
            continue

        logger.info(
            "shelf registered with backend",
            extra={
                "shelf_id": shelf_id,
                "user_id": user_id,
                "attempt": attempt,
                "echoed_shelf_id": response.shelf_id,
            },
        )
        return


async def _sleep_or_stop(delay: float, stop_event: asyncio.Event) -> bool:
    """Sleep ``delay`` seconds, but wake early if ``stop_event`` is set.

    Returns ``True`` if shutdown was signalled (caller should bail out),
    ``False`` if the delay elapsed normally.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return False
    return True
