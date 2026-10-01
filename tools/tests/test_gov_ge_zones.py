"""The airspace.gov.ge converter's command line. U-03."""

from __future__ import annotations

from pathlib import Path

import pytest

from airspace.ed269 import parse
from tools.gov_ge_zones import main

FIXTURES = Path(__file__).resolve().parents[2] / "airspace" / "tests" / "fixtures"


def args(out: Path, rules: Path = FIXTURES / "gov_ge_rules.toml") -> list[str]:
    return [
        "--points",
        str(FIXTURES / "gov_ge_points.js"),
        "--page",
        str(FIXTURES / "gov_ge_page.html"),
        "--rules",
        str(rules),
        "--out",
        str(out),
    ]


def test_the_fixture_is_converted_to_an_importable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "zones.json"
    assert main(args(out)) == 0
    assert "wrote 3 zones" in capsys.readouterr().out
    assert len(parse(out.read_bytes()).zones) == 3


def test_a_refusal_names_the_zone_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.toml"
    text = (FIXTURES / "gov_ge_rules.toml").read_text(encoding="utf-8")
    rules.write_text(text.replace("[kinds.EPR]", "[kinds.NOTEPR]"), encoding="utf-8")
    out = tmp_path / "zones.json"
    assert main(args(out, rules)) == 1
    assert "UGT01_EPR: kind 'EPR' has no rule" in capsys.readouterr().err
    assert not out.exists()


def test_a_rules_file_missing_a_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.toml"
    rules.write_text('[authority]\nname = "x"\n', encoding="utf-8")
    assert main(args(tmp_path / "zones.json", rules)) == 2
    assert "'country'" in capsys.readouterr().err
