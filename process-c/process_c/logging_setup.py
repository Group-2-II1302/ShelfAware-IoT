"""Structured JSON logging for Process C.

Same shape as :mod:`process_b.logging_setup` so logs from both daemons can
be ingested by the same downstream tooling (journald + ``jq``).

One JSON object per line, written to stdout. Fields:

- ``ts``     — ISO 8601 UTC, millisecond precision
- ``level``  — log level name
- ``logger`` — logger name (e.g. ``process_c.handlers``)
- ``msg``    — formatted message
- any ``extra={}`` keys attached to the record (excluding stdlib reserved
  attribute names so we don't double-stamp ``msg``, ``levelname`` etc.)
- ``exc``    — formatted traceback, only present when ``exc_info`` is set
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_RESERVED_LOGRECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload: dict[str, Any] = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Merge in any extra={} fields, skipping reserved LogRecord attrs.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                # Don't let extras overwrite our own reserved fields.
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure(level: str = "INFO") -> None:
    """Install a single stdout handler with :class:`JsonFormatter`.

    Idempotent: replaces any existing handlers on the root logger so calling
    twice does not produce duplicate lines.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
