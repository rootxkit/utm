"""Network identification: every track resolved against the registry. U-02.

The truth table of `gateway/identification.py`, row by row, each status
made to happen and each paired with its neighbour that does not.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from common.uas_identity import RegistrationStatus
from gateway import odid
from gateway.identification import (
    INCIDENT_STATUSES,
    IdentificationStatus,
    Reason,
    resolve,
    resolve_bound,
    resolve_remote_id,
    serial_conflict,
)
from gateway.registry_projection import (
    OperatorFacts,
    RegistrySnapshot,
    UasFacts,
)
from gateway.remote_id import RemoteIdTracker
from gateway.remote_id_ingest import RemoteIdIngest
from gateway.remote_id_match import FleetSerials, Registered
from gateway.tests.rid_frames import LAT, LON, NOW, FlatGeoid, basic, location, pack

ACTIVE_OP = UUID(int=1)
SUSPENDED_OP = UUID(int=2)
REVOKED_OP = UUID(int=3)
GHOST_OP = UUID(int=4)  # owns an aircraft, but is not in the projection

REGISTERED = UUID(int=10)
SUSPENDED_UAS = UUID(int=11)
REVOKED_UAS = UUID(int=12)
OF_SUSPENDED_OP = UUID(int=13)
OF_REVOKED_OP = UUID(int=14)
FLEET = UUID(int=15)
OF_GHOST = UUID(int=16)

R = RegistrationStatus


def snapshot() -> RegistrySnapshot:
    return RegistrySnapshot(
        uas=(
            UasFacts(REGISTERED, "uas-a", "1581F5FKD229400A", R.ACTIVE, ACTIVE_OP),
            UasFacts(
                SUSPENDED_UAS, "uas-b", "1581F5FKD229400B", R.SUSPENDED, ACTIVE_OP
            ),
            UasFacts(REVOKED_UAS, "uas-c", "1581F5FKD229400C", R.REVOKED, ACTIVE_OP),
            UasFacts(OF_SUSPENDED_OP, "uas-d", "SN-D", R.ACTIVE, SUSPENDED_OP),
            UasFacts(OF_REVOKED_OP, "uas-e", "SN-E", R.ACTIVE, REVOKED_OP),
            UasFacts(FLEET, "hexa-01", "SN-FLEET", R.ACTIVE, None),
            UasFacts(OF_GHOST, "uas-g", "SN-G", R.ACTIVE, GHOST_OP),
        ),
        operators=(
            OperatorFacts(ACTIVE_OP, "GEOabcd1234efgh", R.ACTIVE),
            OperatorFacts(SUSPENDED_OP, "GEOSUSP00000001", R.SUSPENDED),
            OperatorFacts(REVOKED_OP, "GEOREVK00000001", R.REVOKED),
        ),
    )


S = IdentificationStatus

# (serial, operator ID broadcast) -> (status, reason, mismatch)
TABLE = [
    # registered: serial known and active, owner active, operator ID matches
    (("1581F5FKD229400A", "GEOabcd1234efgh"), (S.REGISTERED, Reason.MATCHED, False)),
    # ... case-insensitively (U-01), and around stray spaces
    (("1581F5FKD229400A", " geoABCD1234EFGH "), (S.REGISTERED, Reason.MATCHED, False)),
    # suspended: the UAS, or its operator, suspended or revoked
    (
        ("1581F5FKD229400B", "GEOabcd1234efgh"),
        (S.SUSPENDED, Reason.UAS_SUSPENDED, False),
    ),
    (("1581F5FKD229400C", "GEOabcd1234efgh"), (S.SUSPENDED, Reason.UAS_REVOKED, False)),
    (("SN-D", "GEOSUSP00000001"), (S.SUSPENDED, Reason.OPERATOR_SUSPENDED, False)),
    (("SN-E", "GEOREVK00000001"), (S.SUSPENDED, Reason.OPERATOR_REVOKED, False)),
    # ... whatever operator it claims; the mismatch is still flagged
    (
        ("1581F5FKD229400B", "GEOOTHER0000001"),
        (S.SUSPENDED, Reason.UAS_SUSPENDED, True),
    ),
    # unknown operator: absent, not in the registry, or not the owner
    (("1581F5FKD229400A", None), (S.UNKNOWN_OPERATOR, Reason.OPERATOR_ABSENT, False)),
    (("1581F5FKD229400A", "  "), (S.UNKNOWN_OPERATOR, Reason.OPERATOR_ABSENT, False)),
    (
        ("1581F5FKD229400A", "GEONOTREGISTERED"),
        (S.UNKNOWN_OPERATOR, Reason.OPERATOR_MISMATCH, True),
    ),
    (
        ("1581F5FKD229400A", "GEOSUSP00000001"),
        (S.UNKNOWN_OPERATOR, Reason.OPERATOR_MISMATCH, True),
    ),
    # ... or the serial is unknown, the operator known (even active)
    (
        ("SN-NOBODY", "GEOabcd1234efgh"),
        (S.UNKNOWN_OPERATOR, Reason.SERIAL_UNKNOWN, False),
    ),
    (("SN-NOBODY", None), (S.UNKNOWN_OPERATOR, Reason.SERIAL_UNKNOWN, False)),
    # ... or the owner is not in the projection
    (("SN-G", "GEOabcd1234efgh"), (S.UNKNOWN_OPERATOR, Reason.OWNER_UNKNOWN, False)),
    # our own fleet: registered on its serial alone
    (("SN-FLEET", None), (S.REGISTERED, Reason.FLEET, False)),
    (("SN-FLEET", "GEOANYTHING00001"), (S.REGISTERED, Reason.FLEET, False)),
    # unidentified: no serial at all
    ((None, "GEOabcd1234efgh"), (S.UNIDENTIFIED, Reason.NO_SERIAL, False)),
    (("", None), (S.UNIDENTIFIED, Reason.NO_SERIAL, False)),
]


@pytest.mark.parametrize(("given", "expected"), TABLE)
def test_the_truth_table(
    given: tuple[str | None, str | None],
    expected: tuple[IdentificationStatus, Reason, bool],
) -> None:
    serial, operator = given
    found = resolve(snapshot(), serial=serial, operator_reg=operator)
    assert (found.status, found.reason, found.mismatch) == expected


def test_every_status_is_in_the_table() -> None:
    """Presence: each of the four statuses, and a mismatch, actually occur."""
    statuses = {expected[0] for _, expected in TABLE}
    assert statuses == set(IdentificationStatus)
    assert any(expected[2] for _, expected in TABLE)
    assert any(not expected[2] for _, expected in TABLE)


def test_a_mismatch_names_the_registered_operator_and_nothing_else_does() -> None:
    mismatched = resolve(
        snapshot(), serial="1581F5FKD229400A", operator_reg="GEONOTREGISTERED"
    )
    assert mismatched.registered_operator_reg == "GEOabcd1234efgh"
    assert mismatched.as_dict() == {
        "status": "unknown_operator",
        "reason": "operator_mismatch",
        "serial": "1581F5FKD229400A",
        "operator_reg": "GEONOTREGISTERED",
        "mismatch": True,
        "registered_operator_reg": "GEOabcd1234efgh",
    }
    matched = resolve(
        snapshot(), serial="1581F5FKD229400A", operator_reg="GEOabcd1234efgh"
    )
    assert matched.registered_operator_reg is None
    assert matched.drone_id == REGISTERED


def test_a_serial_matches_ignoring_case_only_when_unambiguous() -> None:
    found = resolve(snapshot(), serial="sn-fleet", operator_reg=None)
    assert found.status is S.REGISTERED
    ambiguous = RegistrySnapshot(
        uas=(
            UasFacts(UUID(int=20), "x", "ab-1", R.ACTIVE, None),
            UasFacts(UUID(int=21), "y", "AB-1", R.ACTIVE, None),
        )
    )
    assert resolve(ambiguous, serial="Ab-1", operator_reg=None).status is (
        S.UNKNOWN_OPERATOR
    )
    assert resolve(ambiguous, serial="ab-1", operator_reg=None).drone_id == UUID(int=20)


def test_an_empty_registry_knows_nobody() -> None:
    found = resolve(
        RegistrySnapshot(), serial="1581F5FKD229400A", operator_reg="GEOabcd1234efgh"
    )
    assert found.status is S.UNKNOWN_OPERATOR


def test_incidents_are_for_the_unidentified_and_the_unknown() -> None:
    assert {
        S.UNIDENTIFIED,
        S.UNKNOWN_OPERATOR,
    } == INCIDENT_STATUSES
    assert S.REGISTERED not in INCIDENT_STATUSES
    assert S.SUSPENDED not in INCIDENT_STATUSES


# --- the shapes the adapters hand over ----------------------------------------


def rid(**fields: Any) -> dict[str, Any]:
    return {
        "identified": True,
        "ua_id": "1581F5FKD229400A",
        "id_type": odid.IdType.SERIAL_NUMBER,
        "operator_id": "GEOabcd1234efgh",
        **fields,
    }


def test_a_remote_id_block_is_resolved_by_its_serial_and_operator() -> None:
    assert resolve_remote_id(snapshot(), rid()).status is S.REGISTERED
    assert resolve_remote_id(snapshot(), rid(operator_id=None)).status is (
        S.UNKNOWN_OPERATOR
    )


def test_an_unidentified_remote_id_block_is_unidentified() -> None:
    found = resolve_remote_id(
        snapshot(), rid(identified=False, ua_id="", id_type=odid.IdType.NONE)
    )
    assert found.status is S.UNIDENTIFIED


def test_an_identity_that_is_not_a_serial_is_not_looked_up_as_one() -> None:
    """A CAA registration equal to a registered serial is not that aircraft."""
    found = resolve_remote_id(snapshot(), rid(id_type=odid.IdType.CAA_REGISTRATION_ID))
    assert (found.status, found.reason) == (S.UNKNOWN_OPERATOR, Reason.NOT_A_SERIAL)


@pytest.mark.parametrize(
    ("drone_id", "status", "reason"),
    [
        (FLEET, S.REGISTERED, Reason.RELAY_BINDING),
        (REGISTERED, S.REGISTERED, Reason.RELAY_BINDING),
        (SUSPENDED_UAS, S.SUSPENDED, Reason.UAS_SUSPENDED),
        (OF_SUSPENDED_OP, S.SUSPENDED, Reason.OPERATOR_SUSPENDED),
        # Bound but not yet in the projection read: the binding is the proof.
        (UUID(int=99), S.REGISTERED, Reason.RELAY_BINDING),
    ],
)
def test_a_relay_track_is_identified_by_its_binding(
    drone_id: UUID, status: IdentificationStatus, reason: Reason
) -> None:
    found = resolve_bound(snapshot(), drone_id)
    assert (found.status, found.reason, found.mismatch) == (status, reason, False)


def test_a_relay_track_carries_its_serial_and_owner() -> None:
    found = resolve_bound(snapshot(), REGISTERED).as_dict()
    assert (found["serial"], found["operator_reg"]) == (
        "1581F5FKD229400A",
        "GEOabcd1234efgh",
    )


def test_a_serial_conflict_is_an_unknown_operator_with_a_mismatch() -> None:
    found = serial_conflict("SN-FLEET", " GEOX ")
    assert (found.status, found.reason, found.mismatch) == (
        S.UNKNOWN_OPERATOR,
        Reason.SERIAL_CONFLICT,
        True,
    )
    assert found.operator_reg == "GEOX"


# --- the Remote ID ingest puts it on the bus ------------------------------------


class FakeBus:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.sent.append(json.loads(payload))


def datagram(payload: bytes) -> bytes:
    return json.dumps(
        {
            "receiver_id": "rx-1",
            "transmitter": "AA:BB:CC:00:00:09",
            "payload_hex": payload.hex(),
        }
    ).encode()


def operator_message(operator_id: str) -> bytes:
    return odid.encode_operator_id(
        odid.OperatorId(operator_id_type=0, operator_id=operator_id)
    )


def service(bus: FakeBus) -> RemoteIdIngest:
    ingest = RemoteIdIngest(
        tracker=RemoteIdTracker(geoid=FlatGeoid(), identify_within_s=0.0),
        bus=bus,
        clock_s=lambda: 0.0,
        wall=lambda: NOW,
    )
    ingest.registry = snapshot()
    ingest.fleet.take(ingest.registry)
    return ingest


@pytest.mark.parametrize(
    ("messages", "status", "mismatch"),
    [
        (
            (basic("1581F5FKD229400A"), operator_message("GEOabcd1234efgh")),
            "registered",
            False,
        ),
        (
            (basic("1581F5FKD229400B"), operator_message("GEOabcd1234efgh")),
            "suspended",
            False,
        ),
        (
            (basic("1581F5FKD229400A"), operator_message("GEONOTREGISTERED")),
            "unknown_operator",
            True,
        ),
        ((), "unidentified", False),
    ],
)
async def test_each_status_reaches_the_bus(
    messages: tuple[bytes, ...], status: str, mismatch: bool
) -> None:
    bus = FakeBus()
    ingest = service(bus)

    await ingest.on_datagram(datagram(pack(*messages, location())), "127.0.0.1")

    [message] = bus.sent
    assert message["identification"]["status"] == status
    assert message["identification"]["mismatch"] is mismatch
    assert ingest.status()[f"identified_{status}"] == 1


async def test_a_registered_uas_is_published_under_its_registry_id() -> None:
    bus = FakeBus()
    await service(bus).on_datagram(
        datagram(
            pack(
                basic("1581F5FKD229400A"),
                operator_message("GEOabcd1234efgh"),
                location(),
            )
        ),
        "127.0.0.1",
    )
    [message] = bus.sent
    assert message["drone_id"] == str(REGISTERED)


# --- S-10: a broadcast of our serial away from our aircraft ------------------------


def relay_telemetry(lat: float, lon: float) -> bytes:
    return json.dumps({"drone_id": str(FLEET), "lat_deg": lat, "lon_deg": lon}).encode()


async def test_our_serial_heard_where_our_aircraft_is_is_withheld() -> None:
    """Absence of the guard: the broadcast is our aircraft's own."""
    bus = FakeBus()
    ingest = service(bus)
    ingest.links.on_telemetry(relay_telemetry(LAT, LON + 0.001), now_s=0.0)

    await ingest.on_datagram(datagram(pack(basic("SN-FLEET"), location())), "x")

    assert bus.sent == []
    assert ingest.withheld == 1
    assert ingest.serial_conflicts == 0


