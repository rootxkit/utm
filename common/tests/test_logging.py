"""Logging emits one parseable JSON object per record, carrying its context."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from common.logging import JsonFormatter, bind, configure_logging, get_logger


def _emit(
    capsys: pytest.CaptureFixture[str],
    level: str = "INFO",
    *,
    service: str = "test-service",
) -> Any:
    """Return the single JSON record written to stdout."""
    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 1, f"expected one log line, got {len(captured)}"
    return json.loads(captured[0])


def _record(**attributes: Any) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    for key, value in attributes.items():
        setattr(record, key, value)
    return record


def test_format_produces_the_documented_envelope() -> None:
    payload = json.loads(JsonFormatter(service="gateway").format(_record()))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["service"] == "gateway"
    assert payload["message"] == "hello world"
    assert payload["ts"].endswith("+00:00"), "timestamps must be UTC-aware"


def test_extra_fields_become_top_level_fields() -> None:
    payload = json.loads(
        JsonFormatter(service="gateway").format(
            _record(drone_id="d-17", mission_id="m-4", batt_pct=22.5)
        )
    )

    assert payload["drone_id"] == "d-17"
    assert payload["mission_id"] == "m-4"
    assert payload["batt_pct"] == 22.5


def test_unserialisable_values_do_not_lose_the_line() -> None:
    """A log call must never raise, whatever it is handed."""

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    payload = json.loads(
        JsonFormatter(service="gateway").format(_record(thing=Opaque()))
    )

    assert payload["thing"] == "<opaque>"


def test_exception_is_rendered_into_the_record() -> None:
    try:
        raise ValueError("zone import failed")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter(service="gateway").format(record))

    assert "ValueError: zone import failed" in payload["exception"]


def test_configure_logging_writes_json_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(service="airspace", level="INFO")
    get_logger("airspace.monitor").info("alert raised", extra={"drone_id": "d-9"})

    payload = _emit(capsys)
    assert payload["service"] == "airspace"
    assert payload["message"] == "alert raised"
    assert payload["drone_id"] == "d-9"


def test_configure_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    """A second call must not double every line."""
    configure_logging(service="airspace", level="INFO")
    configure_logging(service="airspace", level="INFO")
    get_logger("airspace.monitor").info("once")

    _emit(capsys)  # asserts exactly one line


def test_level_is_honoured(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="gateway", level="WARNING")
    get_logger("gateway.ingest").info("suppressed")

    assert capsys.readouterr().out == ""


def test_bind_attaches_context_to_every_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(service="gateway", level="INFO")
    log = bind(get_logger("gateway.ingest"), drone_id="d-3", mission_id="m-1")
    log.warning("deviation detected")

    payload = _emit(capsys)
    assert payload["drone_id"] == "d-3"
    assert payload["mission_id"] == "m-1"


def test_bind_is_additive_and_leaves_the_original_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(service="gateway", level="INFO")
    base = bind(get_logger("gateway.ingest"), drone_id="d-3")
    narrowed = bind(base, mission_id="m-1")

    narrowed.info("narrowed")
    payload = _emit(capsys)
    assert payload["drone_id"] == "d-3"
    assert payload["mission_id"] == "m-1"

    base.info("base")
    payload = _emit(capsys)
    assert payload["drone_id"] == "d-3"
    assert "mission_id" not in payload


def test_call_site_context_overrides_bound_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(service="gateway", level="INFO")
    log = bind(get_logger("gateway.ingest"), drone_id="d-3")
    log.info("reassigned", extra={"drone_id": "d-9"})

    assert _emit(capsys)["drone_id"] == "d-9"
