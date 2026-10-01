"""The U-16 bridge into the Remote ID ingest, on simulated clocks. S-27, S-32.

The bridge reads MAVLink as pymavlink parses it, broadcasts what a Remote ID
module would, drops and delays datagrams through its own `FaultyLink`
(`--drop-rate`, `--delay-s`) and signs them as a receiver does. The ingest
checks the signatures and stores what it publishes. Only the clocks are
simulated, so a minute of flight takes milliseconds and every run is the
same run.

S-32's done-when is the U-16 fault run: one message per datagram, a fifth to
a third of them dropped, and the bridge restarted on the same transmitter
address with a new serial. No Location after the restart may be stored under
the old serial.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pymavlink.dialects.v20 import ardupilotmega as dialect

from gateway import odid
from gateway.remote_id import TIME_SOURCE_BROADCAST, RemoteIdTracker
from gateway.remote_id_auth import ReceiverAuthenticator, split
from gateway.remote_id_ingest import RemoteIdIngest
from gateway.remote_id_store import PendingRows, RemoteIdRow
from gateway.tests.rid_frames import FlatGeoid
from tools import sitl_remote_id as bridge

KEY = bytes(range(32))
SYSID = 1
OLD, NEW = "SITLRID-OLD-01", "SITLRID-NEW-02"
# The simulation's t = 0, on the hour so the broadcast times are easy to read.
EPOCH = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
STEP_S = 0.25


def parsed(build: Any) -> Any:
    """A message as pymavlink hands it to the bridge: packed, then parsed."""
    sender = dialect.MAVLink(None, srcSystem=SYSID, srcComponent=1)
    packed = build(sender).pack(sender)
    return dialect.MAVLink(None).decode(bytearray(packed))


class Sitl:
    """Stands in for one SITL vehicle's MAVLink stream: hovering, armed."""

    def __init__(self) -> None:
        self.waiting: list[Any] = []
        self.started = False

    def emit(self, now_s: float, boot_s: float) -> None:
        """`now_s` on the simulation's clock, `boot_s` since this boot."""
        boot_ms = int(boot_s * 1000)
        if not self.started:
            self.started = True
            self.waiting.append(
                parsed(
                    lambda m: m.heartbeat_encode(
                        dialect.MAV_TYPE_QUADROTOR,
                        dialect.MAV_AUTOPILOT_ARDUPILOTMEGA,
                        dialect.MAV_MODE_FLAG_SAFETY_ARMED,
                        0,
                        dialect.MAV_STATE_ACTIVE,
                    )
                )
            )
            # The vehicle's UTC is the simulation's clock.
            unix_us = int((EPOCH.timestamp() + now_s) * 1e6)
            self.waiting.append(
                parsed(lambda m: m.system_time_encode(unix_us, boot_ms))
            )
        self.waiting.append(
            parsed(
                lambda m: m.global_position_int_encode(
                    boot_ms, 417151000, 448271000, 535_000, 80_000, 0, 0, 0, 9000
                )
            )
        )

    def recv_match(self, *, blocking: bool) -> Any:
        return self.waiting.pop(0) if self.waiting else None

    def close(self) -> None:
        pass


@dataclass
class Rows:
    rows: list[RemoteIdRow] = field(default_factory=list)

    async def write(self, rows: list[RemoteIdRow]) -> None:
        self.rows.extend(rows)


class Bus:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.sent.append(json.loads(payload))


@dataclass
class Run:
    rows: list[RemoteIdRow]
    published: list[dict[str, Any]]
    # What reached the ingest after the restart, in order: message kinds.
    after_restart: list[int]
    restart_at: datetime
    tracker: RemoteIdTracker