async def test_our_serial_heard_far_from_our_aircraft_is_a_separate_track() -> None:
    """Presence: 0.05 degrees of longitude is about 4 km at Tbilisi."""
    bus = FakeBus()
    ingest = service(bus)
    ingest.links.on_telemetry(relay_telemetry(LAT, LON + 0.05), now_s=0.0)

    await ingest.on_datagram(datagram(pack(basic("SN-FLEET"), location())), "x")

    [message] = bus.sent
    assert message["drone_id"] != str(FLEET)
    assert "matched" not in message["remote_id"]
    assert message["authenticated"] is False
    assert message["identification"]["status"] == "unknown_operator"
    assert message["identification"]["reason"] == "serial_conflict"
    assert message["identification"]["mismatch"] is True
    assert ingest.serial_conflicts == 1
    assert ingest.withheld == 0


async def test_our_serial_with_a_quiet_link_is_published_as_ours() -> None:
    """No live link, no position to contradict it: the P1-15 takeover."""
    bus = FakeBus()
    ingest = service(bus)

    await ingest.on_datagram(datagram(pack(basic("SN-FLEET"), location())), "x")

    [message] = bus.sent
    assert message["drone_id"] == str(FLEET)
    assert message["identification"]["status"] == "registered"


def test_broadcast_sources_do_not_make_a_link_live() -> None:
    ingest = service(FakeBus())
    for source in ("remote_id", "network_remote_id"):
        ingest.links.on_telemetry(
            json.dumps(
                {
                    "drone_id": str(FLEET),
                    "source": source,
                    "lat_deg": 1.0,
                    "lon_deg": 1.0,
                }
            ).encode(),
            now_s=0.0,
        )
    assert not ingest.links.live(FLEET, now_s=0.0)
    assert ingest.links.position(FLEET) is None
    ingest.links.on_telemetry(relay_telemetry(1.0, 2.0), now_s=0.0)
    assert ingest.links.live(FLEET, now_s=0.0)
    assert ingest.links.position(FLEET) == (1.0, 2.0)


