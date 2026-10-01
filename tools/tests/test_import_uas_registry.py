"""Reading a registry export (U-01). The database side is in
api/tests/test_uas_import_pg.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.uas_registry import OperatorType, RegistrationStatus
from common.uas_identity import ClassLabel
from tools.import_uas_registry import (
    Report,
    RowError,
    parse_operator,
    parse_uas,
    parse_valid_until,
    read_records,
    render,
)


def test_a_csv_with_a_byte_order_mark_reads_its_first_column(tmp_path: Path) -> None:
    path = tmp_path / "operators.csv"
    path.write_text(
        "﻿registration_number , legal_name\nGEO1, Kartli\n", encoding="utf-8"
    )
    assert read_records(path) == [
        {"registration_number": "GEO1", "legal_name": " Kartli"}
    ]


def test_json_must_be_an_array_of_objects(tmp_path: Path) -> None:
    good = tmp_path / "uas.json"
    good.write_text('[{"serial": "X"}]', encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text('{"serial": "X"}', encoding="utf-8")

    assert read_records(good) == [{"serial": "X"}]
    with pytest.raises(RowError, match="array of objects"):
        read_records(bad)
    with pytest.raises(RowError, match=r".csv or .json"):
        read_records(tmp_path / "export.xlsx")


def test_an_operator_row_is_parsed_and_absent_columns_are_left_out() -> None:
    number, fields, status = parse_operator(
        {
            "registration_number": " GEO123 ",
            "legal_name": "Kartli",
            "operator_type": "Legal",
            "contact_email": "",
        }
    )
    assert number == "GEO123"
    assert fields == {
        "legal_name": "Kartli",
        "operator_type": OperatorType.LEGAL_PERSON,
        # Present and empty: cleared.
        "contact_email": None,
    }
    # No status column: active.
    assert status is RegistrationStatus.ACTIVE


@pytest.mark.parametrize(
    ("row", "why"),
    [
        (
            {"legal_name": "x", "operator_type": "legal"},
            "registration_number is required",
        ),
        (
            {"registration_number": "G", "operator_type": "legal"},
            "legal_name is required",
        ),
        (
            {"registration_number": "G", "legal_name": "x", "operator_type": "company"},
            "operator_type",
        ),
        (
            {
                "registration_number": "G",
                "legal_name": "x",
                "operator_type": "legal",
                "status": "paused",
            },
            "status",
        ),
    ],
)
def test_a_bad_operator_row_says_why(row: dict[str, str], why: str) -> None:
    with pytest.raises(RowError, match=why):
        parse_operator(row)


def test_a_date_is_valid_through_that_day_and_a_time_needs_a_zone() -> None:
    assert parse_valid_until("2027-05-01") == datetime(2027, 5, 2, tzinfo=UTC)
    assert parse_valid_until("2027-05-01T12:00:00+04:00") == datetime(
        2027, 5, 1, 8, tzinfo=UTC
    )
    with pytest.raises(RowError, match="zone"):
        parse_valid_until("2027-05-01T12:00:00")
    with pytest.raises(RowError, match="ISO 8601"):
        parse_valid_until("01/05/2027")


def test_a_uas_row_is_parsed() -> None:
    serial, operator, fields, status = parse_uas(
        {
            "serial": "1A2B1X",
            "operator_registration_number": "GEO1",
            "class_label": "c2",
            "mtom_g": "3600",
            "status": "Suspended",
        }
    )
    assert (serial, operator, status) == (
        "1A2B1X",
        "GEO1",
        RegistrationStatus.SUSPENDED,
    )
    assert fields == {"class_label": ClassLabel.C2, "mtom_g": 3600}


@pytest.mark.parametrize("given", ["", "none", "None"])
def test_no_class_label_is_none(given: str) -> None:
    _, _, fields, _ = parse_uas(
        {"serial": "X", "operator_registration_number": "G", "class_label": given}
    )
    assert fields == {"class_label": None}


@pytest.mark.parametrize(
    ("row", "why"),
    [
        (
            {"serial": "X", "operator_registration_number": "G", "class_label": "C7"},
            "C0-C6",
        ),
        (
            {"serial": "X", "operator_registration_number": "G", "mtom_g": "3.6"},
            "whole",
        ),
        (
            {"serial": "X", "operator_registration_number": "G", "mtom_g": "0"},
            "positive",
        ),
        ({"serial": "X"}, "operator_registration_number is required"),
    ],
)
def test_a_bad_uas_row_says_why(row: dict[str, str], why: str) -> None:
    with pytest.raises(RowError, match=why):
        parse_uas(row)


def test_the_report_names_what_changed_and_says_a_dry_run_wrote_nothing() -> None:
    report = Report(dry_run=True)
    report.add("operator", "GEO1", "create")
    report.add("operator", "GEO2", "unchanged")
    report.add("uas", "X", "update", {"mtom_g": {"from": 1, "to": 2}})
    report.add("uas", "Y", "refused", {"code": "invalid_serial", "message": "bad"})

    text = render(report)

    assert "create    operator GEO1" in text
    assert "GEO2" not in text
    assert "update    uas      X: mtom_g" in text
    assert "refused   uas      Y: bad" in text
    assert "operator: 1 created, 0 updated, 1 unchanged, 0 refused" in text
    assert "uas: 0 created, 1 updated, 0 unchanged, 1 refused" in text
    assert text.endswith("dry run: nothing was written")
    assert report.refused == 1
