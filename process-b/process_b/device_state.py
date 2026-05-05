"""Read-only access to ``device.json`` from Process C.

Process C owns writes to this file at provisioning time. Process B only
ever reads it — once at startup, to learn its ``shelf_id`` and the
``user_id`` it should register with the backend.

The file lives at ``DEVICE_FILE_PATH`` (default
``/etc/shelfaware/device.json``). Format is documented in
:mod:`process_c.device_state`; we only depend on a stable read shape so
the two packages don't need a shared library.

Why a separate module from process_c.device_state
-------------------------------------------------
Process B and Process C are independent installables that may run on
different deploy paths and at different versions. Coupling them via a
shared package would mean either splitting out a third package
(``process-shared``) or making B import C, which inverts the lifecycle
direction (C is the writer, B is the reader; readers shouldn't import
writers). A 30-line copy is the right amount of duplication.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DeviceStateError(Exception):
    """Raised when device.json exists but is malformed."""


@dataclass(frozen=True)
class DeviceState:
    user_id: str
    shelf_id: str
    provisioned_at: str
    schema_version: int = 1


def read(path: str | os.PathLike[str]) -> DeviceState | None:
    """Return the persisted :class:`DeviceState`, or ``None`` if absent.

    Raises :class:`DeviceStateError` if the file exists but is malformed
    — callers should treat that as a hard failure rather than silently
    fall back to env-var config (corrupted state should be operator-fixed,
    not papered over).
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
        schema_version=int(data.get("schema_version", 1)),
    )
