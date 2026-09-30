"""Zones from an authority's file, as the monitor alerts on them. P5-18."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from airspace.monitor import AirspaceMonitor, AlertKind, Severity
from airspace.tests.ed269_files import LAT
from airspace.tests.test_height_limit import Slope
from airspace.tests.test_monitor import LAT0, POLICY, message
from airspace.tests.test_zones import agl_zone

A = UUID(int=1)
assert abs(LAT - LAT0) < 1e-9  # the test zone is centred on the test aircraft


def monitor(zone_changes: dict[str, object] | None = None, when: str = "2026-10-01T12:00:00") -> AirspaceMonitor:
    return AirspaceMonitor(
        policy=POLICY,
        zones=[agl_zone(**(zone_changes or {}))],
        terrain=Slope(base_m=500.0),
        wall=lambda: datetime.fromisoformat(when).replace(tzinfo=UTC),
    )


def test_inside_the_band_above_the_ground_raises_a_critical_alert_naming_the_zone() -> None:
    raised = monitor().observe(message(A, 0, alt_amsl_m=600.0), now_s=0.0).raised

    [alert] = raised
    assert (alert.kind, alert.severity) == (AlertKind.ZONE, Severity.CRITICAL)
    assert alert.detail["external_id"] == "TEST01"
    assert alert.detail["restriction"] == "PROHIBITED"
    assert alert.detail["message"] == "Test zone, not a real restriction"


def test_above_the_band_is_outside_it() -> None:
    """650 m AMSL over 500 m ground is 150 m up, above a 0-120 m AGL zone."""
    raised = monitor().observe(message(A, 0, alt_amsl_m=650.0), now_s=0.0).raised
    assert [a for a in raised if a.kind is AlertKind.ZONE] == []


def test_a_zone_outside_its_published_times_does_not_alert() -> None:
    window = {
        "applicability": [
            {
                "permanent": "NO",
                "startDateTime": "2026-10-01T10:00:00Z",
                "endDateTime": "2026-10-01T14:00:00Z",
            }
        ]
    }
    outside = monitor(window, when="2026-10-01T15:00:00")
    inside = monitor(window, when="2026-10-01T12:00:00")

    assert outside.observe(message(A, 0, alt_amsl_m=600.0), now_s=0.0).raised == []
    assert len(inside.observe(message(A, 0, alt_amsl_m=600.0), now_s=0.0).raised) == 1
