"""ED-269 zones in the monitor: restriction to severity, each vertical
reference judged in its own datum, and applicability at the placed time. U-03.

Every "nothing raised" here is paired with the same setup made to raise
(CLAUDE.md: test presence, not only absence).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from airspace.monitor import AirspaceMonitor, Severity, zone_key
from airspace.tests.test_monitor import LAT0, LON0, POLICY, message
from airspace.tests.zone_helpers import square, zone
from airspace.zones import Zone
from common.terrain import Elevation, TerrainFileError

A = UUID(int=1)
GROUND_M = 500.0
UNDULATION_M = 15.0


class Flat:
    """Ground at GROUND_M everywhere, or unknown everywhere."""

    def __init__(self, known: bool = True) -> None:
        self.known = known

    def elevation(self, lat_deg: float, lon_deg: float) -> Elevation | None:
        if not self.known:
            return None
        return Elevation(elevation_m=GROUND_M, dataset="COP-DEM GLO-30", spacing_m=30)


class Unreadable:
    def elevation(self, lat_deg: float, lon_deg: float) -> Elevation | None:
        raise TerrainFileError("tile unreadable")


class Geoid:
    def undulation_m(self, lat_deg: float, lon_deg: float) -> float:
        return UNDULATION_M


def here(**kwargs: Any) -> Zone:
    """A zone around the aircraft's position in `message`."""
    kwargs.setdefault("coordinates", square(LAT0, LON0))
    return zone(**kwargs)


def monitor(*zones: Zone, **kwargs: Any) -> AirspaceMonitor:
    return AirspaceMonitor(policy=POLICY, zones=list(zones), **kwargs)


def raised(
    m: AirspaceMonitor, alt_amsl_m: float, at_s: float = 0.0, **kw: Any
) -> list[Any]:
    return m.observe(
        message(A, 0, alt_amsl_m=alt_amsl_m, at_s=at_s), now_s=at_s, **kw
    ).raised


# --- restriction to severity -------------------------------------------------------


@pytest.mark.parametrize(
    ("restriction", "severity"),
    [
        ("PROHIBITED", Severity.CRITICAL),
        ("REQ_AUTHORISATION", Severity.WARNING),
        ("CONDITIONAL", Severity.WARNING),
    ],
)
def test_each_restriction_raises_its_severity(
    restriction: str, severity: Severity
) -> None:
    (alert,) = raised(monitor(here(restriction=restriction)), 550.0)
    assert alert.severity is severity
    assert alert.detail["restriction"] == restriction
    assert alert.detail["identifier"] == "T1"


def test_conditional_raises_info_when_policy_says_so() -> None:
    m = monitor(here(restriction="CONDITIONAL"), conditional_severity=Severity.INFO)
    (alert,) = raised(m, 550.0)
    assert alert.severity is Severity.INFO
    assert alert.as_dict()["severity"] == "info"


def test_no_restriction_raises_nothing_where_prohibited_would() -> None:
    assert raised(monitor(here(restriction="NO_RESTRICTION")), 550.0) == []
    assert len(raised(monitor(here(restriction="PROHIBITED")), 550.0)) == 1


class Authorised:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[tuple[UUID, str, datetime]] = []

    def authorised(self, drone_id: UUID, zone: Zone, at: datetime) -> bool:
        self.asked.append((drone_id, zone.identifier, at))
        return self.answer


def test_an_authorised_aircraft_is_not_alerted_in_a_req_authorisation_zone() -> None:
    yes, no = Authorised(True), Authorised(False)
    assert (
        raised(
            monitor(here(restriction="REQ_AUTHORISATION"), authorisations=yes), 550.0
        )
        == []
    )
    assert (
        len(
            raised(
                monitor(here(restriction="REQ_AUTHORISATION"), authorisations=no), 550.0
            )
        )
        == 1
    )
    assert yes.asked == [(A, "T1", datetime.fromtimestamp(0, tz=UTC))]


def test_an_authorisation_does_not_lift_a_prohibition() -> None:
    yes = Authorised(True)
    assert (
        len(raised(monitor(here(restriction="PROHIBITED"), authorisations=yes), 550.0))
        == 1
    )
    assert yes.asked == []


