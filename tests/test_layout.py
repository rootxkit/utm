"""The repository layout is a contract, so it is tested like one.

CLAUDE.md documents a fixed set of directories, and the tooling configuration
restates overlapping subsets of them in five separate places: setuptools
packages, mypy files, the mypy strict override, pytest testpaths and coverage
sources. These tests fail loudly when a directory is added to one and forgotten
in the others, which is otherwise silent — mypy simply stops checking the module
it was never told about, and nobody finds out until something ships untyped.

The three lists are deliberately not the same, and the differences are the
point:

- INSTALLED_PACKAGES ship in the distribution. tools/ does not: it holds
  operator diagnostics run by hand at a ground station.
- TYPE_CHECKED_DIRS is wider, because code that never ships can still be wrong.
- STRICT_DIRS is the subset where a type error is a build failure.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Python packages that ship in the installed distribution.
INSTALLED_PACKAGES = (
    "common",
    "agent",
    "gateway",
    "api",
    "airspace",
)

# Python that is checked and tested but deliberately not installed.
UNINSTALLED_PYTHON_DIRS = ("tools",)

TYPE_CHECKED_DIRS = INSTALLED_PACKAGES + UNINSTALLED_PYTHON_DIRS

# Directories that exist but are not Python.
NON_PYTHON_DIRS = ("web-pilot", "infra", "sim", "docs")

# mypy --strict applies to these. CLAUDE.md names gateway and airspace as
# safety-relevant. common is included because they import it;
# tools because the probe produces the evidence an architectural decision rests
# on (P1-00), where a silent parsing bug is not cheaper than one in a service,
# only harder to notice; and agent because the ground relay is where the Stage
# 0 guarantee — that the server cannot reach the aircraft — is enforced, and
# where telemetry is lost for good if it is lost at all. api because it is the
# sign-in boundary and the only writer of the fleet registry and its telemetry
# projection.
STRICT_DIRS = ("common", "gateway", "airspace", "tools", "agent", "api")

# Branch coverage is gated on these. api is strict-typed but not gated: its
# coverage is best effort (CLAUDE.md), and the gate's figures were measured
# without it.
COVERED_DIRS = ("common", "gateway", "airspace", "tools", "agent")

# The per-module half of mypy's --strict bundle. The rest of the bundle is set
# globally and so is not repeated in the override.
STRICT_FLAGS = frozenset(
    {
        "disallow_any_generics",
        "disallow_subclassing_any",
        "disallow_untyped_calls",
        "disallow_untyped_defs",
        "disallow_incomplete_defs",
        "check_untyped_defs",
        "disallow_untyped_decorators",
        "warn_return_any",
        "strict_equality",
        "extra_checks",
    }
)


@pytest.fixture(scope="module")
def pyproject() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


@pytest.mark.parametrize("package", TYPE_CHECKED_DIRS)
def test_package_is_importable(package: str) -> None:
    assert importlib.import_module(package) is not None


@pytest.mark.parametrize("directory", TYPE_CHECKED_DIRS + NON_PYTHON_DIRS)
def test_documented_directory_exists(directory: str) -> None:
    assert (REPO_ROOT / directory).is_dir()


def test_setuptools_declares_every_installed_package(pyproject: dict) -> None:
    declared = pyproject["tool"]["setuptools"]["packages"]
    assert sorted(declared) == sorted(INSTALLED_PACKAGES)


def test_tools_is_not_installed(pyproject: dict) -> None:
    """Diagnostics are run from a checkout, not imported from site-packages."""
    declared = pyproject["tool"]["setuptools"]["packages"]
    for directory in UNINSTALLED_PYTHON_DIRS:
        assert directory not in declared


def test_mypy_checks_every_python_directory(pyproject: dict) -> None:
    checked = pyproject["tool"]["mypy"]["files"]
    assert sorted(checked) == sorted(TYPE_CHECKED_DIRS)


def test_pytest_collects_every_python_directory(pyproject: dict) -> None:
    testpaths = pyproject["tool"]["pytest"]["ini_options"]["testpaths"]
    assert set(testpaths) >= set(TYPE_CHECKED_DIRS)
    assert "tests" in testpaths


def test_mypy_is_strict_on_the_safety_relevant_directories(pyproject: dict) -> None:
    """The strict bundle must be expanded, and cover exactly those directories.

    mypy's `strict` flag is global: setting it inside a per-module override
    turns strict on everywhere, which is why the flags are listed out. If
    someone collapses them back to `strict = true`, this fails.
    """
    overrides = pyproject["tool"]["mypy"]["overrides"]
    strict_override = next(
        override
        for override in overrides
        if set(override["module"]) == {f"{name}.*" for name in STRICT_DIRS}
    )

    assert "strict" not in strict_override, (
        "mypy's `strict` is a global flag; expand the bundle instead"
    )
    assert strict_override.keys() >= STRICT_FLAGS
    assert all(strict_override[flag] is True for flag in STRICT_FLAGS)


def test_coverage_is_measured_on_the_safety_relevant_directories(
    pyproject: dict,
) -> None:
    measured = pyproject["tool"]["coverage"]["run"]["source"]
    assert sorted(measured) == sorted(COVERED_DIRS)
    assert set(COVERED_DIRS) <= set(STRICT_DIRS)


def test_only_tools_and_tests_may_print(pyproject: dict) -> None:
    """T201 is lifted in exactly two places, for two different reasons.

    tools/ prints because its stdout IS the deliverable — it gets pasted into
    a decision record. Tests print because a test that measures something has
    to report the number; agent/tests/test_halfopen.py exists to produce one.

    Nowhere else: in a service, logging goes through common/, and a print() is
    a log line no incident review will ever find.
    """
    ignores = pyproject["tool"]["ruff"]["lint"]["per-file-ignores"]
    printing_allowed = {
        pattern for pattern, codes in ignores.items() if "T201" in codes
    }

    assert printing_allowed == {"tools/**", "**/tests/**"}
