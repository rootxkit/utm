"""UAS geographical zones in EUROCAE ED-269 JSON: read strictly, write back. U-03.

Authorities publish UAS geographical zones (EU 2019/947 Art. 15) as ED-269
JSON. This module turns such a file into `GeoZone`s, refusing it with every
problem named (`Problem`: the field's path and why), and turns `GeoZone`s
back into ED-269 JSON. It touches no database: `api/zones.py` stores what it
reads, `airspace/zones.py` turns stored zones into what the monitor checks.

## Where the field names come from

EUROCAE's ED-269 text is paywalled and EUROCONTROL publishes no JSON schema
for it (checked 2026-10-01). The names, types, limits and enumerations here
are pinned to, in order:

1. InterUSS `uas_standards` (`src/uas_standards/eurocae_ed269.py`): the
   typed model InterUSS's ED-269 tests run against;
2. Luxembourg's live national file (`https://drones.geoportail.lu/zones`)
   and InterUSS's ED-269 fixtures;
3. the Swiss BAZL INTERLIS profile (`UASGeographicalZone_V1.ili`), which
   gives the standard's field definitions and the rule that a permanent
   period has no start or end.

Assumptions beyond those sources, each deliberate:

- **`WGS84` as a vertical reference.** Every source above has only `AGL`
  and `AMSL`. The owner's U-03 field list adds WGS84 (height above the
  ellipsoid), so it is read and written here; a file using it is ours, not
  a published ED-269 file.
- **One volume per zone.** ED-269 allows several (`geometry` is a list).
  Every published file seen has one per zone (Luxembourg: 46 of 46), and
  the zone table keeps one geometry per zone, so a zone with more is
  refused by name rather than half-imported.
- **`REQ_AUTHORIZATION` with a Z is refused.** Some states publish it; it
  is not the ED-269 value, and accepting it would change the file on export.
- A daily `startTime` and `endTime` must carry the same offset, so the
  period's day is unambiguous.

## Strict, so the round trip is exact

A file is refused for an unknown field, a value of the wrong type, a value
outside its enumeration or length, an unclosed polygon ring, or a period
that cannot be evaluated. Nothing is normalised on the way in: what is
stored is what was published, so `feature(parse(f))` gives `f` back. The
one exception is `null`: an optional field that is `null` is read as absent
and written back absent.

Coordinates are GeoJSON order, `[longitude, latitude]`, as every published
file and InterUSS's own parsing use (the standard's prose says "lat, lng" in
places; the files do not).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo
from enum import Enum, StrEnum
from typing import Any, Final, Literal

FEET_M = 0.3048
# Refusals listed in one report, so a broken file does not produce a
# thousand-line answer. The count of the rest is said.
MAX_PROBLEMS = 100

Position = tuple[float, float]
Ring = tuple[Position, ...]


class Restriction(StrEnum):
    PROHIBITED = "PROHIBITED"
    REQ_AUTHORISATION = "REQ_AUTHORISATION"
    CONDITIONAL = "CONDITIONAL"
    NO_RESTRICTION = "NO_RESTRICTION"


class Reason(StrEnum):
    AIR_TRAFFIC = "AIR_TRAFFIC"
    SENSITIVE = "SENSITIVE"
    PRIVACY = "PRIVACY"
    POPULATION = "POPULATION"
    NATURE = "NATURE"
    NOISE = "NOISE"
    FOREIGN_TERRITORY = "FOREIGN_TERRITORY"
    EMERGENCY = "EMERGENCY"
    OTHER = "OTHER"


class VerticalReference(StrEnum):
    AGL = "AGL"
    AMSL = "AMSL"
    # Height above the WGS-84 ellipsoid. Not in the published sources; see
    # the module docstring.
    WGS84 = "WGS84"


class Uom(StrEnum):
    M = "M"
    FT = "FT"


class Purpose(StrEnum):
    AUTHORIZATION = "AUTHORIZATION"
    NOTIFICATION = "NOTIFICATION"
    INFORMATION = "INFORMATION"


class YesNo(StrEnum):
    YES = "YES"
    NO = "NO"


DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
ANY_DAY = "ANY"

# Field lengths, from the InterUSS model and the BAZL profile.
IDENTIFIER_MAX = 7
NAME_MAX = 200
MESSAGE_MAX = 200
OTHER_REASON_MAX = 30
U_SPACE_CLASS_MAX = 100
AUTHORITY_TEXT_MAX = 200
REASONS_MAX = 9

AUTHORITY_FIELDS = (
    "name",
    "service",
    "contactName",
    "siteURL",
    "email",
    "phone",
    "purpose",
    "intervalBefore",
)
_AUTHORITY_LIMITED = frozenset({"name", "service", "contactName", "phone"})
APPLICABILITY_FIELDS = ("permanent", "startDateTime", "endDateTime", "schedule")
DAILY_FIELDS = ("day", "startTime", "endTime")
VOLUME_FIELDS = (
    "uomDimensions",
    "lowerLimit",
    "lowerVerticalReference",
    "upperLimit",
    "upperVerticalReference",
    "horizontalProjection",
)
# Published fields this system carries but does not interpret. They are kept
# as published and written back unchanged.
EXTRA_FIELDS = (
    "restrictionConditions",
    "region",
    "otherReasonInfo",
    "regulationExemption",
    "uSpaceClass",
    "extendedProperties",
    "title",
)
FEATURE_FIELDS = (
    "identifier",
    "country",
    "name",
    "type",
    "restriction",
    "reason",
    "message",
    "applicability",
    "zoneAuthority",
    "geometry",
    *EXTRA_FIELDS,
)
# The two published wrappers: InterUSS's ED269Schema and Luxembourg's file
# use `features`; the Swiss CIS sample uses `UASZoneList`.
_FEATURES_WRAPPER = frozenset({"title", "description", "features"})
_LIST_WRAPPER = frozenset({"formatVersion", "createdAt", "UASZoneList"})

_COUNTRY = re.compile(r"^[A-Z]{3}$")
_TIME = re.compile(
    r"^(?P<h>[01]\d|2[0-3]):(?P<m>[0-5]\d)"
    r"(?::(?P<s>[0-5]\d)(?:\.(?P<f>\d{1,6}))?)?"
    r"(?P<tz>Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)$"
)


# --- refusals ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Problem:
    """Why a field was refused. `field` is a path into the document, e.g.
    `features[3].geometry[0].upperLimit`."""

    field: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "reason": self.reason}


class Ed269Error(ValueError):
    """A document or zone that is refused, with every problem found."""

    def __init__(self, problems: Sequence[Problem], *, more: int = 0) -> None:
        self.problems = tuple(problems)
        self.more = more
        listed = "; ".join(f"{p.field}: {p.reason}" for p in self.problems)
        suffix = f"; and {more} more" if more else ""
        super().__init__(f"refused: {listed}{suffix}")


class _Problems:
    def __init__(self) -> None:
        self.found: list[Problem] = []
        self.more = 0

    def add(self, path: str, reason: str) -> None:
        if len(self.found) < MAX_PROBLEMS:
            self.found.append(Problem(path, reason))
        else:
            self.more += 1

    def __bool__(self) -> bool:
        return bool(self.found)

    def raise_if_any(self) -> None:
        if self.found:
            raise Ed269Error(self.found, more=self.more)


class _Refused(Enum):
    """A field that was refused, as distinct from one that was absent (None)."""

    REFUSED = 0


REFUSED: Final = _Refused.REFUSED
Refusable = Literal[_Refused.REFUSED]


# --- the model -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Polygon:
    # Exterior first, then holes; each closed, (lon_deg, lat_deg).
    rings: tuple[Ring, ...]


@dataclass(frozen=True, slots=True)
class Circle:
    center_lon_deg: float
    center_lat_deg: float
    # In the volume's `uomDimensions`, as published.
    radius: float


@dataclass(frozen=True, slots=True)
class Volume:
    uom: Uom
    # None: from the surface (lower) or unlimited (upper).
    lower_limit: float | None
    lower_reference: VerticalReference
    upper_limit: float | None
    upper_reference: VerticalReference
    projection: Polygon | Circle

    def to_m(self, value: float) -> float:
        return value * FEET_M if self.uom is Uom.FT else value

    @property
    def lower_m(self) -> float | None:
        return None if self.lower_limit is None else self.to_m(self.lower_limit)

    @property
    def upper_m(self) -> float | None:
        return None if self.upper_limit is None else self.to_m(self.upper_limit)

    @property
    def radius_m(self) -> float | None:
        if isinstance(self.projection, Circle):
            return self.to_m(self.projection.radius)
        return None


@dataclass(frozen=True)
class GeoZone:
    """One ED-269 `UASZoneVersion`, as published."""

    identifier: str
    country: str
    name: str | None
    type: str
    restriction: Restriction
    # None: the field was absent. () : an empty list was published.
    reason: tuple[Reason, ...] | None
    message: str | None
    # The published objects, validated; absent and null members left out.
    zone_authority: tuple[dict[str, str], ...]
    applicability: tuple[dict[str, Any], ...]
    volume: Volume
    # EXTRA_FIELDS present in the file, by their ED-269 names.
    extra: dict[str, Any] = field(default_factory=dict)

    def periods(self) -> tuple[Period, ...]:
        """The applicability, parsed for evaluation. Valid by construction:
        a GeoZone is only made from what `parse_zone` accepted."""
        return parse_applicability(self.applicability)


# --- when a zone applies -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DailyPeriod:
    # Python weekday numbers, Monday 0, in `start`'s offset.
    weekdays: frozenset[int]
    start: time
    end: time
    offset: tzinfo

    def contains(self, at: datetime) -> bool:
        """Whether `at` is inside the period. A period whose end is before
        its start runs past midnight: it belongs to the day it starts on,
        so 22:00 to 06:00 on MON is Monday 22:00 to Tuesday 06:00. Both
        ends are included, as `23:59:59` ends in the published files."""
        local = at.astimezone(self.offset)
        clock = local.time().replace(tzinfo=None)
        if self.start <= self.end:
            return local.weekday() in self.weekdays and self.start <= clock <= self.end
        if local.weekday() in self.weekdays and clock >= self.start:
            return True
        previous_day = (local.weekday() - 1) % 7
        return previous_day in self.weekdays and clock <= self.end


@dataclass(frozen=True, slots=True)
class Period:
    permanent: bool
    start: datetime | None
    end: datetime | None
    schedule: tuple[DailyPeriod, ...] | None

    def contains(self, at: datetime) -> bool:
        if self.permanent:
            return True
        if self.start is not None and at < self.start:
            return False
        if self.end is not None and at > self.end:
            return False
        if self.schedule is None:
            return True
        return any(daily.contains(at) for daily in self.schedule)


def applies(periods: Iterable[Period], at: datetime) -> bool:
    """Whether a zone with these periods applies at `at`: when any does."""
    if at.tzinfo is None:
        raise ValueError("a zone's applicability is evaluated at an aware time")
    return any(period.contains(at) for period in periods)


def parse_applicability(raw: Sequence[Mapping[str, Any]]) -> tuple[Period, ...]:
    """Periods from stored or published applicability. Raises Ed269Error."""
    problems = _Problems()
    periods = _applicability(list(raw), "applicability", problems)
    problems.raise_if_any()
    assert periods is not None
    return periods[1]


# --- reading -------------------------------------------------------------------------


@dataclass(frozen=True)
class Ed269Document:
    zones: tuple[GeoZone, ...]
    # From the `features` wrapper; None when absent or for `UASZoneList`.
    title: str | None = None
    description: str | None = None


def parse(data: bytes) -> Ed269Document:
    """A whole ED-269 document. Raises Ed269Error naming every problem; a
    document is accepted whole or not at all, since half of an authority's
    zones looks complete and is not."""
    problems = _Problems()
    try:
        # utf-8-sig: Luxembourg's live file starts with a byte-order mark.
        document = json.loads(data.decode("utf-8-sig"))
    except UnicodeDecodeError as error:
        raise Ed269Error([Problem("$", f"not UTF-8: {error.reason}")]) from error
    except json.JSONDecodeError as error:
        raise Ed269Error(
            [Problem("$", f"not JSON: {error.msg} at line {error.lineno}")]
        ) from error
    if not isinstance(document, dict):
        raise Ed269Error([Problem("$", "not a JSON object")])

    if "features" in document:
        allowed, key = _FEATURES_WRAPPER, "features"
    elif "UASZoneList" in document:
        allowed, key = _LIST_WRAPPER, "UASZoneList"
    else:
        raise Ed269Error(
            [Problem("features", "missing: an ED-269 document lists its zones here")]
        )
    for name in document:
        if name not in allowed:
            problems.add(name, "unknown field; not part of an ED-269 document")
    title = _optional_text(document, "title", "title", None, problems)
    description = _optional_text(document, "description", "description", None, problems)
    raw_zones = document[key]
    if not isinstance(raw_zones, list):
        problems.add(key, "must be a list of zones")
        raw_zones = []

    zones: list[GeoZone] = []
    first_seen: dict[str, int] = {}
    for index, raw in enumerate(raw_zones):
        path = f"{key}[{index}]"
        zone = _zone(raw, path, problems)
        if zone is None:
            continue
        if zone.identifier in first_seen:
            problems.add(
                f"{path}.identifier",
                f"{zone.identifier!r} is also the identifier of "
                f"{key}[{first_seen[zone.identifier]}]",
            )
            continue
        first_seen[zone.identifier] = index
        zones.append(zone)
    problems.raise_if_any()
    return Ed269Document(
        zones=tuple(zones),
        title=title if key == "features" else None,
        description=description if key == "features" else None,
    )


def parse_zone(raw: Any, path: str = "zone") -> GeoZone:
    """One `UASZoneVersion`. Raises Ed269Error naming every problem."""
    problems = _Problems()
    zone = _zone(raw, path, problems)
    problems.raise_if_any()
    assert zone is not None
    return zone


def _zone(raw: Any, path: str, problems: _Problems) -> GeoZone | None:
    if not isinstance(raw, dict):
        problems.add(path, "a zone must be an object")
        return None
    before = len(problems.found) + problems.more
    for name in raw:
        if name not in FEATURE_FIELDS:
            problems.add(f"{path}.{name}", "unknown field; not part of ED-269")

    identifier = _required_text(raw, "identifier", path, IDENTIFIER_MAX, problems)
    if identifier is not None and identifier != identifier.strip():
        problems.add(f"{path}.identifier", "has leading or trailing spaces")
    country = _required_text(raw, "country", path, 3, problems)
    if country is not None and not _COUNTRY.match(country):
        problems.add(
            f"{path}.country", f"{country!r} is not an ISO 3166-1 alpha-3 code"
        )
    name = _optional_text(raw, "name", f"{path}.name", NAME_MAX, problems)
    zone_type = _required_text(raw, "type", path, None, problems)
    restriction = _restriction(raw.get("restriction"), f"{path}.restriction", problems)
    reason = _reasons(raw.get("reason"), f"{path}.reason", problems)
    message = _optional_text(raw, "message", f"{path}.message", MESSAGE_MAX, problems)
    authorities = _authorities(raw.get("zoneAuthority"), path, problems)
    applicability = _applicability(
        raw.get("applicability"), f"{path}.applicability", problems
    )
    volume = _geometry(raw.get("geometry"), f"{path}.geometry", problems)
    extra = _extra(raw, path, problems)

    if len(problems.found) + problems.more > before:
        return None
    assert identifier is not None and country is not None and zone_type is not None
    assert restriction is not None and authorities is not None
    assert applicability is not None and volume is not None
    return GeoZone(
        identifier=identifier,
        country=country,
        name=name,
        type=zone_type,
        restriction=restriction,
        reason=reason,
        message=message,
        zone_authority=authorities,
        applicability=applicability[0],
        volume=volume,
        extra=extra,
    )


def _present(raw: Mapping[str, Any], key: str) -> bool:
    return key in raw and raw[key] is not None


def _required_text(
    raw: Mapping[str, Any],
    key: str,
    path: str,
    max_length: int | None,
    problems: _Problems,
) -> str | None:
    where = f"{path}.{key}"
    if not _present(raw, key):
        problems.add(where, "missing: required")
        return None
    return _text(raw[key], where, max_length, problems, allow_empty=False)


def _optional_text(
    raw: Mapping[str, Any],
    key: str,
    where: str,
    max_length: int | None,
    problems: _Problems,
) -> str | None:
    if not _present(raw, key):
        return None
    return _text(raw[key], where, max_length, problems, allow_empty=True)


def _text(
    value: Any,
    where: str,
    max_length: int | None,
    problems: _Problems,
    *,
    allow_empty: bool,
) -> str | None:
    if not isinstance(value, str):
        problems.add(where, f"must be a string, not {_kind(value)}")
        return None
    if not allow_empty and not value.strip():
        problems.add(where, "must not be empty")
        return None
    if max_length is not None and len(value) > max_length:
        problems.add(where, f"is {len(value)} characters; at most {max_length}")
        return None
    return value


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    return "an object"


def _enum[E: StrEnum](
    value: Any, kind: type[E], where: str, problems: _Problems
) -> E | None:
    allowed = ", ".join(member.value for member in kind)
    if not isinstance(value, str):
        problems.add(where, f"must be one of {allowed}, not {_kind(value)}")
        return None
    try:
        return kind(value)
    except ValueError:
        problems.add(where, f"{value!r} is not one of {allowed}")
        return None


def _restriction(value: Any, where: str, problems: _Problems) -> Restriction | None:
    if value is None:
        problems.add(where, "missing: required")
        return None
    if value == "REQ_AUTHORIZATION":
        problems.add(
            where,
            "'REQ_AUTHORIZATION' is not an ED-269 value; ED-269 spells it "
            "REQ_AUTHORISATION",
        )
        return None
    return _enum(value, Restriction, where, problems)


def _reasons(value: Any, where: str, problems: _Problems) -> tuple[Reason, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        problems.add(where, f"must be a list, not {_kind(value)}")
        return None
    if len(value) > REASONS_MAX:
        problems.add(where, f"has {len(value)} reasons; at most {REASONS_MAX}")
        return None
    reasons: list[Reason] = []
    for index, item in enumerate(value):
        reason = _enum(item, Reason, f"{where}[{index}]", problems)
        if reason is None:
            continue
        if reason in reasons:
            problems.add(f"{where}[{index}]", f"{reason.value} is listed twice")
            continue
        reasons.append(reason)
    return tuple(reasons)


def _authorities(
    value: Any, path: str, problems: _Problems
) -> tuple[dict[str, str], ...] | None:
    where = f"{path}.zoneAuthority"
    if value is None:
        problems.add(where, "missing: required (a list, which may be empty)")
        return None
    if not isinstance(value, list):
        problems.add(where, f"must be a list, not {_kind(value)}")
        return None
    authorities: list[dict[str, str]] = []
    for index, raw in enumerate(value):
        here = f"{where}[{index}]"
        if not isinstance(raw, dict):
            problems.add(here, "an authority must be an object")
            continue
        authority: dict[str, str] = {}
        for name, item in raw.items():
            if name not in AUTHORITY_FIELDS:
                problems.add(f"{here}.{name}", "unknown field; not part of ED-269")
                continue
            if item is None:
                continue
            if name == "purpose":
                purpose = _enum(item, Purpose, f"{here}.purpose", problems)
                if purpose is not None:
                    authority[name] = purpose.value
                continue
            limit = AUTHORITY_TEXT_MAX if name in _AUTHORITY_LIMITED else None
            text = _text(item, f"{here}.{name}", limit, problems, allow_empty=True)
            if text is not None:
                authority[name] = text
        authorities.append(authority)
    return tuple(authorities)


# --- applicability ------------------------------------------------------------------


def _applicability(
    value: Any, where: str, problems: _Problems
) -> tuple[tuple[dict[str, Any], ...], tuple[Period, ...]] | None:
    """(the published periods, cleaned of nulls; the parsed periods)."""
    if value is None:
        problems.add(where, "missing: required, at least one period")
        return None
    if not isinstance(value, list) or not value:
        problems.add(where, "must be a list of at least one period")
        return None
    published: list[dict[str, Any]] = []
    periods: list[Period] = []
    for index, raw in enumerate(value):
        here = f"{where}[{index}]"
        parsed = _period(raw, here, problems)
        if parsed is not None:
            published.append(parsed[0])
            periods.append(parsed[1])
    if len(periods) != len(value):
        return None
    return tuple(published), tuple(periods)


def _period(
    raw: Any, where: str, problems: _Problems
) -> tuple[dict[str, Any], Period] | None:
    if not isinstance(raw, dict):
        problems.add(where, "a period must be an object")
        return None
    ok = True
    for name in raw:
        if name not in APPLICABILITY_FIELDS:
            problems.add(f"{where}.{name}", "unknown field; not part of ED-269")
            ok = False
    permanent = _enum(raw.get("permanent"), YesNo, f"{where}.permanent", problems)
    start = _instant(raw.get("startDateTime"), f"{where}.startDateTime", problems)
    end = _instant(raw.get("endDateTime"), f"{where}.endDateTime", problems)
    schedule = _schedule(raw.get("schedule"), f"{where}.schedule", problems)
    if permanent is None or start is REFUSED or end is REFUSED or schedule is REFUSED:
        return None
    if permanent is YesNo.YES:
        for name in ("startDateTime", "endDateTime", "schedule"):
            if _present(raw, name):
                problems.add(
                    f"{where}.{name}",
                    "a permanent period (permanent YES) applies at all times "
                    "and has no " + name,
                )
                ok = False
    elif start is None and end is None and schedule is None:
        problems.add(
            where,
            "permanent NO needs a startDateTime, an endDateTime or a schedule "
            "to say when it applies",
        )
        ok = False
    if start is not None and end is not None and not start < end:
        problems.add(f"{where}.endDateTime", "is not after startDateTime")
        ok = False
    if not ok:
        return None
    published = {
        name: raw[name] for name in APPLICABILITY_FIELDS if _present(raw, name)
    }
    return published, Period(
        permanent=permanent is YesNo.YES,
        start=start,
        end=end,
        schedule=schedule,
    )


def _instant(
    value: Any, where: str, problems: _Problems
) -> datetime | Refusable | None:
    """A date-time; None when absent; REFUSED when refused."""
    if value is None:
        return None
    if not isinstance(value, str):
        problems.add(where, f"must be an ISO 8601 date-time, not {_kind(value)}")
        return REFUSED
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        problems.add(where, f"{value!r} is not an ISO 8601 date-time")
        return REFUSED
    if moment.tzinfo is None:
        problems.add(where, f"{value!r} has no offset; give Z or +hh:mm")
        return REFUSED
    return moment.astimezone(UTC)


def _schedule(
    value: Any, where: str, problems: _Problems
) -> tuple[DailyPeriod, ...] | Refusable | None:
    """Weekly periods; None when absent; REFUSED when refused."""
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        problems.add(where, "must be a list of at least one daily period")
        return REFUSED
    periods: list[DailyPeriod] = []
    for index, raw in enumerate(value):
        here = f"{where}[{index}]"
        daily = _daily(raw, here, problems)
        if daily is not None:
            periods.append(daily)
    return tuple(periods) if len(periods) == len(value) else REFUSED


def _daily(raw: Any, where: str, problems: _Problems) -> DailyPeriod | None:
    if not isinstance(raw, dict):
        problems.add(where, "a daily period must be an object")
        return None
    ok = True
    for name in raw:
        if name not in DAILY_FIELDS:
            problems.add(f"{where}.{name}", "unknown field; not part of ED-269")
            ok = False
    weekdays = _days(raw.get("day"), f"{where}.day", problems)
    start = _clock(raw.get("startTime"), f"{where}.startTime", problems)
    end = _clock(raw.get("endTime"), f"{where}.endTime", problems)
    if not ok or weekdays is None or start is None or end is None:
        return None
    if start[1] != end[1]:
        problems.add(
            f"{where}.endTime",
            "has a different offset from startTime; a daily period needs one",
        )
        return None
    if start[0] == end[0]:
        problems.add(f"{where}.endTime", "is the same as startTime")
        return None
    return DailyPeriod(weekdays=weekdays, start=start[0], end=end[0], offset=start[1])


def _days(value: Any, where: str, problems: _Problems) -> frozenset[int] | None:
    allowed = ", ".join((*DAYS, ANY_DAY))
    if not isinstance(value, list) or not 1 <= len(value) <= 7:
        problems.add(where, f"must be a list of 1 to 7 of {allowed}")
        return None
    weekdays: set[int] = set()
    for index, day in enumerate(value):
        if day == ANY_DAY:
            weekdays.update(range(7))
        elif isinstance(day, str) and day in DAYS:
            if DAYS.index(day) in weekdays:
                problems.add(f"{where}[{index}]", f"{day} is listed twice")
                return None
            weekdays.add(DAYS.index(day))
        else:
            problems.add(f"{where}[{index}]", f"{day!r} is not one of {allowed}")
            return None
    return frozenset(weekdays)


def _clock(value: Any, where: str, problems: _Problems) -> tuple[time, tzinfo] | None:
    match = _TIME.match(value) if isinstance(value, str) else None
    if match is None:
        problems.add(
            where,
            f"{value!r} is not a time of day with an offset, "
            "e.g. 17:00:00.00Z or 08:30+04:00",
        )
        return None
    fraction = (match["f"] or "0").ljust(6, "0")
    clock = time(int(match["h"]), int(match["m"]), int(match["s"] or 0), int(fraction))
    return clock, _offset(match["tz"])


def _offset(text: str) -> tzinfo:
    if text == "Z":
        return UTC
    sign = 1 if text[0] == "+" else -1
    digits = text[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))


# --- geometry -----------------------------------------------------------------------


def _geometry(value: Any, where: str, problems: _Problems) -> Volume | None:
    if value is None:
        problems.add(where, "missing: required, one airspace volume")
        return None
    if not isinstance(value, list) or not value:
        problems.add(where, "must be a list of one airspace volume")
        return None
    if len(value) > 1:
        problems.add(
            where,
            f"has {len(value)} volumes; one volume per zone is supported "
            "(publish each volume as its own zone)",
        )
        return None
    return _volume(value[0], f"{where}[0]", problems)


def _volume(raw: Any, where: str, problems: _Problems) -> Volume | None:
    if not isinstance(raw, dict):
        problems.add(where, "a volume must be an object")
        return None
    before = len(problems.found) + problems.more
    for name in raw:
        if name not in VOLUME_FIELDS:
            problems.add(f"{where}.{name}", "unknown field; not part of ED-269")
    uom = _enum(raw.get("uomDimensions"), Uom, f"{where}.uomDimensions", problems)
    lower = _limit(raw.get("lowerLimit"), f"{where}.lowerLimit", problems)
    upper = _limit(raw.get("upperLimit"), f"{where}.upperLimit", problems)
    lower_ref = _enum(
        raw.get("lowerVerticalReference"),
        VerticalReference,
        f"{where}.lowerVerticalReference",
        problems,
    )
    upper_ref = _enum(
        raw.get("upperVerticalReference"),
        VerticalReference,
        f"{where}.upperVerticalReference",
        problems,
    )
    projection = _projection(
        raw.get("horizontalProjection"), f"{where}.horizontalProjection", problems
    )
    if len(problems.found) + problems.more > before:
        return None
    assert uom is not None and lower_ref is not None and upper_ref is not None
    assert projection is not None
    assert lower is not REFUSED and upper is not REFUSED
    if (
        lower is not None
        and upper is not None
        and lower_ref is upper_ref
        and not lower < upper
    ):
        problems.add(f"{where}.upperLimit", "is not above lowerLimit")
        return None
    return Volume(
        uom=uom,
        lower_limit=lower,
        lower_reference=lower_ref,
        upper_limit=upper,
        upper_reference=upper_ref,
        projection=projection,
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _limit(value: Any, where: str, problems: _Problems) -> float | Refusable | None:
    """A vertical limit; None when absent; REFUSED when refused."""
    if value is None:
        return None
    number = _number(value)
    if number is None:
        problems.add(where, f"must be a number, not {_kind(value)}")
        return REFUSED
    return number


def _projection(value: Any, where: str, problems: _Problems) -> Polygon | Circle | None:
    if not isinstance(value, dict):
        problems.add(where, "missing: required, a Polygon or a Circle")
        return None
    kind = value.get("type")
    if kind == "Polygon":
        allowed: tuple[str, ...] = ("type", "coordinates")
    elif kind == "Circle":
        allowed = ("type", "center", "radius")
    else:
        problems.add(f"{where}.type", f"{kind!r} is not Polygon or Circle")
        return None
    unknown = [name for name in value if name not in allowed]
    for name in unknown:
        problems.add(f"{where}.{name}", f"not a field of a {kind}")
    if unknown:
        return None
    if kind == "Circle":
        center = _position(value.get("center"), f"{where}.center", problems)
        radius = _number(value.get("radius"))
        if radius is None or radius <= 0:
            problems.add(f"{where}.radius", "must be a number above 0")
            return None
        if center is None:
            return None
        return Circle(center_lon_deg=center[0], center_lat_deg=center[1], radius=radius)
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        problems.add(f"{where}.coordinates", "must be a list of rings")
        return None
    rings: list[Ring] = []
    for index, raw in enumerate(coordinates):
        ring = _ring(raw, f"{where}.coordinates[{index}]", problems)
        if ring is not None:
            rings.append(ring)
    if len(rings) != len(coordinates):
        return None
    return Polygon(rings=tuple(rings))


def _position(value: Any, where: str, problems: _Problems) -> Position | None:
    if not isinstance(value, list) or len(value) != 2:
        problems.add(where, "must be [longitude, latitude]")
        return None
    lon, lat = _number(value[0]), _number(value[1])
    if lon is None or lat is None:
        problems.add(where, "must be two numbers, [longitude, latitude]")
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        problems.add(where, f"{value} is outside longitude and latitude ranges")
        return None
    return lon, lat


def _ring(value: Any, where: str, problems: _Problems) -> Ring | None:
    if not isinstance(value, list):
        problems.add(where, "a ring must be a list of positions")
        return None
    points: list[Position] = []
    for index, raw in enumerate(value):
        point = _position(raw, f"{where}[{index}]", problems)
        if point is None:
            return None
        points.append(point)
    if len(points) < 4:
        problems.add(where, "a ring needs at least four positions, closed")
        return None
    if points[0] != points[-1]:
        problems.add(where, "is not closed: the last position must repeat the first")
        return None
    if len(set(points)) < 3:
        problems.add(where, "needs at least three distinct positions")
        return None
    return tuple(points)


def _extra(raw: Mapping[str, Any], path: str, problems: _Problems) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for name in EXTRA_FIELDS:
        if not _present(raw, name):
            continue
        value = raw[name]
        where = f"{path}.{name}"
        if name == "restrictionConditions":
            if isinstance(value, str) or (
                isinstance(value, list) and all(isinstance(v, str) for v in value)
            ):
                extra[name] = value
            else:
                problems.add(where, "must be a string or a list of strings")
        elif name == "region":
            if isinstance(value, int) and not isinstance(value, bool):
                extra[name] = value
            else:
                problems.add(where, f"must be an integer, not {_kind(value)}")
        elif name == "regulationExemption":
            exemption = _enum(value, YesNo, where, problems)
            if exemption is not None:
                extra[name] = exemption.value
        elif name in ("otherReasonInfo", "uSpaceClass", "title"):
            limit = {
                "otherReasonInfo": OTHER_REASON_MAX,
                "uSpaceClass": U_SPACE_CLASS_MAX,
            }
            text = _text(value, where, limit.get(name), problems, allow_empty=True)
            if text is not None:
                extra[name] = text
        else:
            # extendedProperties: any JSON, carried as published.
            extra[name] = value
    return extra


# --- writing ---------------------------------------------------------------------------


def _json_number(value: float) -> int | float:
    """A whole number as an integer, as published files write limits."""
    return int(value) if value.is_integer() else value


def feature(zone: GeoZone) -> dict[str, Any]:
    """The zone as an ED-269 `UASZoneVersion`."""
    out: dict[str, Any] = {
        "identifier": zone.identifier,
        "country": zone.country,
    }
    if zone.name is not None:
        out["name"] = zone.name
    out["type"] = zone.type
    out["restriction"] = zone.restriction.value
    if zone.reason is not None:
        out["reason"] = [reason.value for reason in zone.reason]
    if zone.message is not None:
        out["message"] = zone.message
    out["applicability"] = [dict(period) for period in zone.applicability]
    out["zoneAuthority"] = [dict(authority) for authority in zone.zone_authority]
    out["geometry"] = [_volume_json(zone.volume)]
    for name in EXTRA_FIELDS:
        if name in zone.extra:
            out[name] = zone.extra[name]
    return out


def _volume_json(volume: Volume) -> dict[str, Any]:
    out: dict[str, Any] = {"uomDimensions": volume.uom.value}
    if volume.lower_limit is not None:
        out["lowerLimit"] = _json_number(volume.lower_limit)
    out["lowerVerticalReference"] = volume.lower_reference.value
    if volume.upper_limit is not None:
        out["upperLimit"] = _json_number(volume.upper_limit)
    out["upperVerticalReference"] = volume.upper_reference.value
    projection = volume.projection
    if isinstance(projection, Circle):
        out["horizontalProjection"] = {
            "type": "Circle",
            "center": [projection.center_lon_deg, projection.center_lat_deg],
            "radius": _json_number(projection.radius),
        }
    else:
        out["horizontalProjection"] = {
            "type": "Polygon",
            "coordinates": [
                [list(point) for point in ring] for ring in projection.rings
            ],
        }
    return out


def document(
    zones: Iterable[GeoZone],
    *,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """An ED-269 document in the `features` wrapper (InterUSS's ED269Schema)."""
    out: dict[str, Any] = {}
    if title is not None:
        out["title"] = title
    if description is not None:
        out["description"] = description
    out["features"] = [feature(zone) for zone in zones]
    return out