def test_a_circle_zone_alerts_inside_and_not_outside() -> None:
    circle = zone(circle=(LAT0, LON0, 200))
    assert len(raised(monitor(circle), 550.0)) == 1
    far = monitor(circle).observe(message(A, 250, alt_amsl_m=550.0), now_s=0.0)
    assert far.raised == []


# --- vertical references --------------------------------------------------------------


def test_amsl_limits_are_judged_on_the_amsl_altitude() -> None:
    band = here(lower=(500, "AMSL"), upper=(700, "AMSL"))
    assert raised(monitor(band), 450.0) == []
    assert len(raised(monitor(band), 600.0)) == 1
    assert raised(monitor(band), 750.0) == []


def test_agl_limits_are_judged_above_the_ground_not_above_sea_level() -> None:
    """Ground at 500 m: 120 m AGL is 620 m AMSL. An AMSL reading of the
    limit would put the aircraft at 600 m far above it."""
    ceiling = here(lower=(0, "AGL"), upper=(120, "AGL"))
    (alert,) = raised(monitor(ceiling, terrain=Flat()), 600.0)
    assert alert.detail["height_agl_m"] == 100.0
    assert alert.detail["upper_reference"] == "AGL"
    assert raised(monitor(ceiling, terrain=Flat()), 630.0) == []


def test_an_agl_floor_leaves_out_an_aircraft_below_it() -> None:
    floor = here(lower=(50, "AGL"))
    assert raised(monitor(floor, terrain=Flat()), 520.0) == []
    assert len(raised(monitor(floor, terrain=Flat()), 560.0)) == 1


def test_wgs84_limits_go_through_the_geoid() -> None:
    """600 m above the ellipsoid with N = 15 m is 585 m AMSL."""
    ceiling = here(upper=(600, "WGS84"))
    (alert,) = raised(monitor(ceiling, geoid=Geoid()), 580.0)
    assert alert.detail["alt_hae_m"] == 595.0
    assert raised(monitor(ceiling, geoid=Geoid()), 590.0) == []


def test_limits_in_feet_are_converted() -> None:
    feet = here(upper=(2000, "AMSL"), uom="FT")  # 609.6 m
    assert len(raised(monitor(feet), 600.0)) == 1
    assert raised(monitor(feet), 615.0) == []


# --- not evaluated, counted, and never cleared by silence ------------------------------


@pytest.mark.parametrize(
    ("limit", "kwargs"),
    [
        ((120, "AGL"), {}),
        ((120, "AGL"), {"terrain": Flat(known=False)}),
        ((120, "AGL"), {"terrain": Unreadable()}),
        ((600, "WGS84"), {}),
    ],
    ids=["no terrain", "ground unknown", "tile unreadable", "no geoid"],
)
def test_a_limit_without_its_data_is_not_evaluated_and_counted(
    limit: tuple[float, str], kwargs: dict[str, Any]
) -> None:
    m = monitor(here(upper=limit), **kwargs)
    assert raised(m, 550.0) == []
    assert m.zone_checks_not_evaluated == 1
    raised(m, 550.0, at_s=1.0)
    assert m.zone_checks_not_evaluated == 2


def test_the_height_the_caller_could_not_load_is_not_evaluated() -> None:
    m = monitor(here(upper=(120, "AGL")), terrain=Flat())
    assert raised(m, 550.0, height_available=False) == []
    assert m.zone_checks_not_evaluated == 1
    # The same message with the tile loaded alerts.
    assert len(raised(m, 550.0, at_s=1.0)) == 1


def test_an_unjudged_alert_is_neither_refreshed_nor_cleared() -> None:
    terrain = Flat()
    m = monitor(here(upper=(120, "AGL")), terrain=terrain, clear_after_s=3.0)
    assert len(raised(m, 550.0)) == 1
    key = zone_key(A, m.zones[0])

    terrain.known = False
    for at_s in (1.0, 2.0, 5.0, 9.0):
        change = m.observe(message(A, 0, alt_amsl_m=550.0, at_s=at_s), now_s=at_s)
        assert change.cleared == []
    assert key in {alert.key for alert in m.active}

    # Known again and above the ceiling: shown false, and false for longer
    # than the hysteresis since it was last seen true (at 0 s), so cleared.
    terrain.known = True
    cleared = m.observe(message(A, 0, alt_amsl_m=700.0, at_s=10.0), now_s=10.0).cleared
    assert [c.alert.key for c in cleared] == [key]


