"""Read/write the device provisioning file.

Single source of truth for "is this Pi provisioned, and if so for whom?".
Process C writes it on successful provisioning. Process B reads it at
startup to learn its ``shelf_id`` and to register with the backend.

File location is ``cfg.device_file_path`` (default
``/etc/shelfaware/device.json``). Schema:

.. code-block:: json

   {
     "user_id":        "<UUIDv4>",
     "shelf_id":       "<UUIDv4>",
     "provisioned_at": "<ISO 8601 UTC>",
     "schema_version": 1
   }

``schema_version`` is a forward-compatibility hatch: if we ever need to
change the file's shape, bumping this lets readers detect older formats
and migrate.

Atomic writes
-------------
Writes go to a sibling ``.tmp`` file then ``os.replace`` it onto the
target path. ``os.replace`` is atomic on POSIX (and Windows for files on
the same volume), so a reader can never observe a half-written file even
if Process C is killed mid-write.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class DeviceStateError(Exception):
    """Raised when device.json exists but is malformed."""


@dataclass(frozen=True)
class DeviceState:
    user_id: str
    shelf_id: str
    provisioned_at: str
    schema_version: int = SCHEMA_VERSION


def read(path: str | os.PathLike[str]) -> DeviceState | None:
    """Return the persisted :class:`DeviceState`, or ``None`` if not provisioned.

    Raises :class:`DeviceStateError` if the file exists but is malformed —
    callers should treat that as a hard failure (don't silently re-provision
    over corrupted data; an operator must look).
    """
    p = Path(path)
    if not p.exists():
        return None

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeviceStateError(f"could not read {p}: {exc}") from exc

    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeviceStateError(f"{p} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise DeviceStateError(f"{p} root must be a JSON object")

    missing = [k for k in ("user_id", "shelf_id", "provisioned_at") if k not in data]
    if missing:
        raise DeviceStateError(f"{p} missing required fields: {missing}")

    return DeviceState(
        user_id=str(data["user_id"]),
        shelf_id=str(data["shelf_id"]),
        provisioned_at=str(data["provisioned_at"]),
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
    )


def write(path: str | os.PathLike[str], state: DeviceState) -> None:
    """Atomically persist ``state`` to ``path``.

    Creates the parent directory if missing (mode 750). The file itself is
    written with mode 640 — readable by the orchestrator/Process B that
    share the root-owned process tree, but not world-readable.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o750)

    payload = {
        "user_id":        state.user_id,
        "shelf_id":       state.shelf_id,
        "provisioned_at": state.provisioned_at,
        "schema_version": state.schema_version,
    }

    # Write to a temp file in the same directory, then atomic-rename onto
    # the target. Same-directory matters: os.replace requires same volume.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".device-",
        suffix=".json.tmp",
        dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o640)
        except OSError:
            # chmod is a no-op on Windows; tolerate it for cross-platform tests.
            pass
        os.replace(tmp_path, p)
    except Exception:
        # Best-effort cleanup of the temp file on any failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info(
        "device.json written",
        extra={"path": str(p), "shelf_id": state.shelf_id, "user_id": state.user_id},
    )


def delete(path: str | os.PathLike[str]) -> bool:
    """Remove the device file. Returns ``True`` if a file was deleted.

    Used by the (debug) ``POST /reset`` endpoint. Idempotent.
    """
    p = Path(path)
    try:
        p.unlink()
    except FileNotFoundError:
        return False
    logger.warning("device.json deleted", extra={"path": str(p)})
    return True


def is_provisioned(path: str | os.PathLike[str]) -> bool:
    """Convenience: ``True`` if a valid (or just-existing) device file is present.

    Doesn't validate fields — see :func:`read` for that. The cheap check is
    enough for ``GET /health`` and the orchestrator's "should I start
    Process C?" decision.
    """
    return Path(path).exists()


def make_state(user_id: str, shelf_id: str) -> DeviceState:
    """Build a :class:`DeviceState` with a fresh ``provisioned_at`` timestamp."""
    return DeviceState(
        user_id=user_id,
        shelf_id=shelf_id,
        provisioned_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        schema_version=SCHEMA_VERSION,
    )
