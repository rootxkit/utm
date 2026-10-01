"""What a large zone costs the monitor, and the parser's cap on it. U-03."""

from __future__ import annotations

import copy
import json
import math
import time
from uuid import UUID

import pytest

from airspace import ed269
from airspace.ed269 import Ed269Error, Restriction, parse
from airspace.monitor import AirspaceMonitor
from airspace.tests.test_ed269 import fixture_json
from airspace.tests.test_monitor import LAT0, LON0, POLICY, A, message
from airspace.zones import Zone

VERTICES = 300_000


def huge_zone(lat_deg: float, lon_deg: float) -> Zone:
    """A circle-like polygon of 300 000 vertices, 0.01 degrees across."""
    ring = [
        (
            lon_deg + 0.01 * math.cos(2 * math.pi * k / VERTICES),
            lat_deg + 0.01 * math.sin(2 * math.pi * k / VERTICES),
        )
        for k in range(VERTICES)
    ]
    ring.append(ring[0])
    return Zone(
        zone_id=UUID(int=300),
        identifier="HUGE",
        name=None,
        restriction=Restriction.PROHIBITED,
        exterior=tuple(ring),
        holes=(),
        circle=None,
        lower=None,
        upper=None,
        periods=ed269.parse_applicability([{"permanent": "YES"}]),
    )


def per_message_s(monitor: AirspaceMonitor, north_m: float, count: int = 200) -> float:
    started = time.perf_counter()
    for n in range(count):
        monitor.observe(message(A, north_m, at_s=float(n)), now_s=float(n))
    return (time.perf_counter() - started) / count


def test_a_huge_zone_far_away_does_not_slow_a_track() -> None:
    """The bounding box answers for a track 50 km away; ray casting 300 000
    edges would take tens of milliseconds a message."""
    far = huge_zone(LAT0 + 0.5, LON0)
    with_zone = per_message_s(AirspaceMonitor(policy=POLICY, zones=[far]), 0.0)
    without = per_message_s(AirspaceMonitor(policy=POLICY), 0.0)
    assert with_zone < without + 0.001, (with_zone, without)


def test_a_huge_zone_under_the_track_is_still_judged() -> None:
    """The presence pair: the box lets a track inside through to the full
    test, which finds it."""
    here = huge_zone(LAT0, LON0)
    raised = (
        AirspaceMonitor(policy=POLICY, zones=[here])
        .observe(message(A, 0.0), now_s=0.0)
        .raised
    )
    assert [alert.detail["identifier"] for alert in raised] == ["HUGE"]


def ring_of(count: int) -> list[list[float]]:
    points = [
        [
            44.8 + 0.01 * math.cos(2 * math.pi * k / count),
            41.7 + 0.01 * math.sin(2 * math.pi * k / count),
        ]
        for k in range(count)
    ]
    return [*points, points[0]]


def document_with(ring: list[list[float]]) -> bytes:
    feature = copy.deepcopy(fixture_json()["features"][0])
    feature["geometry"][0]["horizontalProjection"]["coordinates"] = [ring]
    return json.dumps({"features": [feature]}).encode()


def test_a_ring_over_the_cap_is_refused_by_name() -> None:
    with pytest.raises(Ed269Error) as refused:
        parse(document_with(ring_of(ed269.MAX_RING_VERTICES)))
    (problem,) = refused.value.problems
    assert problem.field.endswith("horizontalProjection.coordinates[0]")
    assert f"at most {ed269.MAX_RING_VERTICES}" in problem.reason


def test_a_ring_at_the_cap_and_a_caller_s_own_cap_are_accepted() -> None:
    assert parse(document_with(ring_of(ed269.MAX_RING_VERTICES - 1))).zones
    assert parse(document_with(ring_of(6000)), max_ring_vertices=6001).zones
    with pytest.raises(Ed269Error):
        parse(document_with(ring_of(100)), max_ring_vertices=50)


def test_deep_nesting_is_refused_with_a_reason_not_a_crash() -> None:
    with pytest.raises(Ed269Error) as refused:
        parse(b'{"features": ' + b"[" * 100_000 + b"]" * 100_000 + b"}")
    assert refused.value.problems[0] == ed269.Problem(
        "$", "nested too deeply to be ED-269"
    )
