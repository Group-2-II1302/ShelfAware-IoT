"""Structured-log configuration for Process B.

One JSON-per-line formatter on stdout, ready for ``journald`` ingest. The
goal is not pretty for humans — ``journalctl -u process-b`` is the human
interface — but predictable for log shippers, alerting rules, and grep.

Why hand-rolled instead of structlog
------------------------------------
We don't yet need cross-task context propagation, log filtering pipelines,
or pluggable processors. Stdlib ``logging`` plus a 40-line formatter covers
the use case. If we later want richer structured logging in deep call
stacks, switching to structlog is a one-day job.

What ends up on each line
-------------------------
Always: ``ts`` (UTC ISO-8601 with milliseconds), ``level``, ``logger``,
``msg``.

Optionally:

- ``task`` — set by a :class:`logging.LoggerAdapter` per asyncio task.
- ``exc`` — formatted traceback when ``logger.exception(...)`` is used.
- Anything passed via ``extra={...}`` is merged in at the top level. Reserved
  field names (``ts``, ``level``, etc.) are not overwritten by ``extra`` —
  if a caller passes ``extra={"level": "EMERGENCY"}`` we drop it on the
  floor rather than corrupt the output shape.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any


_RESERVED_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"ts", "level", "logger", "msg", "exc"})

# Attribute names that ``logging.LogRecord`` sets internally. Anything *not*
# in this set on a record was passed via ``extra=`` and should be merged
# into the JSON object.
_LOGRECORD_BUILTIN_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",  # added in Python 3.12
    }
)


class JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _format_ts(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _LOGRECORD_BUILTIN_ATTRS:
                continue
            if key in _RESERVED_TOP_LEVEL_KEYS:
                continue
            payload[key] = _coerce(value)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.stack_info:
            payload["exc"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=_coerce, separators=(",", ":"))


def _format_ts(created: float) -> str:
    """Render a UNIX timestamp as ISO 8601 UTC with milliseconds."""
    dt = _dt.datetime.fromtimestamp(created, tz=_dt.UTC)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    millis = f"{dt.microsecond // 1000:03d}"
    return f"{base}.{millis}Z"


def _coerce(value: Any) -> Any:
    """Coerce values to something json.dumps can render safely.

    Most things round-trip; a few (like exceptions or arbitrary objects)
    fall back to ``repr`` rather than raising — a log line is best-effort
    diagnostic, not durable data.
    """
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return repr(value)


def configure(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: re-running replaces the handler rather than stacking. Safe
    to call from tests that import :mod:`process_b.main` modules.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
