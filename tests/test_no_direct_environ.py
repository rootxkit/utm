"""No service reads the environment directly.

This is P0-07's acceptance criterion, enforced rather than asserted in a
README. Configuration reaches a service through `common.config`, where it is
validated once at startup; an `os.getenv` buried in a module is a value that
was never validated, has no declared type, and fails at the moment it is first
used rather than before the process starts.

The check is AST-based, so a mention in a comment or a docstring does not trip
it, and `os.environ` written in any of its spellings does.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVICE_PACKAGES = ("agent", "gateway", "api", "airspace")

# common.config is the one place allowed to reach the environment, and it does
# so through pydantic-settings rather than os.environ. Tests may set up their
# own environment; they are not services.
FORBIDDEN_NAMES = frozenset({"environ", "getenv", "putenv", "environb"})


def _service_modules() -> list[Path]:
    modules: list[Path] = []
    for package in SERVICE_PACKAGES:
        modules.extend(
            path
            for path in (REPO_ROOT / package).rglob("*.py")
            if "tests" not in path.parts
        )
    return modules


def _environment_accesses(source: str) -> list[str]:
    """Return a description of every direct environment access in `source`."""
    tree = ast.parse(source)
    found: list[str] = []

    for node in ast.walk(tree):
        # os.environ[...] / os.getenv(...)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in FORBIDDEN_NAMES
        ):
            found.append(f"os.{node.attr} (line {node.lineno})")

        # from os import environ, getenv
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            found.extend(
                f"from os import {alias.name} (line {node.lineno})"
                for alias in node.names
                if alias.name in FORBIDDEN_NAMES
            )

    return found


def test_there_are_service_modules_to_check() -> None:
    """Guard against the check silently passing because it found nothing."""
    assert _service_modules(), "no service modules discovered"


@pytest.mark.parametrize("module", _service_modules(), ids=lambda path: str(path.name))
def test_service_does_not_read_the_environment_directly(module: Path) -> None:
    accesses = _environment_accesses(module.read_text(encoding="utf-8"))

    relative = module.relative_to(REPO_ROOT).as_posix()
    assert not accesses, (
        f"{relative} reads the environment directly ({', '.join(accesses)}). "
        f"Declare the value on the service's settings class instead."
    )


def test_every_service_package_has_a_settings_class() -> None:
    """A service that imports nothing from common has not been wired up."""
    for package in SERVICE_PACKAGES:
        config = REPO_ROOT / package / "config.py"
        assert config.is_file(), f"{package} has no config.py"

        source = config.read_text(encoding="utf-8")
        assert "from common import" in source or "from common." in source, (
            f"{package}/config.py does not import the shared library"
        )


def test_the_check_detects_what_it_is_looking_for() -> None:
    """The detector must actually detect; a broken check is worse than none."""
    assert _environment_accesses("import os\nx = os.environ['A']\n")
    assert _environment_accesses("import os\nx = os.getenv('A')\n")
    assert _environment_accesses("from os import getenv\nx = getenv('A')\n")
    assert not _environment_accesses("# os.environ is not read here\nx = 1\n")
    assert not _environment_accesses('"""Docstring mentioning os.getenv."""\n')