def test_fleet_serials_follow_the_snapshot() -> None:
    fleet = FleetSerials()
    fleet.take(snapshot())
    assert fleet.by_serial["SN-FLEET"] == Registered(drone_id=FLEET, label="hexa-01")


# --- a relay draining its backlog is history, not where the aircraft is -----------


def relay_row(
    lat: float, lon: float, *, backlog: bool = False, behind_s: float = 0.0
) -> bytes:
    """A relay row as the Gateway publishes it: received at NOW, captured
    `behind_s` earlier."""
    from datetime import timedelta

    return json.dumps(
        {
            "drone_id": str(FLEET),
            "lat_deg": lat,
            "lon_deg": lon,
            "backlog": backlog,
            "rx_ts": NOW.isoformat(),
            "captured_at": (NOW - timedelta(seconds=behind_s)).isoformat(),
        }
    ).encode()


async def test_a_backlog_drain_does_not_split_our_aircraft_off() -> None:
    """After an outage the relay drains minutes-old positions 4 km away.
    Those are history: the link is not live from them, so our aircraft's
    current broadcast speaks for it, and is not a spoof."""
    bus = FakeBus()
    ingest = service(bus)
    for _ in range(20):
        ingest.links.on_telemetry(relay_row(LAT, LON + 0.05, backlog=True), now_s=0.0)

    await ingest.on_datagram(datagram(pack(basic("SN-FLEET"), location())), "x")

    [message] = bus.sent
    assert message["drone_id"] == str(FLEET)
    assert message["identification"]["status"] == "registered"
    assert ingest.serial_conflicts == 0
    assert ingest.links.ignored_history == 20


