"""Receiver datagrams to the bus. P1-15."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from gateway import odid
from gateway.remote_id import RemoteIdTracker
from gateway.remote_id_ingest import (
    DatagramError,
    RemoteIdIngest,
    geoid_model,
    parse_datagram,
)
from gateway.remote_id_store import PendingRows, RemoteIdRow
from gateway.tests.rid_frames import NOW, FlatGeoid, basic, location, pack


class FakeBus:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.fail:
            raise ConnectionError("bus down")
        self.sent.append((subject, json.loads(payload)))


def datagram(payload: bytes, **extra: Any) -> bytes:
    report = {
        "receiver_id": "rx-1",
        "transmitter": "AA:BB:CC:00:00:01",
        "payload_hex": payload.hex(),
        "rssi_dbm": -70,
        **extra,
    }
    return json.dumps(report).encode()


class Rows:
    def __init__(self) -> None:
        self.rows: list[RemoteIdRow] = []

    async def write(self, rows: list[RemoteIdRow]) -> None:
        self.rows.extend(rows)


def ingest(bus: FakeBus, store: PendingRows | None = None) -> RemoteIdIngest:
    return RemoteIdIngest(
        tracker=RemoteIdTracker(geoid=FlatGeoid()),
        bus=bus,
        store=store,
        geoid_model="flat 20 m",
        clock_s=lambda: 0.0,
        wall=lambda: NOW,
    )


async def test_a_complete_broadcast_is_published_as_telemetry() -> None:
    bus = FakeBus()
    service = ingest(bus)

    await service.on_datagram(datagram(pack(basic(), location())), "127.0.0.1")

    assert len(bus.sent) == 1
    subject, message = bus.sent[0]
    assert subject == f"telemetry.{message['drone_id']}"
    assert message["source"] == "remote_id"
    assert message["ts"] == NOW.isoformat()
    assert service.published == 1


async def test_a_refused_datagram_publishes_nothing_and_is_counted() -> None:
    bus = FakeBus()
    service = ingest(bus)

    await service.on_datagram(b"not json", "127.0.0.1")
    await service.on_datagram(datagram(b"\x12\x00\x01"), "127.0.0.1")

    assert bus.sent == []
    assert service.refused == 2


async def test_a_bus_failure_is_logged_not_raised() -> None:
    service = ingest(FakeBus(fail=True))

    await service.on_datagram(datagram(pack(basic(), location())), "127.0.0.1")

    assert service.published == 0


async def test_a_published_observation_is_also_stored() -> None:
    rows = Rows()
    store = PendingRows(writer=rows)
    payload = pack(basic(), location())

    await ingest(FakeBus(), store).on_datagram(datagram(payload), "127.0.0.1")
    await store.flush()

    assert len(rows.rows) == 1
    row = rows.rows[0]
    assert (row.ts, row.receiver_id, row.payload) == (NOW, "rx-1", payload)
    assert row.geoid_model == "flat 20 m"


async def test_a_late_broadcast_is_stored_and_published_at_its_own_time() -> None:
    """S-27: 1.5 s between the broadcast and the ingest's clock."""
    rows = Rows()
    store = PendingRows(writer=rows)
    bus = FakeBus()
    # NOW is on the hour: 3598.5 s after the previous one is 1.5 s before it.
    payload = pack(basic(), location(seconds_after_hour=3598.5))

    await ingest(bus, store).on_datagram(datagram(payload), "127.0.0.1")
    await store.flush()

    broadcast = NOW - timedelta(seconds=1.5)
    assert rows.rows[0].ts == broadcast
    message = bus.sent[0][1]
    assert message["captured_at"] == broadcast.isoformat()
    assert message["rx_ts"] == NOW.isoformat()


async def test_a_bus_failure_does_not_lose_the_record() -> None:
    rows = Rows()
    store = PendingRows(writer=rows)

    await ingest(FakeBus(fail=True), store).on_datagram(
        datagram(pack(basic(), location())), "127.0.0.1"
    )
    await store.flush()

    assert len(rows.rows) == 1


