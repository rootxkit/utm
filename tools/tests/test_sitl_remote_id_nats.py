"""The bridge end to end: MAVLink bytes in, an observation on NATS out. U-16.

A stand-in for SITL sends real MAVLink frames over UDP; the bridge reads
them through pymavlink, as it reads SITL, and sends signed receiver
datagrams to the Remote ID ingest, which runs in-process on a real socket
and publishes on a real NATS broker. Marked `nats`: `make up` starts the
broker, and NATS_URL points at it.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from typing import Any

import nats
import pytest
from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as dialect

from gateway.remote_id import RemoteIdTracker
from gateway.remote_id_auth import ReceiverAuthenticator
from gateway.remote_id_ingest import RemoteIdIngest, listen
from gateway.tests.rid_frames import FlatGeoid
from tools import sitl_remote_id as bridge

pytestmark = pytest.mark.nats

KEY = bytes(range(32))
SYSID = 3


def nats_url() -> str:
    url = os.environ.get("NATS_URL")
    if not url:
        pytest.skip("NATS_URL is not set; `make up` starts the broker")
    return url


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def sitl_frames(serial_lat_e7: int) -> list[bytes]:
    vehicle = dialect.MAVLink(None, srcSystem=SYSID, srcComponent=1)
    return [
        vehicle.heartbeat_encode(
            dialect.MAV_TYPE_QUADROTOR,
            dialect.MAV_AUTOPILOT_ARDUPILOTMEGA,
            dialect.MAV_MODE_FLAG_SAFETY_ARMED,
            0,
            dialect.MAV_STATE_ACTIVE,
            3,
        ).pack(vehicle),
        vehicle.system_time_encode(1_790_000_000_000_000, 60_000).pack(vehicle),
        vehicle.global_position_int_encode(
            60_100, serial_lat_e7, 448271000, 635_000, 30_000, 300, -400, -150, 9000
        ).pack(vehicle),
    ]


async def test_a_sitl_position_arrives_on_nats_as_remote_id() -> None:
    client = await nats.connect(nats_url())
    serial = f"U16{uuid.uuid4().hex[:12].upper()}"
    lat_e7 = 417151000
    ingest = RemoteIdIngest(
        tracker=RemoteIdTracker(geoid=FlatGeoid()),
        bus=client,
        authenticator=ReceiverAuthenticator(keys={"sitl-rx": KEY}),
    )
    transport = await listen(ingest, "127.0.0.1", 0)
    ingest_port = transport.get_extra_info("sockname")[1]
    received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def on_message(message: Any) -> None:
        body = json.loads(message.data)
        if body.get("label") == serial:
            await received.put(body)

    await client.subscribe("telemetry.*", cb=on_message)
    mavlink_port = free_udp_port()
    source = mavutil.mavlink_connection(f"udpin:127.0.0.1:{mavlink_port}")
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    to_ingest = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(datagram: bytes) -> None:
        to_ingest.sendto(datagram, ("127.0.0.1", ingest_port))

    try:
        b = bridge.Bridge(
            vehicles=[
                bridge.Vehicle(
                    bridge.RidModule(
                        state=bridge.VehicleState(sysid=SYSID),
                        identity=bridge.Identity(serial, "GEO-OP-SITL"),
                        geoid=FlatGeoid(),
                    ),
                    source,
                    bridge.transmitter_for(SYSID),
                )
            ],
            receiver=bridge.Receiver("sitl-rx", KEY),
            link=bridge.FaultyLink(),
            send=send,
        )
        for frame in sitl_frames(lat_e7):
            out.sendto(frame, ("127.0.0.1", mavlink_port))
        for _ in range(100):
            b.step()
            if b.sent:
                break
            await asyncio.sleep(0.02)
        assert b.sent == 1

        seen = await asyncio.wait_for(received.get(), timeout=5.0)
    finally:
        out.close()
        to_ingest.close()
        source.close()
        transport.close()
        await client.drain()

    assert ingest.refused == 0
    assert ingest.published == 1
    assert seen["source"] == "remote_id"
    assert seen["authenticated"] is False
    assert seen["lat_deg"] == pytest.approx(lat_e7 / 1e7, abs=1e-7)
    assert seen["lon_deg"] == pytest.approx(44.8271, abs=1e-7)
    assert seen["alt_amsl_m"] == pytest.approx(635.0, abs=0.25)
    assert seen["alt_above_home_m"] == pytest.approx(30.0, abs=0.25)
    assert seen["groundspeed_ms"] == pytest.approx(5.0, abs=0.125)
    assert seen["station_id"] == "sitl-rx"
    assert seen["remote_id"]["transmitter"] == "02:55:16:00:00:03"
