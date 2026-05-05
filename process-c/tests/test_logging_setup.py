"""Tests for :mod:`process_c.logging_setup`."""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from process_c import logging_setup
from process_c.logging_setup import JsonFormatter


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Make sure each test starts with no leftover handlers."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)


def test_format_emits_one_json_line() -> None:
    record = logging.LogRecord(
        name="process_c.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    out = JsonFormatter().format(record)
    assert "\n" not in out
    payload = json.loads(out)
    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "process_c.test"
    assert payload["ts"].endswith("Z")


def test_extra_fields_are_merged() -> None:
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="m", args=(), exc_info=None,
    )
    record.shelf_id = "abc"  # how logging.Logger.makeRecord injects extras
    record.attempt = 3

    payload = json.loads(JsonFormatter().format(record))
    assert payload["shelf_id"] == "abc"
    assert payload["attempt"] == 3


def test_extra_does_not_overwrite_reserved() -> None:
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="real", args=(), exc_info=None,
    )
    # Try to inject a "level" field via extra; formatter should ignore it.
    record.level = "EMERGENCY"  # type: ignore[assignment]
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"


def test_exception_appears_under_exc_key() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="x", level=logging.ERROR, pathname="", lineno=0,
            msg="oops", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "exc" in payload
    assert "ValueError" in payload["exc"]
    assert "boom" in payload["exc"]


def test_configure_is_idempotent() -> None:
    logging_setup.configure("INFO")
    first = logging.getLogger().handlers[:]
    logging_setup.configure("INFO")
    second = logging.getLogger().handlers[:]
    # Same number of handlers; not duplicated.
    assert len(second) == len(first) == 1


def test_configure_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    logging_setup.configure("INFO")
    logging.getLogger("process_c.test").info("smoke")
    out = capsys.readouterr().out
    payload = json.loads(out.strip())
    assert payload["msg"] == "smoke"
