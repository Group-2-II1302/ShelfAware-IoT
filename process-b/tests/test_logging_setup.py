"""Tests for :mod:`process_b.logging_setup`.

We run real ``logging.LogRecord`` instances through :class:`JsonFormatter`
and parse the output as JSON. ``configure()`` is exercised through capsys
so we see what hits stdout in production.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from process_b.logging_setup import JsonFormatter, configure


def _make_record(
    *,
    name: str = "process_b.test",
    level: int = logging.INFO,
    msg: str = "hello",
    args: tuple = (),
    extra: dict | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=42,
        msg=msg,
        args=args,
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


class TestJsonFormatterShape:
    def test_emits_single_line_json(self) -> None:
        formatter = JsonFormatter()
        line = formatter.format(_make_record())

        assert "\n" not in line
        parsed = json.loads(line)
        assert isinstance(parsed, dict)

    def test_required_fields_present(self) -> None:
        formatter = JsonFormatter()
        line = formatter.format(_make_record(msg="hi"))
        parsed = json.loads(line)

        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "process_b.test"
        assert parsed["msg"] == "hi"
        assert "ts" in parsed

    def test_timestamp_format_is_iso_utc_with_millis(self) -> None:
        formatter = JsonFormatter()
        line = formatter.format(_make_record())
        parsed = json.loads(line)

        # 2026-04-21T08:30:00.123Z shape
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", parsed["ts"]
        )

    def test_message_args_are_interpolated(self) -> None:
        formatter = JsonFormatter()
        line = formatter.format(_make_record(msg="count=%d", args=(7,)))
        parsed = json.loads(line)

        assert parsed["msg"] == "count=7"


class TestExtraMerging:
    def test_extra_keys_merged_at_top_level(self) -> None:
        formatter = JsonFormatter()
        line = formatter.format(
            _make_record(extra={"shelf_id": "abc", "count": 3})
        )
        parsed = json.loads(line)

        assert parsed["shelf_id"] == "abc"
        assert parsed["count"] == 3

    def test_extra_dict_value_is_preserved(self) -> None:
        formatter = JsonFormatter()
        line = formatter.format(
            _make_record(extra={"meta": {"battery": 88, "rssi": -65}})
        )
        parsed = json.loads(line)

        assert parsed["meta"] == {"battery": 88, "rssi": -65}

    def test_extra_cannot_overwrite_reserved_top_level_keys(self) -> None:
        # ``extra={"level": ...}`` would overwrite the LogRecord's levelname
        # if our formatter blindly merged it. The formatter must drop any
        # extra key that collides with the reserved top-level shape.
        formatter = JsonFormatter()
        line = formatter.format(
            _make_record(
                level=logging.INFO,
                extra={"level": "EMERGENCY", "ts": "fake-ts", "logger": "fake"},
            )
        )
        parsed = json.loads(line)

        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "process_b.test"
        # ``ts`` is a real ISO timestamp, not "fake-ts".
        assert parsed["ts"] != "fake-ts"

    def test_logrecord_internal_attrs_are_not_emitted(self) -> None:
        formatter = JsonFormatter()
        line = formatter.format(_make_record())
        parsed = json.loads(line)

        for attr in ("pathname", "filename", "lineno", "process", "thread"):
            assert attr not in parsed

    def test_non_serializable_value_falls_back_to_repr(self) -> None:
        formatter = JsonFormatter()

        class Weird:
            def __repr__(self) -> str:
                return "<Weird:42>"

        line = formatter.format(_make_record(extra={"thing": Weird()}))
        parsed = json.loads(line)

        assert parsed["thing"] == "<Weird:42>"


class TestExceptionRendering:
    def test_logger_exception_emits_exc_field(self) -> None:
        import sys

        logger = logging.getLogger("process_b.test_exc")
        logger.handlers = [logging.NullHandler()]

        try:
            raise ValueError("boom")
        except ValueError:
            record = logger.makeRecord(
                "process_b.test_exc",
                logging.ERROR,
                __file__,
                42,
                "something broke",
                None,
                exc_info=sys.exc_info(),
            )

        formatter = JsonFormatter()
        line = formatter.format(record)
        parsed = json.loads(line)

        assert parsed["msg"] == "something broke"
        assert "exc" in parsed
        assert "ValueError" in parsed["exc"]
        assert "boom" in parsed["exc"]


class TestConfigure:
    def test_configure_is_idempotent_no_handler_stack(self) -> None:
        configure("INFO")
        configure("DEBUG")
        root = logging.getLogger()

        # After two calls we should still have exactly one handler — the
        # one configure() installed.
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG

    def test_configure_emits_json_on_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure("INFO")
        logger = logging.getLogger("process_b.test_configure")

        logger.info("hello", extra={"shelf_id": "abc"})
        captured = capsys.readouterr()

        assert captured.err == ""
        assert captured.out.count("\n") == 1
        parsed = json.loads(captured.out.strip())
        assert parsed["msg"] == "hello"
        assert parsed["shelf_id"] == "abc"

    def test_configure_respects_level_lowercase(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure("warning")
        logger = logging.getLogger("process_b.test_level")

        logger.info("not visible")
        logger.warning("visible")
        captured = capsys.readouterr()

        lines = [line for line in captured.out.split("\n") if line]
        assert len(lines) == 1
        assert json.loads(lines[0])["msg"] == "visible"
