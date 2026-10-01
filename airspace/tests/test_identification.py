"""U-02 in the airspace monitor: identification and mismatch alerts.

An aircraft nobody can name inside a zone that needs a name raises an
`identification` alert beside the zone's own, which the service hands to
the incident seam (U-12). A registered serial broadcast with another
operator's number raises `identification_mismatch`. Each "nothing raised"
is paired with the same setup made to raise.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from airspace.monitor import (
    INCIDENT_STATUSES,
    AirspaceMonitor,
    AlertKind,
    ClearReason,
    Severity,
    identification_key,
    mismatch_key,
    zone_key,
)
from airspace.service import AirspaceService, CountedIncidentCandidates
from airspace.tests.test_monitor import LAT0, LON0, POLICY, message
from airspace.tests.test_service import Clock, RecordingAudit, RecordingBus
from airspace.tests.zone_helpers import square, zone
from airspace.zones import Zone
from common.sources import NETWORK_REMOTE_ID, Control, SourceControlState
from gateway.identification import INCIDENT_STATUSES as GATEWAY_INCIDENT_STATUSES

A = UUID(int=1)


def ident(status: str, *, mismatch: bool = False) -> dict[str, Any]:
    return {
        "status": status,
        "reason": "test",
        "serial": None if status == "unidentified" else "SN-1",
        "operator_reg": "GEOX",
        "mismatch": mismatch,
        "registered_operator_reg": "GEOY" if mismatch else None,
    }


def track(
    status: str | None, *, at_s: float = 0.0, mismatch: bool = False, north_m: float = 0
) -> dict[str, Any]:
    body = message(A, north_m, at_s=at_s)
    if status is not None:
        body["identification"] = ident(status, mismatch=mismatch)
    return body


def here(restriction: str = "PROHIBITED") -> Zone:
    return zone(restriction=restriction, coordinates=square(LAT0, LON0))


def kinds(alerts: list[Any]) -> list[str]:
    return sorted(alert.kind.value for alert in alerts)


def test_the_monitor_and_the_gateway_agree_on_which_statuses_open_incidents() -> None:
    assert {s.value for s in GATEWAY_INCIDENT_STATUSES} == INCIDENT_STATUSES


@pytest.mark.parametrize("status", ["unidentified", "unknown_operator"])
@pytest.mark.parametrize("restriction", ["PROHIBITED", "REQ_AUTHORISATION"])
def test_an_unnamed_aircraft_in_a_zone_raises_an_identification_alert(
    status: str, restriction: str
) -> None:
    z = here(restriction)
    m = AirspaceMonitor(policy=POLICY, zones=[z])

    raised = m.observe(track(status), now_s=0.0).raised

    assert kinds(raised) == ["identification", "zone"]
    by_kind = {alert.kind: alert for alert in raised}
    alert = by_kind[AlertKind.IDENTIFICATION]
    assert alert.key == identification_key(A, z)
    assert alert.severity is Severity.CRITICAL
    assert alert.detail["status"] == status
    assert alert.detail["identifier"] == "T1"
    assert alert.detail["restriction"] == restriction
    assert alert.detail["incident_candidate"] is True
    # The zone alert carries the identification too.
    assert by_kind[AlertKind.ZONE].detail["identification"]["status"] == status


@pytest.mark.parametrize("status", ["registered", "suspended", None])
def test_a_named_aircraft_in_the_same_zone_raises_only_the_zone_alert(
    status: str | None,
) -> None:
    m = AirspaceMonitor(policy=POLICY, zones=[here()])

    raised = m.observe(track(status), now_s=0.0).raised

    assert kinds(raised) == ["zone"]


def test_an_unnamed_aircraft_in_a_conditional_zone_raises_no_identification() -> None:
    m = AirspaceMonitor(policy=POLICY, zones=[here("CONDITIONAL")])
    assert kinds(m.observe(track("unidentified"), now_s=0.0).raised) == ["zone"]


def test_an_unnamed_aircraft_outside_every_zone_raises_nothing() -> None:
    m = AirspaceMonitor(policy=POLICY, zones=[here()])
    # 5 km north of the zone, which is about 1.1 km across.
    assert m.observe(track("unidentified", north_m=5000), now_s=0.0).raised == []


def test_the_identification_alert_severity_is_configuration() -> None:
    m = AirspaceMonitor(
        policy=POLICY, zones=[here()], identification_severity=Severity.WARNING
    )
    [alert] = [
        a
        for a in m.observe(track("unknown_operator"), now_s=0.0).raised
        if a.kind is AlertKind.IDENTIFICATION
    ]
    assert alert.severity is Severity.WARNING


def test_leaving_the_zone_clears_both_after_the_hysteresis() -> None:
    z = here()
    m = AirspaceMonitor(policy=POLICY, zones=[z], clear_after_s=3.0)
    m.observe(track("unidentified", at_s=0.0), now_s=0.0)

    m.observe(track("unidentified", at_s=1.0, north_m=5000), now_s=1.0)
    cleared = m.observe(track("unidentified", at_s=5.0, north_m=5000), now_s=5.0)

    assert sorted((c.alert.key, c.reason) for c in cleared.cleared) == sorted(
        [
            (identification_key(A, z), ClearReason.RESOLVED),
            (zone_key(A, z), ClearReason.RESOLVED),
        ]
    )


def test_becoming_identified_inside_the_zone_clears_only_the_identification() -> None:
    z = here()
    m = AirspaceMonitor(policy=POLICY, zones=[z], clear_after_s=3.0)
    m.observe(track("unidentified", at_s=0.0), now_s=0.0)

    m.observe(track("registered", at_s=1.0), now_s=1.0)
    change = m.observe(track("registered", at_s=5.0), now_s=5.0)

    assert [c.alert.key for c in change.cleared] == [identification_key(A, z)]
    assert [a.key for a in m.active] == [zone_key(A, z)]


# --- mismatch -------------------------------------------------------------------


def test_a_mismatch_raises_its_alert_anywhere() -> None:
    m = AirspaceMonitor(policy=POLICY)

    [alert] = m.observe(track("unknown_operator", mismatch=True), now_s=0.0).raised

    assert alert.kind is AlertKind.IDENTIFICATION_MISMATCH
    assert alert.key == mismatch_key(A)
    assert alert.severity is Severity.WARNING
    assert alert.detail["operator_reg"] == "GEOX"
    assert alert.detail["registered_operator_reg"] == "GEOY"


def test_no_mismatch_raises_nothing() -> None:
    m = AirspaceMonitor(policy=POLICY)
    assert m.observe(track("unknown_operator"), now_s=0.0).raised == []


def test_a_mismatch_that_ends_clears_and_one_that_stays_is_raised_once() -> None:
    m = AirspaceMonitor(policy=POLICY, clear_after_s=3.0)
    m.observe(track("unknown_operator", mismatch=True, at_s=0.0), now_s=0.0)
    assert (
        m.observe(track("unknown_operator", mismatch=True, at_s=1.0), now_s=1.0).raised
        == []
    )

    m.observe(track("registered", at_s=2.0), now_s=2.0)
    change = m.observe(track("registered", at_s=6.0), now_s=6.0)

    assert [(c.alert.key, c.reason) for c in change.cleared] == [
        (mismatch_key(A), ClearReason.RESOLVED)
    ]


def test_the_mismatch_severity_is_configuration() -> None:
    m = AirspaceMonitor(policy=POLICY, mismatch_severity=Severity.CRITICAL)
    [alert] = m.observe(track("suspended", mismatch=True), now_s=0.0).raised
    assert alert.severity is Severity.CRITICAL


# --- U-15: a network Remote ID provider switched off ------------------------------


def test_a_network_rid_aircraft_is_dropped_as_source_disabled() -> None:
    state = [SourceControlState()]

    def enabled(source_type: str, instance_id: str | None) -> bool:
        return state[0].enabled(source_type, instance_id)

    z = here()
    m = AirspaceMonitor(policy=POLICY, zones=[z], source_enabled=enabled)
    body = {
        **track("unidentified"),
        "source": NETWORK_REMOTE_ID,
        "station_id": "fake-ussp",
        "armed": None,
        "airborne": True,
    }
    assert kinds(m.observe(body, now_s=0.0).raised) == ["identification", "zone"]

    state[0] = SourceControlState(
        version=1,
        controls=(Control(NETWORK_REMOTE_ID, "fake-ussp", False, "test", "admin", ""),),
    )
    cleared = m.apply_sources(now_s=1.0).cleared

    assert sorted((c.alert.kind.value, c.reason) for c in cleared) == [
        ("identification", ClearReason.SOURCE_DISABLED),
        ("zone", ClearReason.SOURCE_DISABLED),
    ]
    assert m.tracked == 0


# --- the service: the incident seam ----------------------------------------------


async def test_identification_alerts_reach_the_incident_seam_and_others_do_not() -> (
    None
):
    bus, audit, clock = RecordingBus(), RecordingAudit(), Clock()
    incidents = CountedIncidentCandidates()
    svc = AirspaceService(
        monitor=AirspaceMonitor(policy=POLICY, zones=[here()], clear_after_s=3.0),
        bus=bus,
        audit=audit,
        clock=clock,
        incidents=incidents,
    )

    await svc.on_telemetry(json.dumps(track("unidentified")).encode())

    assert (incidents.raised, incidents.cleared) == (1, 0)
    published = sorted(body["kind"] for _, body in bus.sent)
    assert published == ["identification", "zone"]
    await svc.flush_audit()
    assert len(audit.rows) == 2
    assert svc.status()["incident_candidates_raised"] == 1

    # Out of the zone: both clear; only the identification reaches the seam.
    for clock.now_s in (1.0, 5.0):
        body = track("unidentified", at_s=clock.now_s, north_m=5000)
        await svc.on_telemetry(json.dumps(body).encode())
    assert (incidents.raised, incidents.cleared) == (1, 1)


async def test_without_an_incident_sink_identification_alerts_still_publish() -> None:
    bus = RecordingBus()
    svc = AirspaceService(
        monitor=AirspaceMonitor(policy=POLICY, zones=[here()]),
        bus=bus,
        clock=Clock(),
    )
    await svc.on_telemetry(json.dumps(track("unknown_operator")).encode())
    assert "identification" in [body["kind"] for _, body in bus.sent]
    assert "incident_candidates_raised" not in svc.status()
