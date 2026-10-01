"""`remote_id_observations` against a real TimescaleDB. P1-15.

What only the database can show: the point is stored longitude-first, a
missing position is NULL and not (0, 0), a repeated observation is one row,
two receivers are two, and an AMSL height that does not name its geoid is
refused.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.remote_id import PRESSURE_ALTITUDE_MODEL, RemoteIdTracker
from gateway.remote_id_store import RemoteIdRow, RemoteIdWriter, row_from_observation
from gateway.tests.rid_frames import NOW, FlatGeoid, basic, frame, location, pack

pytestmark = pytest.mark.postgres


def a_row() -> RemoteIdRow:
    tracker = RemoteIdTracker(geoid=FlatGeoid())
    payload = pack(basic(), location(lat=41.7151, lon=44.8271))
    found = tracker.take(frame(payload), now_s=0.0)
    assert found is not None
    # Its own aircraft, so tests sharing the database do not see each other.
    return replace(
        row_from_observation(found, ts=NOW, payload=payload, geoid_model="EGM2008"),
        aircraft_id=uuid4(),
    )


async def stored(
    engine: AsyncEngine, row: RemoteIdRow
) -> list[sa.Row[tuple[object, ...]]]:
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    sa.text(
                        "SELECT receiver_id, ST_Y(geom) AS lat, ST_X(geom) AS lon, "
                        "geom IS NULL AS no_point, alt_hae_m, alt_amsl_m, "
                        "geoid_model, payload, ST_Y(operator_geom) AS op_lat "
                        "FROM remote_id_observations WHERE aircraft_id = :id "
                        "ORDER BY receiver_id"
                    ),
                    {"id": row.aircraft_id},
                )
            ).all()
        )


async def test_a_row_is_stored_with_its_point_the_right_way_round(
    engine: AsyncEngine,
) -> None:
    row = a_row()
    await RemoteIdWriter(engine).write([row])

    [back] = await stored(engine, row)
    assert (back.lat, back.lon) == pytest.approx((41.7151, 44.8271))
    assert (back.alt_hae_m, back.alt_amsl_m, back.geoid_model) == (
        520.0,
        500.0,
        "EGM2008",
    )
    assert bytes(back.payload) == row.payload
    assert back.op_lat is None


async def test_a_pressure_altitude_row_satisfies_the_height_model_rule(
    engine: AsyncEngine,
) -> None:
    """S-33: an AMSL height from pressure names that, not a geoid."""
    tracker = RemoteIdTracker(geoid=FlatGeoid())
    payload = pack(basic(), location(alt_hae_m=None, alt_baro_m=507.5))
    found = tracker.take(frame(payload), now_s=0.0)
    assert found is not None
    row = replace(
        row_from_observation(found, ts=NOW, payload=payload, geoid_model="EGM2008"),
        aircraft_id=uuid4(),
    )
    await RemoteIdWriter(engine).write([row])

    [back] = await stored(engine, row)
    assert (back.alt_hae_m, back.alt_amsl_m) == (None, 507.5)
    assert back.geoid_model == PRESSURE_ALTITUDE_MODEL


async def test_no_position_is_null_not_zero(engine: AsyncEngine) -> None:
    row = replace(a_row(), lat_deg=None, lon_deg=None)
    await RemoteIdWriter(engine).write([row])

    [back] = await stored(engine, row)
    assert back.no_point is True


async def test_a_repeat_is_one_row_and_a_second_receiver_is_another(
    engine: AsyncEngine,
) -> None:
    row = a_row()
    writer = RemoteIdWriter(engine)
    await writer.write([row])
    await writer.write([row, replace(row, receiver_id="rx-2")])
    await writer.write([replace(row, ts=NOW + timedelta(seconds=1))])

    back = await stored(engine, row)
    assert [r.receiver_id for r in back] == ["rx-1", "rx-1", "rx-2"]


async def test_an_amsl_height_must_name_its_geoid(engine: AsyncEngine) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        await RemoteIdWriter(engine).write([replace(a_row(), geoid_model=None)])


async def test_the_serials_of_our_active_aircraft_are_read_for_matching(
    engine: AsyncEngine,
) -> None:
    from gateway.binding import BindingResolver
    from gateway.remote_id_match import FleetSerials

    resolver = BindingResolver(engine=engine)
    active, retired, unnamed = uuid4(), uuid4(), uuid4()
    serial = f"SN-{active.hex[:8]}"
    await resolver.register_drone(active, f"a-{active.hex[:6]}", serial=serial)
    await resolver.register_drone(
        retired,
        f"r-{retired.hex[:6]}",
        serial=f"SN-{retired.hex[:8]}",
        retired_at=NOW,
    )
    await resolver.register_drone(unnamed, f"u-{unnamed.hex[:6]}")
    # Re-registering without a serial keeps the one projected.
    await resolver.register_drone(active, f"a-{active.hex[:6]}")

    fleet = FleetSerials()
    await fleet.refresh(engine)

    assert fleet.by_serial[serial].drone_id == active
    assert f"SN-{retired.hex[:8]}" not in fleet.by_serial
    assert unnamed not in {r.drone_id for r in fleet.by_serial.values()}


async def test_two_aircraft_cannot_share_a_serial(engine: AsyncEngine) -> None:
    from gateway.binding import BindingResolver
    from gateway.ingest_store import StoreError

    resolver = BindingResolver(engine=engine)
    serial = f"SN-{uuid4().hex[:8]}"
    await resolver.register_drone(uuid4(), f"x-{serial}", serial=serial)
    with pytest.raises(StoreError):
        await resolver.register_drone(uuid4(), f"y-{serial}", serial=serial)


async def test_the_matched_aircraft_is_stored_with_the_observation(
    engine: AsyncEngine,
) -> None:
    ours = uuid4()
    row = replace(a_row(), matched_drone_id=ours)
    await RemoteIdWriter(engine).write([row])

    async with engine.connect() as connection:
        matched: UUID | None = (
            await connection.execute(
                sa.text(
                    "SELECT matched_drone_id FROM remote_id_observations "
                    "WHERE aircraft_id = :id"
                ),
                {"id": row.aircraft_id},
            )
        ).scalar_one()
    assert matched == ours