async def test_old_rows_not_flagged_backlog_are_history_too() -> None:
    """Captured 120 s before the Gateway received them: not where it is."""
    bus = FakeBus()
    ingest = service(bus)
    ingest.links.on_telemetry(relay_row(LAT, LON + 0.05, behind_s=120.0), now_s=0.0)

    await ingest.on_datagram(datagram(pack(basic("SN-FLEET"), location())), "x")

    assert bus.sent[0]["drone_id"] == str(FLEET)
    assert ingest.serial_conflicts == 0


async def test_a_drain_after_live_rows_leaves_the_live_position() -> None:
    """Live and nearby, then a drain of far-away history: still withheld."""
    bus = FakeBus()
    ingest = service(bus)
    ingest.links.on_telemetry(relay_row(LAT, LON + 0.001), now_s=0.0)
    ingest.links.on_telemetry(relay_row(LAT, LON + 0.05, backlog=True), now_s=0.0)

    await ingest.on_datagram(datagram(pack(basic("SN-FLEET"), location())), "x")

    assert bus.sent == []
    assert ingest.withheld == 1


async def test_a_live_row_far_away_still_splits_it_off() -> None:
    """The presence half: current relay telemetry 4 km away."""
    bus = FakeBus()
    ingest = service(bus)
    ingest.links.on_telemetry(relay_row(LAT, LON + 0.05, behind_s=0.5), now_s=0.0)

    await ingest.on_datagram(datagram(pack(basic("SN-FLEET"), location())), "x")

    assert bus.sent[0]["identification"]["reason"] == "serial_conflict"
    assert ingest.serial_conflicts == 1