async def fly(
    tracker: RemoteIdTracker,
    *,
    seed: int,
    drop_rate: float = 0.3,
    delay_s: float = 0.0,
    restart_pause_s: float = 4.0,
    leg_s: float = 30.0,
) -> Run:
    clock = {"t": 0.0}
    rows = Rows()
    store = PendingRows(writer=rows)
    bus = Bus()
    ingest = RemoteIdIngest(
        tracker=tracker,
        bus=bus,
        store=store,
        geoid_model="flat 20 m",
        authenticator=ReceiverAuthenticator(keys={"sitl-rx": KEY}),
        clock_s=lambda: clock["t"],
        wall=lambda: EPOCH + timedelta(seconds=clock["t"]),
    )
    outbox: list[bytes] = []
    after_restart: list[int] = []
    rng = random.Random(seed)

    def leg(serial: str) -> bridge.Bridge:
        return bridge.Bridge(
            vehicles=[
                bridge.Vehicle(
                    bridge.RidModule(
                        state=bridge.VehicleState(sysid=SYSID),
                        identity=bridge.Identity(serial, "GEO-OP-SITL"),
                        geoid=FlatGeoid(),
                        transport="single",
                    ),
                    sitl,
                    bridge.transmitter_for(SYSID),
                )
            ],
            receiver=bridge.Receiver(
                "sitl-rx",
                KEY,
                wall_s=lambda: (EPOCH + timedelta(seconds=clock["t"])).timestamp(),
            ),
            link=bridge.FaultyLink(drop_rate, delay_s, rng),
            send=outbox.append,
            clock_s=lambda: clock["t"],
        )

    async def run_leg(b: bridge.Bridge, start_s: float, *, record: bool) -> None:
        steps = int(leg_s / STEP_S)
        for i in range(steps + int(delay_s / STEP_S) + 1):
            clock["t"] = start_s + i * STEP_S
            if i <= steps:
                sitl.emit(clock["t"], clock["t"] - start_s)
            b.step()
            for datagram in outbox:
                if record:
                    report, _ = split(datagram)
                    payload = bytes.fromhex(json.loads(report)["payload_hex"])
                    after_restart.append(odid.message_type(payload[0]))
                await ingest.on_datagram(datagram, "127.0.0.1")
            outbox.clear()

    sitl = Sitl()
    await run_leg(leg(OLD), 0.0, record=False)
    restart_s = clock["t"] + restart_pause_s
    sitl = Sitl()
    await run_leg(leg(NEW), restart_s, record=True)
    await store.flush()
    assert ingest.refused == 0
    return Run(
        rows=rows.rows,
        published=bus.sent,
        after_restart=after_restart,
        restart_at=EPOCH + timedelta(seconds=restart_s),
        tracker=tracker,
    )


# Chosen so that the restarted module's first Basic ID is dropped and a
# Location gets through before any Basic ID does: the case U-16 found. The
# first test asserts it, so a change in the bridge cannot quietly make the
# run miss the case.
SEED = 3


async def test_the_fault_run_exercises_a_location_before_the_new_basic_id() -> None:
    run = await fly(RemoteIdTracker(geoid=FlatGeoid()), seed=SEED)

    first_basic = run.after_restart.index(odid.MessageType.BASIC_ID)
    assert odid.MessageType.LOCATION in run.after_restart[:first_basic]


async def test_after_a_serial_change_no_location_is_stored_under_the_old_one() -> None:
    run = await fly(RemoteIdTracker(geoid=FlatGeoid()), seed=SEED)

    after = [row for row in run.rows if row.ts >= run.restart_at]
    assert after, "the restarted bridge stored nothing"
    assert OLD not in {row.ua_id for row in after}
    assert {row.ua_id for row in after} <= {NEW, ""}
    assert NEW in {row.ua_id for row in after}
    assert run.tracker.silences == 1


async def test_with_the_old_identity_rules_the_same_run_misattributes() -> None:
    """The presence half: a minute of memory, as before S-32, and the run
    above stores the new aircraft's positions under the old serial."""
    old_rules = RemoteIdTracker(geoid=FlatGeoid(), identity_ttl_s=60.0, max_gap_s=60.0)

    run = await fly(old_rules, seed=SEED)

    after = [row for row in run.rows if row.ts >= run.restart_at]
    assert OLD in {row.ua_id for row in after}


async def test_many_drop_patterns_never_store_the_old_serial() -> None:
    for seed in range(20):
        run = await fly(RemoteIdTracker(geoid=FlatGeoid()), seed=seed, leg_s=12.0)

        after = {row.ua_id for row in run.rows if row.ts >= run.restart_at}
        assert OLD not in after, f"seed {seed}"


async def test_a_delayed_broadcast_is_placed_at_its_broadcast_time() -> None:
    """S-27's done-when through the bridge: `--delay-s 2`."""
    run = await fly(
        RemoteIdTracker(geoid=FlatGeoid()), seed=SEED, drop_rate=0.0, delay_s=2.0
    )

    assert run.published
    for message in run.published:
        rx = datetime.fromisoformat(message["rx_ts"])
        captured = datetime.fromisoformat(message["captured_at"])
        assert message["remote_id"]["time_source"] == TIME_SOURCE_BROADCAST
        # The vehicle's own clock, truncated to the tenth the field holds.
        assert timedelta(seconds=1.9) <= rx - captured <= timedelta(seconds=2.0)
