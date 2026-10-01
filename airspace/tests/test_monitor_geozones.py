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


# --- limits without their data -------------------------------------------------------
#
# A CONDITIONAL zone, or a limit above the ellipsoid without the geoid, is not
# evaluated: silent, counted, never cleared by silence. A PROHIBITED or
# REQ_AUTHORISATION zone whose only unjudged limit is above the ground warns
# instead (`limit_not_judged`): a false warning beats a missed critical.


@pytest.mark.parametrize(
    ("restriction", "limit", "kwargs"),
    [
        ("CONDITIONAL", (120, "AGL"), {}),
        ("CONDITIONAL", (120, "AGL"), {"terrain": Flat(known=False)}),
        ("CONDITIONAL", (120, "AGL"), {"terrain": Unreadable()}),
        ("PROHIBITED", (600, "WGS84"), {}),
    ],
    ids=["no terrain", "ground unknown", "tile unreadable", "no geoid"],
)
def test_a_limit_without_its_data_is_not_evaluated_and_counted(
    restriction: str, limit: tuple[float, str], kwargs: dict[str, Any]
) -> None:
    m = monitor(here(restriction=restriction, upper=limit), **kwargs)
    assert raised(m, 550.0) == []
    assert m.zone_checks_not_evaluated == 1
    raised(m, 550.0, at_s=1.0)
    assert m.zone_checks_not_evaluated == 2


@pytest.mark.parametrize("restriction", ["PROHIBITED", "REQ_AUTHORISATION"])
@pytest.mark.parametrize(
    "kwargs",
    [{}, {"terrain": Flat(known=False)}, {"terrain": Unreadable()}],
    ids=["no terrain", "ground unknown", "tile unreadable"],
)
def test_an_agl_ceiling_without_the_ground_warns_that_it_was_not_judged(
    restriction: str, kwargs: dict[str, Any]
) -> None:
    m = monitor(
        here(restriction=restriction, lower=(0, "AGL"), upper=(120, "AGL")), **kwargs
    )
    (alert,) = raised(m, 550.0)
    assert alert.severity is Severity.WARNING
    assert alert.detail["vertical_known"] is False
    assert alert.detail["limit_not_judged"] is True
    assert alert.detail["not_judged"] == ["AGL"]
    assert m.zone_limits_not_judged == 1
    assert m.zone_checks_not_evaluated == 0


def test_the_same_agl_zone_with_the_ground_is_judged_at_its_own_severity() -> None:
    """The presence pair: with the DEM, 50 m AGL is critical and nothing is
    flagged; 130 m AGL raises nothing."""
    zone_ = here(lower=(0, "AGL"), upper=(120, "AGL"))
    (alert,) = raised(monitor(zone_, terrain=Flat()), GROUND_M + 50)
    assert alert.severity is Severity.CRITICAL
    assert "limit_not_judged" not in alert.detail
    assert raised(monitor(zone_, terrain=Flat()), GROUND_M + 130) == []


def test_an_agl_floor_above_the_ground_also_needs_it() -> None:
    m = monitor(here(lower=(50, "AGL")))
    (alert,) = raised(m, 550.0)
    assert alert.detail["limit_not_judged"] is True
    assert alert.severity is Severity.WARNING
    assert raised(monitor(here(lower=(50, "AGL")), terrain=Flat()), GROUND_M + 20) == []


def test_an_agl_floor_at_the_ground_is_met_without_terrain() -> None:
    """A lower AGL limit at or below 0 is met by any airborne aircraft."""
    m = monitor(here(lower=(0, "AGL"), upper=(700, "AMSL")))  # no terrain
    (alert,) = raised(m, 650.0)
    assert alert.severity is Severity.CRITICAL
    assert "limit_not_judged" not in alert.detail
    assert m.zone_checks_not_evaluated == 0
    # The AMSL ceiling still decides.
    assert raised(monitor(here(lower=(0, "AGL"), upper=(700, "AMSL"))), 750.0) == []


def test_the_height_the_caller_could_not_load_is_not_evaluated() -> None:
    m = monitor(here(restriction="CONDITIONAL", upper=(120, "AGL")), terrain=Flat())
    assert raised(m, 550.0, height_available=False) == []
    assert m.zone_checks_not_evaluated == 1
    # The same message with the tile loaded alerts.
    assert len(raised(m, 550.0, at_s=1.0)) == 1


