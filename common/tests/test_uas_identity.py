"""Serials and registration numbers (U-01)."""

from __future__ import annotations

import re

import pytest

from common.uas_identity import (
    ClassLabel,
    cta2063_problem,
    is_cta2063,
    public_registration_number,
    registration_number_problem,
    serial_problem,
)

EU = re.compile(r"^[A-Z]{3}[A-Za-z0-9]{8,16}$")


@pytest.mark.parametrize(
    "serial",
    [
        "1A2B1X",  # length 1
        "1A2B9123456789",  # length 9
        "1A2BF123456789ABCDEF",  # length 15, 20 characters in all
        "1581A1234567890",  # length A = 10
    ],
)
def test_a_well_formed_cta_serial_is_accepted(serial: str) -> None:
    assert cta2063_problem(serial) is None
    assert is_cta2063(serial)


@pytest.mark.parametrize(
    ("serial", "why"),
    [
        ("1A2B3AB", "says 3"),  # length says 3, 2 follow
        ("1A2B2ABC", "says 2"),  # length says 2, 3 follow
        ("1O2B1X", "not a CTA"),  # O is not allowed
        ("1A2B1I", "not a CTA"),  # nor I
        ("1a2b1x", "not a CTA"),  # lower case
        ("1A2B0X", "not a CTA"),  # length 0 does not exist
        ("1A2BG123456789ABCDEFG", "not a CTA"),  # G is not a length
        ("", "not a CTA"),
    ],
)
def test_a_malformed_cta_serial_says_why(serial: str, why: str) -> None:
    problem = cta2063_problem(serial)
    assert problem is not None
    assert why in problem


def test_only_the_broadcasting_classes_need_a_cta_serial() -> None:
    legacy = "DJI-0042"
    for label in (
        ClassLabel.C1,
        ClassLabel.C2,
        ClassLabel.C3,
        ClassLabel.C5,
        ClassLabel.C6,
    ):
        assert serial_problem(legacy, label) is not None, label
    for no_cta in (ClassLabel.C0, ClassLabel.C4, None):
        assert serial_problem(legacy, no_cta) is None, no_cta
    assert serial_problem("", None) == "a serial number is required"


def test_a_registration_number_matching_the_pattern_is_accepted() -> None:
    assert registration_number_problem("FIN87astrdge12k8", EU) is None


@pytest.mark.parametrize(
    ("number", "why"),
    [
        ("FIN87astrdge12k8-xyz", "public part"),
        ("fin87astrdge12k8", "configured format"),
        ("FIN87", "configured format"),
    ],
)
def test_a_registration_number_not_matching_is_refused(number: str, why: str) -> None:
    problem = registration_number_problem(number, EU)
    assert problem is not None
    assert why in problem


@pytest.mark.parametrize(
    ("given", "public"),
    [
        # The EU number with its three secret characters (U-02).
        ("FIN87astrdge12k8-xyz", "FIN87astrdge12k8"),
        (" FIN87astrdge12k8-XY1 ", "FIN87astrdge12k8"),
        # No secret tail: kept as it is.
        ("FIN87astrdge12k8", "FIN87astrdge12k8"),
        ("GEO-OP-SITL", "GEO-OP-SITL"),
        ("FIN87astrdge12k8-", "FIN87astrdge12k8-"),
        ("-xyz", "-xyz"),
        ("FIN87astrdge12k8-x!z", "FIN87astrdge12k8-x!z"),
    ],
)
def test_the_public_part_drops_only_a_three_character_secret(
    given: str, public: str
) -> None:
    assert public_registration_number(given) == public
