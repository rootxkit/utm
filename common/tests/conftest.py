"""Shared fixtures for `common/` tests."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from common.logging import JsonFormatter


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Undo what `configure_logging` does to the root logger (S-26).

    It is meant to run once per process, at service start: it sets the root
    level and installs a JSON handler. A test that calls it (through
    `start_service`) would otherwise leave level ERROR in place, and a later
    test in another package that expects a WARNING to be logged sees
    nothing. pytest's own capture handlers are added and removed by pytest
    per phase, so they are left alone here.
    """
    root = logging.getLogger()
    level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if isinstance(handler.formatter, JsonFormatter):
                root.removeHandler(handler)
        root.setLevel(level)