def test_an_unjudged_alert_is_neither_refreshed_nor_cleared() -> None:
    terrain = Flat()
    m = monitor(
        here(restriction="CONDITIONAL", upper=(120, "AGL")),
        terrain=terrain,
        clear_after_s=3.0,
    )
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


def test_a_critical_alert_that_loses_the_ground_drops_to_a_flagged_warning() -> None:
    """A severity change is raised again under its key (S-33's `_refresh`)."""
    terrain = Flat()
    m = monitor(here(upper=(120, "AGL")), terrain=terrain)
    (first,) = raised(m, 550.0)
    terrain.known = False
    (second,) = raised(m, 550.0, at_s=1.0)
    assert (first.severity, second.severity) == (Severity.CRITICAL, Severity.WARNING)
    assert first.key == second.key
    assert raised(m, 550.0, at_s=2.0) == []


def test_prohibited_zones_without_terrain_are_named() -> None:
    zones = [
        here(identifier="P1", upper=(120, "AGL")),
        here(identifier="P2", lower=(0, "AGL"), upper=(700, "AMSL")),
        here(identifier="C1", restriction="CONDITIONAL", upper=(120, "AGL")),
    ]
    assert monitor(*zones).prohibited_without_terrain() == ["P1"]
    assert monitor(*zones, terrain=Flat()).prohibited_without_terrain() == []


def test_outside_horizontally_is_judged_without_the_ground() -> None:
    m = monitor(here(upper=(120, "AGL")))
    m.observe(message(A, 5_000, alt_amsl_m=550.0), now_s=0.0)
    assert m.zone_checks_not_evaluated == 0
    assert m.zone_limits_not_judged == 0


# --- a pressure altitude against ED-269 limits (S-33) -----------------------------------


def pressure(alt_amsl_m: float, at_s: float = 0.0) -> dict[str, Any]:
    return {**message(A, 0, alt_amsl_m=alt_amsl_m, at_s=at_s), "alt_source": "pressure"}


@pytest.mark.parametrize(
    ("height_agl_m", "alerts", "within_band"),
    [(100.0, 1, True), (300.0, 1, False), (400.0, 0, None)],
)
def test_a_pressure_track_against_an_agl_zone_is_widened_by_the_margin(
    height_agl_m: float, alerts: int, within_band: bool | None
) -> None:
    """0-120 m AGL over ground at 500 m, margin 250 m: 100 m AGL is inside
    (critical), 300 m only inside the widened band (warning), 400 m out."""
    m = monitor(here(lower=(0, "AGL"), upper=(120, "AGL")), terrain=Flat())
    found = m.observe(pressure(GROUND_M + height_agl_m), now_s=0.0).raised
    assert len(found) == alerts
    if found:
        assert found[0].detail["vertical_known"] is False
        assert found[0].detail["within_band"] is within_band
        assert found[0].severity is (
            Severity.CRITICAL if within_band else Severity.WARNING
        )


def test_a_geodetic_track_300_m_above_the_agl_zone_raises_nothing() -> None:
    m = monitor(here(lower=(0, "AGL"), upper=(120, "AGL")), terrain=Flat())
    assert raised(m, GROUND_M + 300) == []


def test_a_pressure_track_in_a_wgs84_zone_goes_through_the_geoid_and_the_margin() -> (
    None
):
    m = monitor(here(upper=(600, "WGS84")), geoid=Geoid())
    (alert,) = m.observe(pressure(700.0), now_s=0.0).raised
    assert alert.severity is Severity.WARNING
    assert alert.detail["within_band"] is False


def test_a_pressure_track_and_an_unjudged_agl_ceiling_carry_both_flags() -> None:
    m = monitor(here(lower=(0, "AGL"), upper=(120, "AGL")))
    (alert,) = m.observe(pressure(550.0), now_s=0.0).raised
    assert alert.severity is Severity.WARNING
    assert alert.detail["limit_not_judged"] is True
    assert alert.detail["vertical_known"] is False


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
