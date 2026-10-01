"""Each migration tree has exactly one head.

Two branches that each add a migration on top of the same revision both pass
their own CI, and merge into a tree with two heads: `alembic upgrade head`
then refuses to run, and the deployment stops at its migration step. U-15 and
U-03 each added a relational `0006` on `0005`, which is how this was nearly
found. Reading the trees needs no database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).resolve().parent.parent / "infra" / "migrations"
TREES = ("relational", "telemetry")


def script(tree: str) -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(MIGRATIONS / tree / "alembic.ini")))


@pytest.mark.parametrize("tree", TREES)
def test_the_tree_has_exactly_one_head(tree: str) -> None:
    heads = script(tree).get_heads()
    assert len(heads) == 1, (
        f"infra/migrations/{tree} has {len(heads)} heads: {sorted(heads)}. "
        "Re-point the newer migration's down_revision at the other head."
    )


@pytest.mark.parametrize("tree", TREES)
def test_every_revision_reaches_the_base(tree: str) -> None:
    """The paired presence: the walk sees every file, so one head is a
    finding about the tree, not about a walk that found nothing."""
    directory = script(tree)
    revisions = list(directory.walk_revisions())
    files = [
        p for p in (MIGRATIONS / tree / "versions").glob("*.py") if p.stem[0].isdigit()
    ]
    assert len(revisions) == len(files) >= 1
    assert [r for r in revisions if r.down_revision is None] != []


def test_two_heads_are_found(tmp_path: Path) -> None:
    """The guard itself, pointed at a tree with two heads."""
    versions = tmp_path / "versions"
    versions.mkdir()
    for revision, down in (("a", None), ("b", "a"), ("c", "a")):
        (versions / f"{revision}.py").write_text(
            f"revision = {revision!r}\ndown_revision = {down!r}\n"
            "branch_labels = None\ndepends_on = None\n",
            encoding="utf-8",
        )
    config = Config()
    config.set_main_option("script_location", str(tmp_path))
    config.set_main_option("path_separator", "os")
    config.set_main_option("version_locations", str(versions))
    assert sorted(ScriptDirectory.from_config(config).get_heads()) == ["b", "c"]