async def test_refused_and_incomplete_datagrams_store_nothing() -> None:
    rows = Rows()
    store = PendingRows(writer=rows)
    service = ingest(FakeBus(), store)

    await service.on_datagram(b"not json", "127.0.0.1")
    await service.on_datagram(datagram(location()), "127.0.0.1")
    await store.flush()

    assert rows.rows == []


def test_the_geoid_is_named_by_its_description_or_its_file(tmp_path: Any) -> None:
    from common.geoid import GeoidGrid
    from common.tests.test_geoid import pgm

    described = tmp_path / "described.pgm"
    described.write_bytes(pgm())
    unnamed = tmp_path / "unnamed.pgm"
    unnamed.write_bytes(pgm().replace(b"# Description test grid\n", b""))

    assert geoid_model(None, None) is None
    assert geoid_model(GeoidGrid.load(described), described) == "test grid"
    assert geoid_model(GeoidGrid.load(unnamed), unnamed) == "unnamed.pgm"


async def test_an_incomplete_broadcast_waits_quietly() -> None:
    bus = FakeBus()
    service = ingest(bus)

    await service.on_datagram(datagram(location()), "127.0.0.1")

    assert bus.sent == []
    assert service.refused == 0


def test_the_datagram_fields_arrive_in_the_frame() -> None:
    frame = parse_datagram(datagram(basic(), rssi_dbm=-55.5), received_at=NOW)

    assert (frame.receiver_id, frame.transmitter, frame.rssi_dbm) == (
        "rx-1",
        "AA:BB:CC:00:00:01",
        -55.5,
    )
    assert odid.decode(frame.payload)[0] == odid.decode(basic())[0]


@pytest.mark.parametrize(
    ("data", "complaint"),
    [
        (b"[1, 2]", "object"),
        (json.dumps({"transmitter": "x", "payload_hex": "00"}).encode(), "receiver_id"),
        (datagram(b"", payload_hex="zz"), "hex"),
        (datagram(b"\x00", rssi_dbm="loud"), "rssi"),
        (datagram(b"\x00", rssi_dbm=True), "rssi"),
        (b" " * 5000, "more than"),
    ],
    ids=["not-object", "no-receiver", "not-hex", "rssi-text", "rssi-bool", "too-big"],
)
def test_malformed_datagrams_are_refused(data: bytes, complaint: str) -> None:
    assert parse_datagram(datagram(basic()), received_at=NOW)
    with pytest.raises(DatagramError, match=complaint):
        parse_datagram(data, received_at=NOW)


# --- the socket and the geoid, as the service wires them ------------------------


async def test_a_datagram_on_the_socket_reaches_the_bus() -> None:
    import asyncio
    import socket

    from gateway.remote_id_ingest import listen

    bus = FakeBus()
    service = ingest(bus)
    transport = await listen(service, "127.0.0.1", 0)
    port = transport.get_extra_info("sockname")[1]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(datagram(pack(basic(), location())), ("127.0.0.1", port))
        for _ in range(200):
            if bus.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        transport.close()

    assert len(bus.sent) == 1
    assert bus.sent[0][1]["source"] == "remote_id"


def test_without_a_geoid_path_there_is_no_geoid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import remote_id_ingest

    warnings: list[str] = []
    monkeypatch.setattr(
        remote_id_ingest._log,
        "warning",
        lambda message, *a, **k: warnings.append(message),
    )

    assert remote_id_ingest.load_geoid(None) is None
    assert any("no geoid model configured" in w for w in warnings)


def test_a_geoid_path_loads_the_grid(tmp_path: Any) -> None:
    from common.tests.test_geoid import pgm
    from gateway.remote_id_ingest import load_geoid

    path = tmp_path / "grid.pgm"
    path.write_bytes(pgm())
    geoid = load_geoid(path)

    assert geoid is not None
    assert geoid.undulation_m(0.0, 0.0) == pytest.approx(-100.0 + 0.01 * 1900)


