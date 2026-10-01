"""An identity is joined to a transmitter address only while it is fresh. S-32.

The frames are built with the encoder that tests/test_odid.py pins to the
reference library's bytes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from gateway import odid
from gateway.remote_id import (
    RemoteIdTracker,
    aircraft_id,
    unidentified_aircraft_id,
)
from gateway.tests.rid_frames import (
    LAT,
    NOW,
    FlatGeoid,
    basic,
    frame,
    location,
    pack,
)

ADDRESS = "AA:BB:CC:00:00:01"
OLD, NEW = "SN-OLD-0001", "SN-NEW-0002"


def tracker(**settings: float) -> RemoteIdTracker:
    return RemoteIdTracker(geoid=FlatGeoid(), **settings)  # type: ignore[arg-type]


def serial_of(seen: dict[str, Any] | None) -> str | None:
    assert seen is not None
    ua_id = seen["remote_id"]["ua_id"]
    assert isinstance(ua_id, str)
    return ua_id


def test_a_different_basic_id_expires_the_identity() -> None:
    t = tracker()
    first = t.take(frame(pack(basic(OLD), location())), now_s=0.0)

    changed = t.take(frame(basic(NEW)), now_s=1.0)
    after = t.take(frame(location(lat=LAT + 0.001)), now_s=2.0)

    assert serial_of(first) == OLD
    # The Basic ID alone publishes nothing: the old aircraft's Location is
    # not the new one's.
    assert changed is None
    assert serial_of(after) == NEW
    assert t.identity_changes == 1


def test_a_change_drops_the_previous_operator() -> None:
    t = tracker()
    operator = odid.encode_operator_id(
        odid.OperatorId(operator_id_type=0, operator_id="GEO-OP-OLD")
    )
    t.take(frame(pack(basic(OLD), location(), operator)), now_s=0.0)

    seen = t.take(frame(pack(basic(NEW), location())), now_s=1.0)

    assert seen is not None
    assert seen["remote_id"]["operator_id"] is None


def test_the_same_basic_id_again_is_not_a_change() -> None:
    t = tracker()
    t.take(frame(pack(basic(OLD), location())), now_s=0.0)

    t.take(frame(basic(OLD)), now_s=1.0)
    seen = t.take(frame(location()), now_s=2.0)

    assert serial_of(seen) == OLD
    assert t.identity_changes == 0


def test_a_second_id_type_is_another_identity_not_a_change() -> None:
    """A serial and a registration from one module are both kept."""
    t = tracker()
    t.take(frame(pack(basic(OLD), location())), now_s=0.0)

    t.take(frame(basic("GEO-REG-1", odid.IdType.CAA_REGISTRATION_ID)), now_s=1.0)
    seen = t.take(frame(location()), now_s=2.0)

    assert serial_of(seen) == OLD
    assert t.identity_changes == 0


# --- silence -------------------------------------------------------------------


def test_after_a_silence_a_location_is_not_attached_to_the_old_serial() -> None:
    t = tracker(max_gap_s=3.0, identify_within_s=4.0)
    t.take(frame(pack(basic(OLD), location())), now_s=0.0)

    # 5 s of nothing: a reboot, or another aircraft on the address.
    held = [t.take(frame(location()), now_s=s) for s in (5.0, 6.0, 7.0, 8.0)]
    unidentified = t.take(frame(location()), now_s=9.0)

    assert t.silences == 1
    assert held == [None] * 4
    assert unidentified is not None
    assert unidentified["remote_id"]["identified"] is False
    assert unidentified["remote_id"]["ua_id"] == ""


def test_within_the_gap_the_identity_is_kept() -> None:
    """The presence half: the same frames, a silence just under the gap."""
    t = tracker(max_gap_s=3.0)
    t.take(frame(pack(basic(OLD), location())), now_s=0.0)

    seen = t.take(frame(location()), now_s=2.9)

    assert serial_of(seen) == OLD
    assert t.silences == 0


def test_a_basic_id_after_the_silence_identifies_the_held_location() -> None:
    t = tracker(max_gap_s=3.0)
    t.take(frame(pack(basic(OLD), location())), now_s=0.0)
    late = NOW + timedelta(seconds=5)

    held = t.take(frame(location(), received_at=late), now_s=5.0)
    identified = t.take(
        frame(basic(NEW), received_at=late + timedelta(seconds=1)), now_s=6.0
    )

    assert held is None
    assert serial_of(identified) == NEW
    # Placed by the frame that carried the Location, not the Basic ID.
    assert identified is not None
    assert identified["rx_ts"] == late.isoformat()


# --- age -----------------------------------------------------------------------


def test_an_identity_older_than_its_ttl_is_not_used() -> None:
    t = tracker(identity_ttl_s=15.0, identify_within_s=4.0, max_gap_s=3.0)
    t.take(frame(pack(basic(OLD), location())), now_s=0.0)

    observed = {
        now_s: t.take(frame(location()), now_s=float(now_s)) for now_s in range(1, 21)
    }

    assert serial_of(observed[15]) == OLD
    # From 15 s the identity is stale; held for 4 s, then unidentified.
    assert all(observed[s] is None for s in range(16, 19))
    unidentified = observed[19]
    assert unidentified is not None
    assert unidentified["remote_id"]["identified"] is False
    assert t.unidentified == 2


def test_the_identity_ttl_is_configurable() -> None:
    t = tracker(identity_ttl_s=60.0, max_gap_s=3.0)
    t.take(frame(pack(basic(OLD), location())), now_s=0.0)

    seen = None
    for now_s in range(1, 41):
        seen = t.take(frame(location()), now_s=float(now_s))

    assert serial_of(seen) == OLD


# --- the unidentified track ----------------------------------------------------


def test_a_transmitter_never_identified_is_published_as_unidentified() -> None:
    t = tracker(identify_within_s=4.0)

    held = [t.take(frame(location()), now_s=s) for s in (0.0, 1.0, 2.0, 3.0)]
    later = t.take(frame(location(lat=LAT + 0.001)), now_s=4.0)

    assert held == [None] * 4
    assert later is not None
    assert later["drone_id"] == str(unidentified_aircraft_id(ADDRESS))
    assert later["label"] == ADDRESS
    assert later["remote_id"]["id_type"] == odid.IdType.NONE
    assert later["remote_id"]["ua_type"] is None
    assert later["lat_deg"] == pytest.approx(LAT + 0.001)
    assert later["authenticated"] is False


def test_an_identified_track_says_so() -> None:
    seen = tracker().take(frame(pack(basic(OLD), location())), now_s=0.0)

    assert seen is not None
    assert seen["remote_id"]["identified"] is True
    expected = aircraft_id(
        odid.BasicId(id_type=odid.IdType.SERIAL_NUMBER, ua_type=2, ua_id=OLD)
    )
    assert seen["drone_id"] == str(expected)


def test_one_unidentified_transmitter_heard_by_two_receivers_is_one_id() -> None:
    t = tracker(identify_within_s=0.0)
    a = t.take(frame(location()), now_s=0.0)
    other = frame(location())
    b = t.take(
        type(other)(
            receiver_id="rx-2",
            transmitter=other.transmitter,
            received_at=other.received_at,
            payload=other.payload,
        ),
        now_s=0.0,
    )

    assert a is not None and b is not None
    assert a["drone_id"] == b["drone_id"]
