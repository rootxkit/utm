"""Remote ID frames for tests, built with the encoder that tests/test_odid.py
pins to the reference library's bytes. P1-15."""

from __future__ import annotations

from datetime import UTC, datetime

from gateway import odid
from gateway.remote_id import Frame

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
LAT, LON = 41.7151, 44.8271


class FlatGeoid:
    """A geoid 20 m above the ellipsoid everywhere: enough to see it applied."""

    def undulation_m(self, lat_deg: float, lon_deg: float) -> float:
        return 20.0


def basic(
    ua_id: str = "SN-RID-0001", id_type: int = odid.IdType.SERIAL_NUMBER
) -> bytes:
    return odid.encode_basic_id(odid.BasicId(id_type=id_type, ua_type=2, ua_id=ua_id))


def location(
    *,
    lat: float = LAT,
    lon: float = LON,
    alt_hae_m: float | None = 520.0,
    alt_baro_m: float | None = None,
    vert_accuracy: int = 4,
    track_deg: float | None = 90.0,
    speed_ms: float | None = 10.0,
    climb_ms: float | None = 1.0,
    status: int = odid.Status.AIRBORNE,
    height_reference: int = odid.HeightReference.OVER_TAKEOFF,
    # NOW is on the hour, so 0.0 is a broadcast made the moment it arrives.
    seconds_after_hour: float | None = 0.0,
    ts_accuracy: int = 0,
) -> bytes:
    return odid.encode_location(
        odid.Location(
            status=status,
            direction_deg=track_deg,
            speed_horizontal_ms=speed_ms,
            speed_vertical_ms=climb_ms,
            lat_deg=lat,
            lon_deg=lon,
            alt_baro_m=alt_baro_m,
            alt_hae_m=alt_hae_m,
            height_reference=height_reference,
            height_m=30.0,
            horiz_accuracy=10,
            vert_accuracy=vert_accuracy,
            baro_accuracy=0,
            speed_accuracy=3,
            ts_accuracy=ts_accuracy,
            seconds_after_hour=seconds_after_hour,
        )
    )


def frame(
    payload: bytes,
    transmitter: str = "AA:BB:CC:00:00:01",
    *,
    received_at: datetime = NOW,
) -> Frame:
    return Frame(
        receiver_id="rx-1",
        transmitter=transmitter,
        received_at=received_at,
        payload=payload,
        rssi_dbm=-71.0,
    )


def pack(*messages: bytes) -> bytes:
    return odid.encode_pack(list(messages))