# --- receiver authentication ----------------------------------------------------


def signed(payload: bytes, *, nonce: str = "n-1", key: bytes = bytes(32)) -> bytes:
    from gateway.remote_id_auth import sign

    report = {
        "receiver_id": "rx-1",
        "transmitter": "AA:BB:CC:00:00:01",
        "payload_hex": payload.hex(),
        "sent_at_ms": int(NOW.timestamp() * 1000),
        "nonce": nonce,
    }
    return sign(json.dumps(report).encode(), key)


def authenticated_ingest(bus: FakeBus) -> RemoteIdIngest:
    from gateway.remote_id_auth import ReceiverAuthenticator

    service = ingest(bus)
    service.authenticator = ReceiverAuthenticator(keys={"rx-1": bytes(32)})
    return service


async def test_with_keys_a_signed_datagram_is_published() -> None:
    bus = FakeBus()
    service = authenticated_ingest(bus)

    await service.on_datagram(signed(pack(basic(), location())), "10.0.0.7")

    assert len(bus.sent) == 1
    assert service.refused == 0


async def test_with_keys_unsigned_and_forged_datagrams_are_refused() -> None:
    bus = FakeBus()
    service = authenticated_ingest(bus)

    await service.on_datagram(datagram(pack(basic(), location())), "10.0.0.7")
    await service.on_datagram(
        signed(pack(basic(), location()), key=bytes([1]) * 32), "10.0.0.7"
    )

    assert bus.sent == []
    assert service.refused == 2


async def test_without_keys_a_signed_datagram_is_still_read() -> None:
    """A receiver configured to sign keeps working on a loopback test ingest."""
    bus = FakeBus()
    await ingest(bus).on_datagram(signed(pack(basic(), location())), "127.0.0.1")
    assert len(bus.sent) == 1


@pytest.mark.parametrize(
    ("host", "keys", "allowed"),
    [
        ("127.0.0.1", None, True),
        ("::1", None, True),
        ("localhost", None, True),
        ("0.0.0.0", None, False),
        ("10.0.0.5", None, False),
        ("receiver-gw.local", None, False),
        ("0.0.0.0", "keys", True),
    ],
)
def test_an_unauthenticated_ingest_only_binds_to_loopback(
    monkeypatch: pytest.MonkeyPatch, host: str, keys: str | None, allowed: bool
) -> None:
    from pydantic import ValidationError

    from gateway.config import RemoteIdSettings

    monkeypatch.setenv("REMOTE_ID_BIND_HOST", host)
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv(
        "TELEMETRY_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5433/t"
    )
    if keys:
        monkeypatch.setenv("REMOTE_ID_RECEIVER_KEYS", keys)
    else:
        monkeypatch.delenv("REMOTE_ID_RECEIVER_KEYS", raising=False)

    if allowed:
        assert RemoteIdSettings(_env_file=None).remote_id_bind_host == host  # type: ignore[call-arg]
    else:
        with pytest.raises(ValidationError, match="REMOTE_ID_RECEIVER_KEYS"):
            RemoteIdSettings(_env_file=None)  # type: ignore[call-arg]


