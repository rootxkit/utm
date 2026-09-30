"""Neighbour lookup. P5-06: correct against brute force, and under 5 ms at 100
airborne drones."""

from __future__ import annotations

import random
import time
from uuid import UUID

import pytest

from airspace.cpa import Track, horizontal_distance_m
from airspace.neighbours import NeighbourIndex

LAT0 = 41.7151
LON0 = 44.8271


def scattered(count: int, *, spread_deg: float, seed: int) -> list[Track]:
    rng = random.Random(seed)
    return [
        Track(
            drone_id=UUID(int=n + 1),
            lat_deg=LAT0 + rng.uniform(-spread_deg, spread_deg),
            lon_deg=LON0 + rng.uniform(-spread_deg, spread_deg),
            alt_amsl_m=550.0,
            vn_ms=0.0,
            ve_ms=0.0,
            vd_ms=0.0,
            captured_at_s=0.0,
        )
        for n in range(count)
    ]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_it_finds_exactly_what_brute_force_finds(seed: int) -> None:
    """Across cell boundaries in every direction: a grid bug shows up here."""
    tracks = scattered(300, spread_deg=0.03, seed=seed)
    index = NeighbourIndex(radius_m=800)
    for track in tracks:
        index.upsert(track)

    for me in tracks:
        expected = {
            other.drone_id
            for other in tracks
            if other.drone_id != me.drone_id and horizontal_distance_m(me, other) <= 800
        }
        found = {other.drone_id for other in index.neighbours(me.drone_id)}
        assert found == expected


def test_a_moved_aircraft_is_found_where_it_is_now() -> None:
    index = NeighbourIndex(radius_m=800)
    a, b = scattered(2, spread_deg=0.0, seed=1)
    far = Track(b.drone_id, LAT0 + 0.1, LON0, 550.0, 0.0, 0.0, 0.0, 0.0)
    index.upsert(a)
    index.upsert(far)
    assert index.neighbours(a.drone_id) == []

    index.upsert(b)
    assert [t.drone_id for t in index.neighbours(a.drone_id)] == [b.drone_id]


def test_a_removed_aircraft_is_nobodys_neighbour() -> None:
    index = NeighbourIndex(radius_m=800)
    a, b = scattered(2, spread_deg=0.0, seed=1)
    index.upsert(a)
    index.upsert(b)
    index.remove(b.drone_id)
    assert index.neighbours(a.drone_id) == []
    assert len(index) == 1


def test_lookup_stays_under_5_ms_at_100_airborne_drones() -> None:
    """P5-06's criterion. 100 drones over a city, one lookup per tick; the
    slowest of all 100 lookups must be under 5 ms."""
    tracks = scattered(100, spread_deg=0.05, seed=7)
    index = NeighbourIndex(radius_m=800)
    for track in tracks:
        index.upsert(track)

    slowest_s = 0.0
    for track in tracks:
        started = time.perf_counter()
        index.neighbours(track.drone_id)
        slowest_s = max(slowest_s, time.perf_counter() - started)

    assert slowest_s < 0.005, f"slowest lookup {slowest_s * 1000:.2f} ms"


def test_a_non_positive_radius_is_refused() -> None:
    with pytest.raises(ValueError):
        NeighbourIndex(radius_m=0)