def test_a_limit_that_can_be_judged_decides_when_another_cannot() -> None:
    """Above an AMSL ceiling is outside, whatever an AGL floor says."""
    mixed = here(lower=(0, "AGL"), upper=(700, "AMSL"))
    m = monitor(mixed)  # no terrain
    assert raised(m, 750.0) == []
    assert m.zone_checks_not_evaluated == 0
    assert raised(m, 650.0, at_s=1.0) == []
    assert m.zone_checks_not_evaluated == 1


def test_outside_horizontally_is_judged_without_the_ground() -> None:
    m = monitor(here(upper=(120, "AGL")))
    m.observe(message(A, 5_000, alt_amsl_m=550.0), now_s=0.0)
    assert m.zone_checks_not_evaluated == 0


# --- applicability, at the placed time in UTC ----------------------------------------


def epoch(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def window(start: str, end: str) -> list[dict[str, Any]]:
    return [{"permanent": "NO", "startDateTime": start, "endDateTime": end}]


def test_a_zone_alerts_inside_its_window_and_not_outside() -> None:
    timed = here(applicability=window("2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z"))
    assert raised(monitor(timed), 550.0, at_s=epoch("2026-10-01T09:59:00Z")) == []
    assert len(raised(monitor(timed), 550.0, at_s=epoch("2026-10-01T10:30:00Z"))) == 1
    assert raised(monitor(timed), 550.0, at_s=epoch("2026-10-01T11:00:01Z")) == []


def test_applicability_is_judged_at_the_placed_time_not_the_arrival() -> None:
    """Captured inside the window, judged 5 s later after it ended: the
    aircraft was in the zone while it applied."""
    end_s = epoch("2026-10-01T11:00:00Z")
    timed = here(applicability=window("2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z"))
    m = monitor(timed)
    late = message(A, 0, alt_amsl_m=550.0, at_s=end_s - 1.0)
    assert len(m.observe(late, now_s=end_s + 4.0).raised) == 1
    early = message(A, 0, alt_amsl_m=550.0, at_s=end_s + 1.0)
    assert monitor(timed).observe(early, now_s=end_s - 4.0).raised == []


def test_a_zone_that_stops_applying_clears_its_alert() -> None:
    end_s = epoch("2026-10-01T11:00:00Z")
    m = monitor(
        here(applicability=window("2026-10-01T10:00:00Z", "2026-10-01T11:00:00Z")),
        clear_after_s=3.0,
    )
    assert len(raised(m, 550.0, at_s=end_s - 1.0)) == 1
    raised(m, 550.0, at_s=end_s + 1.0)
    cleared = m.observe(
        message(A, 0, alt_amsl_m=550.0, at_s=end_s + 5.0), now_s=end_s + 5.0
    ).cleared
    assert len(cleared) == 1


def test_a_weekly_night_zone_across_midnight_utc() -> None:
    """Friday 22:00Z to 02:00Z: alerting on Saturday 00:30Z, not on
    Saturday 03:00Z, nor on Friday 01:00Z (Thursday's night)."""
    night = here(
        restriction="CONDITIONAL",
        applicability=[
            {
                "permanent": "NO",
                "schedule": [
                    {
                        "day": ["FRI"],
                        "startTime": "22:00:00.00Z",
                        "endTime": "02:00:00.00Z",
                    }
                ],
            }
        ],
    )
    assert len(raised(monitor(night), 550.0, at_s=epoch("2026-10-10T00:30:00Z"))) == 1
    assert len(raised(monitor(night), 550.0, at_s=epoch("2026-10-09T23:59:59Z"))) == 1
    assert raised(monitor(night), 550.0, at_s=epoch("2026-10-10T03:00:00Z")) == []
    assert raised(monitor(night), 550.0, at_s=epoch("2026-10-09T01:00:00Z")) == []