def test_the_tracker_takes_its_limits_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway.config import RemoteIdSettings
    from gateway.remote_id_ingest import tracker_from_settings

    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv(
        "TELEMETRY_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5433/t"
    )
    monkeypatch.setenv("REMOTE_ID_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("REMOTE_ID_TIME_TOLERANCE_S", "0.5")
    monkeypatch.setenv("REMOTE_ID_MAX_LATENCY_S", "2.5")
    monkeypatch.setenv("REMOTE_ID_MIN_VERTICAL_ACCURACY", "4")

    tracker = tracker_from_settings(RemoteIdSettings(_env_file=None), None)  # type: ignore[call-arg]

    assert (tracker.time_tolerance_s, tracker.max_latency_s) == (0.5, 2.5)
    assert tracker.min_vertical_accuracy == 4


# --- one of ours broadcasting (serial match) --------------------------------------


def ours_ingest(bus: FakeBus, store: PendingRows | None = None) -> RemoteIdIngest:
    from gateway.remote_id_match import FleetSerials, Registered

    service = ingest(bus, store)
    service.fleet = FleetSerials(
        by_serial={"SN-RID-0001": Registered(drone_id=UUID(int=42), label="hexa-01")}
    )
    return service


async def test_our_aircraft_broadcasting_while_its_link_is_live_is_not_a_second_track() -> (
    None
):
    bus = FakeBus()
    rows = Rows()
    store = PendingRows(writer=rows)
    service = ours_ingest(bus, store)
    service.links.on_telemetry(
        json.dumps({"drone_id": str(UUID(int=42))}).encode(), now_s=0.0
    )

    await service.on_datagram(datagram(pack(basic(), location())), "127.0.0.1")
    await store.flush()

    assert bus.sent == []
    assert service.withheld == 1
    assert rows.rows[0].matched_drone_id == UUID(int=42)


async def test_when_its_link_is_quiet_the_broadcast_is_published_as_that_aircraft() -> (
    None
):
    bus = FakeBus()
    service = ours_ingest(bus)

    await service.on_datagram(datagram(pack(basic(), location())), "127.0.0.1")

    [(subject, message)] = bus.sent
    assert subject == f"telemetry.{UUID(int=42)}"
    assert (message["drone_id"], message["label"]) == (str(UUID(int=42)), "hexa-01")
    assert message["source"] == "remote_id"
    assert message["remote_id"]["matched"] is True


async def test_a_stranger_stays_a_stranger() -> None:
    bus = FakeBus()
    rows = Rows()
    store = PendingRows(writer=rows)
    service = ours_ingest(bus, store)

    await service.on_datagram(
        datagram(pack(basic("SN-STRANGER"), location())), "127.0.0.1"
    )
    await store.flush()

    [(_, message)] = bus.sent
    assert message["drone_id"] != str(UUID(int=42))
    assert "matched" not in message["remote_id"]
    assert rows.rows[0].matched_drone_id is None


async def test_the_broadcast_takes_over_once_the_link_has_been_quiet_long_enough() -> (
    None
):
    bus = FakeBus()
    service = ours_ingest(bus)
    now = [0.0]
    service.clock_s = lambda: now[0]
    service.links.on_telemetry(
        json.dumps({"drone_id": str(UUID(int=42))}).encode(), now_s=0.0
    )

    for now[0] in (1.0, 4.9):
        await service.on_datagram(datagram(pack(basic(), location())), "127.0.0.1")
    assert bus.sent == []

    now[0] = 5.1
    await service.on_datagram(datagram(pack(basic(), location())), "127.0.0.1")
    assert [subject for subject, _ in bus.sent] == [f"telemetry.{UUID(int=42)}"]


# --- the refusal warning is rate limited (S-07) ----------------------------


async def test_refusals_from_one_source_are_logged_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line per refused datagram is a way to fill the disk from any host
    that can reach the port. The first is logged; the rest are counted and
    the next report says how many."""
    from gateway import remote_id_ingest
    from gateway.rate_limit import RateLimiter

    warnings: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        remote_id_ingest._log,
        "warning",
        lambda message, *a, **k: warnings.append((message, k.get("extra"))),
    )
    clock = [0.0]
    service = ingest(FakeBus())
    service.refusals = RateLimiter(interval_s=60.0, clock=lambda: clock[0])

    for _ in range(500):
        await service.on_datagram(b"not json", "10.0.0.7")
    clock[0] += 61.0
    await service.on_datagram(b"not json", "10.0.0.7")

    assert service.refused == 501
    refused = [
        extra for message, extra in warnings if message == "remote id datagram refused"
    ]
    assert [extra["suppressed"] for extra in refused] == [0, 499]
    assert refused[0]["source"] == "10.0.0.7"
