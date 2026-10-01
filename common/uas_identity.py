"""How a UAS and its operator are identified. U-01.

Pure functions, in `common/` because both sides of the registry need them:
the API checks what is registered (U-01), and the Remote ID path matches
what is broadcast against it (U-02). Two copies of these rules would be two
answers to "is this the same aircraft".

## Serial numbers: ANSI/CTA-2063-A

A physical serial number is 4 characters of manufacturer code, 1 character
giving the length of what follows (1-9, then A-F for 10-15), and the
manufacturer's serial of exactly that length. Digits and upper-case letters,
except O and I, which read as 0 and 1. At most 20 characters.

2019/945 requires a CTA-2063-A serial of the classes that must broadcast
direct Remote ID: C1, C2 and C3, and C5 and C6, which are built on C3. C0 and
C4 have no such requirement, and neither has a legacy or privately built
aircraft with no class label. Their serials are whatever the maker printed.

## Operator registration numbers

The EU number is a country code and alphanumerics; the exact shape a
Georgian number takes is not confirmed, so the pattern is configuration, not
code (`UAS_OPERATOR_REGISTRATION_PATTERN` in `api.config`). Comparison is
case-insensitive.
"""

from __future__ import annotations

import re
from enum import StrEnum

# Digits and upper-case letters without O and I (CTA-2063-A).
_CTA_CHAR = "[0-9A-HJ-NP-Z]"
_CTA_SHAPE = re.compile(rf"^({_CTA_CHAR}{{4}})([1-9A-F])({_CTA_CHAR}{{1,15}})$")


class RegistrationStatus(StrEnum):
    """An operator's, a remote pilot's or a UAS's registration (U-01).

    Here because both sides need it: the API sets it, and U-02's resolvers
    read it back from the projection (`gateway/registry_projection.py`).
    """

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class ClassLabel(StrEnum):
    """The class marking of 2019/945. None of them: no class label."""

    C0 = "C0"
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"
    C4 = "C4"
    C5 = "C5"
    C6 = "C6"


# The classes 2019/945 requires to carry a CTA-2063-A serial (direct Remote ID).
CTA2063_REQUIRED = frozenset(
    {ClassLabel.C1, ClassLabel.C2, ClassLabel.C3, ClassLabel.C5, ClassLabel.C6}
)


def normalize_serial(serial: str) -> str:
    """A serial as it is stored and compared: surrounding space removed.

    Case is kept. A CTA-2063-A serial is upper case by definition, and a
    legacy serial is whatever the maker printed; folding it could make two
    different aircraft one.
    """
    return serial.strip()


def cta2063_problem(serial: str) -> str | None:
    """Why `serial` is not a valid ANSI/CTA-2063-A serial, or None if it is."""
    found = _CTA_SHAPE.match(serial)
    if found is None:
        return (
            "not a CTA-2063-A serial: 4-character manufacturer code, a length "
            "character (1-9, A-F), then that many characters; digits and "
            "upper-case letters without O and I"
        )
    declared = int(found.group(2), 16)
    actual = len(found.group(3))
    if declared != actual:
        return (
            f"not a CTA-2063-A serial: the length character says {declared} "
            f"characters follow, and {actual} do"
        )
    return None


def is_cta2063(serial: str) -> bool:
    return cta2063_problem(serial) is None


def serial_problem(serial: str, class_label: ClassLabel | None) -> str | None:
    """Why `serial` cannot be registered for an aircraft of this class.

    Only an empty serial is refused for a class with no CTA-2063-A
    requirement.
    """
    if not serial:
        return "a serial number is required"
    if class_label in CTA2063_REQUIRED:
        problem = cta2063_problem(serial)
        if problem is not None:
            return f"class {class_label} requires a CTA-2063-A serial; {problem}"
    return None


def normalize_registration_number(value: str) -> str:
    """A registration number as it is stored: surrounding space removed."""
    return value.strip()


def registration_number_problem(value: str, pattern: re.Pattern[str]) -> str | None:
    """Why `value` is not an operator registration number, or None.

    The EU number's three secret characters follow a hyphen; they are for the
    operator to prove the number is theirs and are never registered here.
    """
    if "-" in value:
        return (
            "only the public part of the registration number is registered; "
            "leave out the hyphen and the secret characters after it"
        )
    if pattern.fullmatch(value) is None:
        return f"does not match the configured format {pattern.pattern}"
    return None
