"""The relay-v1 server side, driven over a real WebSocket on loopback.

Two of these exist because of things that have already shipped broken here:

- The 401 test asserts the HTTP *status code*, not merely that the connection
  failed. A stub that rejected tokens with a WebSocket close once made the
  relay's fatal-auth path untestable while every test passed. `relay-v1.md` §3
  is specific about the code, and the relay's behaviour depends on it.
- The ordering test proves the ack follows the store, rather than assuming it.
  An ack for data still in a buffer turns a Gateway crash into a permanent hole
  in the flight record, and no amount of "it passed" shows the order was right.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
import websockets
from websockets.asyncio.client import connect

from agent.framing import Record as RelayRecord
from agent.framing import encode_records
from gateway.ingest_store import InMemoryIngestStore, StoredBatch, StoreError
from gateway.rate_limit import RateLimiter
from gateway.relay_messages import Gap
from gateway.relay_records import Record
from gateway.relay_server import RelayServer
from gateway.station_state import LinkState

EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"
OTHER_EPOCH = "00112233445566778899aabbccddeeff"
TOKEN = "gateway-test-token"
STATION = "tbilisi-base-1"


class StubAuthenticator:
    """One token, one station. Token policy itself is spec §12 question 1."""

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self.tokens = tokens if tokens is not None else {TOKEN: STATION}

    async def station_for_token(self, token: str) -> str | None:
        return self.tokens.get(token)


@dataclass
class OrderRecordingStore(InMemoryIngestStore):
    """Notes the order of durable writes, so the ack can be placed against it."""

    calls: list[str] = field(default_factory=list)

    async def store_records(
        self, station_id: str, epoch: str, records: list[Record]
    ) -> StoredBatch:
        if records:
            self.calls.append(f"store:{records[0].seq}-{records[-1].seq}")
        return await super().store_records(station_id, epoch, records)

    async def record_gap(self, station_id: str, epoch: str, gap: Gap) -> None:
        self.calls.append(f"gap:{gap.from_seq}-{gap.to_seq}")
        await super().record_gap(station_id, epoch, gap)


def hello(epoch: str = EPOCH, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "type": "hello",
        "station_id": STATION,
        "epoch": epoch,
        "relay_version": "0.1.0",
        "protocol_version": 1,
        "oldest_seq_held": 0,
        "newest_seq_held": 999_999,
        "monotonic_ns": 1,
        "utc_ns": 2,
    }
    return json.dumps({**body, **overrides})


def status(**overrides: Any) -> str:
    body: dict[str, Any] = {
        "type": "status",
        "queue_depth": 0,
        "queue_bytes": 0,
        "dropped_intake_total": 0,
        "dropped_cap_total": 0,
        "last_datagram_age_ms": 20,
        "uptime_s": 100,
        "monotonic_ns": 1,
        "utc_ns": 1_758_412_800_000_000_000,
    }
    return json.dumps({**body, **overrides})


def batch(first_seq: int, count: int) -> bytes:
    return encode_records(
        [
            RelayRecord(
                seq=first_seq + n,
                recv_utc_ns=1_758_412_800_000_000_000 + n,
                datagram=bytes([n % 256]) * 16,
            )
            for n in range(count)
        ]
    )


@contextlib.asynccontextmanager
async def running(
    store: InMemoryIngestStore | None = None,
    authenticator: StubAuthenticator | None = None,
    **options: Any,
) -> Any:
    server = RelayServer(
        store=store if store is not None else InMemoryIngestStore(),
        authenticator=(
            authenticator if authenticator is not None else StubAuthenticator()
        ),
        host="127.0.0.1",
        port=0,
        **options,
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def url(server: RelayServer) -> str:
    return f"ws://127.0.0.1:{server.port_in_use}/relay/v1"


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def read_until(connection: Any, message_type: str, limit: int = 20) -> Any:
    """Read control messages until one of `message_type` arrives.

    Both failure paths say what was seen and how long it took. A bare "no ack"
    is indistinguishable between a server that sent nothing, a server that sent
    something else, and a machine that was simply slow - and that ambiguity is
    what gets a failure written off as flaky.
    """
    started = time.monotonic()
    seen: list[str] = []
    for _ in range(limit):
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=5.0)
        except TimeoutError:
            raise AssertionError(
                f"timed out after {time.monotonic() - started:.2f}s waiting for "
                f"{message_type!r}; messages seen so far: {seen}"
            ) from None
        if isinstance(raw, bytes):
            seen.append(f"<binary {len(raw)}B>")
            continue
        payload = json.loads(raw)
        seen.append(str(payload.get("type")))
        if payload.get("type") == message_type:
            return payload
    raise AssertionError(
        f"no {message_type!r} within {limit} messages after "
        f"{time.monotonic() - started:.2f}s; saw {seen}"
    )


async def handshake(connection: Any, message: str) -> Any:
    """Send `hello` and read `welcome`, with the wait bounded and explained.

    Measured worst case for this exchange is ~90 ms with every core saturated,
    against websockets' 10 s open timeout, so 15 s here is not a margin that
    can be reached by slowness. If it ever trips, the cause is something other
    than a loaded machine, and the message should not imply otherwise.
    """
    started = time.monotonic()
    await connection.send(message)
    try:
        raw = await asyncio.wait_for(connection.recv(), timeout=15.0)
    except TimeoutError:
        raise AssertionError(
            f"no reply to `hello` after {time.monotonic() - started:.2f}s. "
            f"The handshake takes ~2ms idle and ~90ms with all cores busy, so "
            f"this is not slowness - look for a server-side exception."
        ) from None
    return json.loads(raw)


# --- authentication --------------------------------------------------------


async def test_a_bad_token_is_rejected_with_http_401() -> None:
    """§3: the status code is load-bearing, not cosmetic.

    The relay treats 401 as fatal and stops retrying. A WebSocket close instead
    would leave it reconnecting for ever against a credential that will never
    work.
    """
    async with running() as server:
        with pytest.raises(websockets.InvalidStatus) as caught:
            async with connect(
                url(server), additional_headers={"Authorization": "Bearer wrong"}
            ):
                pass

    assert caught.value.response.status_code == 401


async def test_a_missing_authorization_header_is_rejected_with_http_401() -> None:
    async with running() as server:
        with pytest.raises(websockets.InvalidStatus) as caught:
            async with connect(url(server)):
                pass

    assert caught.value.response.status_code == 401


async def test_a_valid_token_is_accepted() -> None:
    """The paired presence test. Without it, a server that rejected every
    connection would pass both tests above."""
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        welcome = await handshake(connection, hello())

    assert welcome["type"] == "welcome"
    assert welcome["protocol_version"] == 1


# --- handshake -------------------------------------------------------------


async def test_an_unknown_epoch_resumes_from_zero() -> None:
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        welcome = await handshake(connection, hello())

    assert welcome["resume_from_seq"] == 0


async def test_the_resume_point_comes_from_the_store_not_from_memory() -> None:
    """§4.2: a Gateway that restarts must answer the same number.

    The store is pre-loaded here and the server is started fresh against it,
    which is what a restart looks like from the relay's side.
    """
    store = InMemoryIngestStore()
    await store.store_records(
        STATION,
        EPOCH,
        [Record(seq=n, recv_utc_ns=0, datagram=b"x") for n in range(500)],
    )

    async with (
        running(store=store) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        welcome = await handshake(connection, hello())

    assert welcome["resume_from_seq"] == 500


async def test_a_different_epoch_resumes_from_zero_independently() -> None:
    """§4: the epoch exists so a recreated queue is not mistaken for old data.

    A queue deleted and recreated restarts `seq` at 0. Without the epoch in the
    key, the Gateway would dedupe the new records away as already seen.
    """
    store = InMemoryIngestStore()
    await store.store_records(
        STATION,
        EPOCH,
        [Record(seq=n, recv_utc_ns=0, datagram=b"x") for n in range(500)],
    )

    async with (
        running(store=store) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        welcome = await handshake(connection, hello(epoch=OTHER_EPOCH))

    assert welcome["resume_from_seq"] == 0


async def test_a_station_id_that_contradicts_the_token_is_refused() -> None:
    """The token is the authority on who this is, not the `hello` body."""
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello(station_id="somebody-elses-base"))
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(connection.recv(), timeout=5.0)


async def test_a_binary_frame_before_hello_is_refused() -> None:
    """§5: `welcome` precedes any data frame."""
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(batch(0, 1))
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(connection.recv(), timeout=5.0)


# --- storing and acknowledging ---------------------------------------------


async def test_records_are_stored_and_then_acknowledged() -> None:
    store = OrderRecordingStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(batch(0, 10))
        ack = await read_until(connection, "ack")

    assert ack == {"type": "ack", "epoch": EPOCH, "seq": 9}
    assert store.calls == ["store:0-9"]
    assert len(store.records[(STATION, EPOCH)]) == 10


async def test_nothing_is_acknowledged_before_it_is_stored() -> None:
    """§4.3, and the ordering an ack promises.

    The store is made slow so that an implementation which acknowledged
    optimistically would send the ack during the delay. The assertion is on the
    observed order, not on the final state, because the final state is the same
    either way.
    """
    order: list[str] = []

    class SlowStore(InMemoryIngestStore):
        async def store_records(
            self, station_id: str, epoch: str, records: list[Record]
        ) -> StoredBatch:
            if records:
                await asyncio.sleep(0.3)
                order.append("stored")
            return await super().store_records(station_id, epoch, records)

    async with (
        running(store=SlowStore(), ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(batch(0, 4))
        await read_until(connection, "ack")
        order.append("acked")

    assert order == ["stored", "acked"]


async def test_a_retransmitted_batch_is_deduplicated() -> None:
    """§10: at-least-once on the wire, exactly-once after dedupe."""
    store = InMemoryIngestStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(batch(0, 10))
        await read_until(connection, "ack")
        await connection.send(batch(0, 10))
        await connection.send(batch(10, 5))
        ack = await read_until(connection, "ack")

    assert ack["seq"] == 14
    assert len(store.records[(STATION, EPOCH)]) == 15


async def test_the_ack_is_cumulative_across_batches() -> None:
    store = InMemoryIngestStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        for start in (0, 10, 20):
            await connection.send(batch(start, 10))
        ack = await read_until(connection, "ack")
        while ack["seq"] < 29:
            ack = await read_until(connection, "ack")

    assert ack["seq"] == 29


# --- gaps ------------------------------------------------------------------


async def test_a_gap_is_recorded_and_advances_the_resume_point() -> None:
    """§11: without this the watermark sticks at the hole for ever.

    Every later reconnect would ask for records the relay cannot supply, and
    the relay would answer with the same gap again for the life of the epoch.
    """
    store = OrderRecordingStore()

    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(batch(0, 10))
        await read_until(connection, "ack")
        await connection.send(
            json.dumps(
                {
                    "type": "gap",
                    "epoch": EPOCH,
                    "from_seq": 10,
                    "to_seq": 40,
                    "reason": "queue_cap",
                }
            )
        )
        await connection.send(batch(40, 5))
        ack = await read_until(connection, "ack")
        while ack["seq"] < 44:
            ack = await read_until(connection, "ack")

        # A fresh connection sees the advanced resume point.
        async with connect(url(server), additional_headers=auth()) as connection:
            await connection.send(hello())
            welcome = json.loads(await connection.recv())

    assert ack["seq"] == 44
    assert welcome["resume_from_seq"] == 45
    assert store.gaps[(STATION, EPOCH)][0].from_seq == 10
    assert "gap:10-40" in store.calls


async def test_the_gap_is_recorded_before_the_watermark_moves() -> None:
    """Order matters on a crash.

    A watermark past a hole with no record of the hole is a silent
    discontinuity in the flight history - the one outcome this design exists to
    prevent.
    """
    store = OrderRecordingStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(
            json.dumps(
                {
                    "type": "gap",
                    "epoch": EPOCH,
                    "from_seq": 0,
                    "to_seq": 30,
                    "reason": "queue_cap",
                }
            )
        )
        await connection.send(batch(30, 2))
        await read_until(connection, "ack")

    assert store.calls[0] == "gap:0-30"


async def test_a_gap_for_another_epoch_is_refused() -> None:
    async with (
        running(ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(
            json.dumps(
                {
                    "type": "gap",
                    "epoch": OTHER_EPOCH,
                    "from_seq": 0,
                    "to_seq": 10,
                    "reason": "queue_cap",
                }
            )
        )
        with pytest.raises(websockets.ConnectionClosed):
            for _ in range(20):
                await asyncio.wait_for(connection.recv(), timeout=5.0)


# --- status and link state -------------------------------------------------


async def test_an_intake_drop_delta_is_recorded_as_a_loss() -> None:
    store = InMemoryIngestStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(status(dropped_intake_total=0))
        await connection.send(status(dropped_intake_total=40))
        await asyncio.sleep(0.2)

    assert [loss.datagram_count for _, _, loss in store.losses] == [40]


async def test_a_healthy_station_records_no_loss() -> None:
    store = InMemoryIngestStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(status())
        await connection.send(status(uptime_s=101))
        await asyncio.sleep(0.2)

    assert store.losses == []
    # `unreachable` first: the session records its state at connect, when no
    # `status` has arrived yet. `StationLinkTracker.state` has always called
    # that unreachable - "connected, no status yet, not healthy" - but until
    # the state was reported on a timer nothing ever wrote it down, so the log
    # began wherever the first `status` put it.
    # And `unreachable` last: the session ended, and the log says so (P10-03).
    assert [state for _, state, _ in store.link_states] == [
        LinkState.UNREACHABLE,
        LinkState.HEALTHY,
        LinkState.UNREACHABLE,
    ]


async def test_an_unknown_control_message_is_ignored_and_counted() -> None:
    """§14: the connection must survive a message this Gateway does not know."""
    async with (
        running(ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await connection.send(hello())
        await connection.recv()
        await connection.send(json.dumps({"type": "from_the_future", "x": 1}))
        await connection.send(batch(0, 3))
        ack = await read_until(connection, "ack")

        assert server.trackers[STATION].ignored_message_count == 1

    assert ack["seq"] == 2


# --- station state reaches the console -------------------------------------
#
# All of this exists because of one screenshot: the console said "Stations:
# none" while `tbilisi-base-1` was connected and streaming ten aircraft. The
# link state was being computed correctly and written to `ingest_events`
# correctly. Nothing published it - `TelemetryPublisher.publish_station` had no
# caller anywhere outside its own unit test - and every test passed.
#
# So these tests assert that something is *published*, from a real session over
# a real socket, rather than that the state is *computed*, which was never the
# broken half.


@dataclass
class RecordingReporter:
    """Captures what a console would have received."""

    reports: list[tuple[str, LinkState]] = field(default_factory=list)
    last_datagram_age_ms: int | None = None
    queue_depth: int | None = None

    async def publish_station(
        self,
        station_id: str,
        state: LinkState,
        *,
        last_datagram_age_ms: int | None = None,
        queue_depth: int | None = None,
        losses: Any = None,
        lag_s: float | None = None,
    ) -> None:
        self.reports.append((station_id, state))
        self.last_datagram_age_ms = last_datagram_age_ms
        self.queue_depth = queue_depth
        self.losses = list(losses or [])
        self.lag_s = lag_s


async def wait_for_reports(reporter: RecordingReporter, count: int) -> None:
    """Wait until `count` reports have been published, or say what did arrive."""
    started = time.monotonic()
    while len(reporter.reports) < count:
        if time.monotonic() - started > 5.0:
            raise AssertionError(
                f"only {len(reporter.reports)} report(s) after "
                f"{time.monotonic() - started:.2f}s, wanted {count}: "
                f"{reporter.reports}"
            )
        await asyncio.sleep(0.01)


async def wait_for_state(reporter: RecordingReporter, state: LinkState) -> None:
    started = time.monotonic()
    while time.monotonic() - started < 5.0:
        if reporter.reports and reporter.reports[-1][1] is state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"never reported {state} within {time.monotonic() - started:.2f}s; "
        f"saw {reporter.reports}"
    )


async def test_a_connected_station_is_reported_to_the_bus() -> None:
    """The presence test for the whole defect.

    Before the fix this failed on a Gateway that was otherwise working
    perfectly: storing, acknowledging, converting and writing drone_state.
    """
    reporter = RecordingReporter()
    async with (
        running(station_reporter=reporter, station_report_interval_s=0.02) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(status())
        await wait_for_state(reporter, LinkState.HEALTHY)

    assert (STATION, LinkState.HEALTHY) in reporter.reports


async def test_the_station_is_reported_again_although_nothing_changed() -> None:
    """A console attaching late must still be able to learn the state.

    Publishing only on change is why the panel was empty: the station went
    healthy once, seconds before the browser was opened, and nothing ever said
    so again. Repetition is what makes a late subscriber serviceable.
    """
    reporter = RecordingReporter()
    async with (
        running(station_reporter=reporter, station_report_interval_s=0.02) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(status())
        # Four, not three: the first report is the `unreachable` at
        # connect, before the station has said anything.
        await wait_for_reports(reporter, 4)

    healthy = [state for _, state in reporter.reports if state is LinkState.HEALTHY]
    assert len(healthy) >= 3


async def test_the_event_log_keeps_only_transitions() -> None:
    """The paired absence test, and the reason the two paths are separate.

    The bus repeats; the log must not. `ingest_events` is what an incident is
    reconstructed from, and a state written once a second would bury the
    transitions under a heartbeat.
    """
    store = InMemoryIngestStore()
    reporter = RecordingReporter()
    async with (
        running(
            store=store, station_reporter=reporter, station_report_interval_s=0.02
        ) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(status())
        await wait_for_reports(reporter, 5)

    assert len(reporter.reports) >= 5
    # UNREACHABLE on connect, because no status has arrived yet, then HEALTHY,
    # then UNREACHABLE when the session ends. Never a row for a state that did
    # not change, however many reports went out between.
    states = [state for _, state, _ in store.link_states]
    assert states == [LinkState.UNREACHABLE, LinkState.HEALTHY, LinkState.UNREACHABLE]


async def test_a_station_that_stops_sending_status_becomes_unreachable() -> None:
    """The transition §9 exists for, which nothing could previously notice.

    `unreachable` is defined by the *absence* of `status` messages. While the
    state was evaluated only on an incoming message, a station that went quiet
    held whatever it last reported forever, and the transport saw nothing wrong
    either because the socket stays open.
    """
    reporter = RecordingReporter()
    async with (
        running(
            station_reporter=reporter,
            station_report_interval_s=0.02,
            unreachable_after_s=0.1,
        ) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(status())
        await wait_for_state(reporter, LinkState.HEALTHY)
        # Say nothing at all from here. The socket stays open.
        await wait_for_state(reporter, LinkState.UNREACHABLE)

    assert LinkState.HEALTHY in [state for _, state in reporter.reports]
    assert reporter.reports[-1] == (STATION, LinkState.UNREACHABLE)


async def test_a_station_that_disconnects_is_not_left_reported_healthy() -> None:
    """The reporter dies with the session; its last word must not be `healthy`.

    §9: a relay we cannot reach is presumed to be buffering, so `unreachable`
    is the honest state and `data_lost` stays reserved for observed loss.
    """
    reporter = RecordingReporter()
    async with running(
        station_reporter=reporter, station_report_interval_s=0.02
    ) as server:
        async with connect(url(server), additional_headers=auth()) as connection:
            await handshake(connection, hello())
            await connection.send(status())
            await wait_for_state(reporter, LinkState.HEALTHY)
        # The session's `finally` runs after the client closes.
        await wait_for_state(reporter, LinkState.UNREACHABLE)

    assert reporter.reports[-1] == (STATION, LinkState.UNREACHABLE)


async def test_a_station_that_disconnects_is_logged_unreachable() -> None:
    """The event log, not only the live report. P10-03 replay explains a hole
    in a track from `ingest_events`; a log whose last word on a departed
    station is `healthy` leaves the commonest cause of a hole unexplained."""
    store = InMemoryIngestStore()
    reporter = RecordingReporter()
    async with running(
        store=store, station_reporter=reporter, station_report_interval_s=0.02
    ) as server:
        async with connect(url(server), additional_headers=auth()) as connection:
            await handshake(connection, hello())
            await connection.send(status())
            await wait_for_state(reporter, LinkState.HEALTHY)
        await wait_for_state(reporter, LinkState.UNREACHABLE)

    states = [state for _, state, _ in store.link_states]
    assert states[-2:] == [LinkState.HEALTHY, LinkState.UNREACHABLE]


async def test_the_report_carries_the_relay_queue_depth_and_datagram_age() -> None:
    """The console renders these; they must be the station's, not placeholders."""
    reporter = RecordingReporter()
    async with (
        running(station_reporter=reporter, station_report_interval_s=0.02) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(status(queue_depth=17, last_datagram_age_ms=42))
        await wait_for_state(reporter, LinkState.HEALTHY)

    assert reporter.queue_depth == 17
    assert reporter.last_datagram_age_ms == 42


async def test_a_station_whose_backlog_grows_is_reported_lagging_and_recovers() -> None:
    """P1-14, end to end over a real socket: into `lagging` and back out.

    The stored batch was captured a year ago on the station's clock, so the
    stored record is old. It takes the queue growing as well to make the
    station lag, and the queue shrinking to bring it back.
    """
    store = InMemoryIngestStore()
    reporter = RecordingReporter()
    async with (
        running(
            store=store,
            station_reporter=reporter,
            station_report_interval_s=0.02,
            lagging_after_s=5.0,
        ) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(batch(0, 3))
        await read_until(connection, "ack")
        await connection.send(status(queue_depth=100))
        await wait_for_state(reporter, LinkState.HEALTHY)

        for depth in (900, 1_700, 2_500, 3_300, 4_100):
            await connection.send(status(queue_depth=depth))
        await wait_for_state(reporter, LinkState.LAGGING)
        lag_while_lagging = reporter.lag_s

        for depth in (3_000, 2_000, 1_000, 500, 100):
            await connection.send(status(queue_depth=depth))
        await wait_for_state(reporter, LinkState.HEALTHY)

    assert lag_while_lagging is not None and lag_while_lagging > 5.0
    states = [state for _, state, _ in store.link_states]
    # The event log records the transition in and the transition out.
    assert LinkState.LAGGING in states
    assert LinkState.HEALTHY in states[states.index(LinkState.LAGGING) :]


async def test_a_steady_station_is_never_reported_lagging() -> None:
    """The paired absence, on the same old record: without a growing queue,
    an old capture time alone - a slow station clock - is not lagging."""
    reporter = RecordingReporter()
    async with (
        running(
            station_reporter=reporter,
            station_report_interval_s=0.02,
            lagging_after_s=5.0,
        ) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(batch(0, 3))
        await read_until(connection, "ack")
        for _ in range(8):
            await connection.send(status(queue_depth=100))
        await wait_for_reports(reporter, 10)

    assert LinkState.LAGGING not in [state for _, state in reporter.reports]
    assert reporter.lag_s is not None and reporter.lag_s > 5.0


async def test_a_gateway_with_no_reporter_still_serves_the_transport() -> None:
    """The reporter is optional, and its absence must stay a console problem.

    Publishing to the bus must never be able to break ingest: the archive and
    the hypertable are the record, and the console is a view of it.
    """
    store = InMemoryIngestStore()
    async with (
        running(store=store, station_report_interval_s=0.02) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(batch(0, 4))
        await read_until(connection, "ack")

    assert len(store.records[(STATION, EPOCH)]) == 4


# --- refused connections are logged, but not without limit (P1-07) ---------


class Captured(logging.Handler):
    """Captured on the module's own logger with propagation off, rather than
    through the root logger, where a handler left behind by another test may
    hold a stream that has already been closed."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def captured_rejections(monkeypatch: pytest.MonkeyPatch) -> Any:
    logger = logging.getLogger("gateway.relay_server")
    handler = Captured()
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


async def attempt(server: RelayServer, headers: dict[str, str] | None) -> None:
    with contextlib.suppress(websockets.InvalidStatus):
        async with connect(url(server), additional_headers=headers or {}):
            pass


async def test_repeated_bad_tokens_are_logged_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each refusal costs the caller nothing, so logging every one is a way to
    fill the disk. The first is logged; the rest are counted."""
    clock = [0.0]
    limiter = RateLimiter(interval_s=60.0, clock=lambda: clock[0])

    with captured_rejections(monkeypatch) as handler:
        async with running(auth_rejections=limiter) as server:
            for _ in range(5):
                await attempt(server, {"Authorization": "Bearer wrong"})
            await attempt(server, None)
            clock[0] += 61.0
            await attempt(server, {"Authorization": "Bearer wrong"})

    lines = [
        r for r in handler.records if r.getMessage() == "rejected relay connection"
    ]
    assert [cast(Any, r).suppressed for r in lines] == [0, 5]
    assert cast(Any, lines[0]).reason == "unknown or revoked token"


async def test_a_valid_token_logs_no_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The absence half, paired with the one above."""
    with captured_rejections(monkeypatch) as handler:
        async with (
            running() as server,
            connect(
                url(server), additional_headers={"Authorization": f"Bearer {TOKEN}"}
            ),
        ):
            pass

    assert not [
        r for r in handler.records if r.getMessage() == "rejected relay connection"
    ]


# --- the pipeline sees each record once (S-05) -----------------------------


@dataclass
class RecordingProcessor:
    """Notes every record handed to the pipeline, batch by batch."""

    batches: list[list[int]] = field(default_factory=list)

    async def process(self, station_id: str, epoch: str, records: list[Record]) -> None:
        self.batches.append([record.seq for record in records])


async def test_a_retransmitted_batch_is_not_processed_again() -> None:
    """§10 at-least-once: the resend is stored-and-ignored, and the pipeline
    must not see it either, or it republishes the telemetry and counts the
    same datagrams twice in link quality."""
    processor = RecordingProcessor()
    async with (
        running(processor=processor, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(batch(0, 10))
        await read_until(connection, "ack")
        await connection.send(batch(0, 10))
        await connection.send(batch(5, 10))
        ack = await read_until(connection, "ack")
        while ack["seq"] < 14:
            ack = await read_until(connection, "ack")

    # Presence: the first copy was processed, and the new tail of the
    # overlapping batch. Absence: neither duplicate reached the pipeline.
    assert processor.batches == [list(range(10)), list(range(10, 15))]


# --- a superseded session writes nothing (S-06) ----------------------------
#
# Trackers are shared per station. After a half-open link the relay
# reconnects in seconds; the old session notices only when its own pings time
# out, and its last act was to log and publish `unreachable` over a station
# whose new session was healthy and streaming.


async def test_a_stale_session_closing_does_not_mark_a_live_station_unreachable() -> (
    None
):
    store = InMemoryIngestStore()
    reporter = RecordingReporter()
    async with running(
        store=store, station_reporter=reporter, station_report_interval_s=0.02
    ) as server:
        old = await connect(url(server), additional_headers=auth())
        await handshake(old, hello())
        await old.send(status())
        await wait_for_state(reporter, LinkState.HEALTHY)

        # The relay reconnects while the old socket is still open.
        async with connect(url(server), additional_headers=auth()) as new:
            await handshake(new, hello())
            await new.send(status())
            await wait_for_state(reporter, LinkState.HEALTHY)
            since = len(reporter.reports)

            # Now the old session finds out, the way it would after a ping
            # timeout, and ends.
            await old.close()
            await asyncio.sleep(0.2)

            assert LinkState.UNREACHABLE not in [
                state for _, state in reporter.reports[since:]
            ], "the stale session overwrote the live station's state"
            logged_while_live = [state for _, state, _ in store.link_states]
            assert logged_while_live[-1] is LinkState.HEALTHY

        # Presence: when the session that speaks for the station ends, the
        # station really is unreachable, and it is said.
        await wait_for_state(reporter, LinkState.UNREACHABLE)

    logged_after = [state for _, state, _ in store.link_states]
    assert logged_after[-1] is LinkState.UNREACHABLE


# --- the reporter outlives a store failure (S-06) --------------------------


@dataclass
class FailingLinkStateStore(InMemoryIngestStore):
    """`record_link_state` raises for the states named, once each."""

    fail_on: set[LinkState] = field(default_factory=set)
    failures: list[LinkState] = field(default_factory=list)

    async def record_link_state(
        self, station_id: str, state: LinkState, *, at_utc_ns: int
    ) -> None:
        if state in self.fail_on:
            self.fail_on.discard(state)
            self.failures.append(state)
            raise StoreError(f"event log unavailable while writing {state}")
        await super().record_link_state(station_id, state, at_utc_ns=at_utc_ns)


async def test_a_store_failure_in_one_tick_does_not_end_the_reporter() -> None:
    """The console froze: a StoreError killed the reporter task, and nothing
    was published for the rest of the session. The tick must log and go on,
    and the transition it failed to write must reach the log on a later tick.
    """
    store = FailingLinkStateStore(fail_on={LinkState.HEALTHY})
    reporter = RecordingReporter()
    async with (
        running(
            store=store, station_reporter=reporter, station_report_interval_s=0.02
        ) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(status())
        await wait_for_state(reporter, LinkState.HEALTHY)
        assert store.failures == [LinkState.HEALTHY]
        # Still alive: reports keep arriving after the failure.
        seen = len(reporter.reports)
        await wait_for_reports(reporter, seen + 3)

    # Retried, and logged, once the store came back.
    assert LinkState.HEALTHY in [state for _, state, _ in store.link_states]


async def test_a_failed_disconnect_write_still_reports_the_station_unreachable() -> (
    None
):
    """The `except StoreError` in `_report_disconnected`, which had never run.

    The session is over either way; what must survive the failure is the
    report, so the console is not left showing `healthy` for a station that
    left.
    """
    store = FailingLinkStateStore()
    reporter = RecordingReporter()
    async with running(
        store=store, station_reporter=reporter, station_report_interval_s=0.02
    ) as server:
        async with connect(url(server), additional_headers=auth()) as connection:
            await handshake(connection, hello())
            await connection.send(status())
            await wait_for_state(reporter, LinkState.HEALTHY)
            # Arm the failure for the disconnect write only.
            store.fail_on.add(LinkState.UNREACHABLE)
        await wait_for_state(reporter, LinkState.UNREACHABLE)

    assert store.failures == [LinkState.UNREACHABLE]
    assert reporter.reports[-1] == (STATION, LinkState.UNREACHABLE)


@dataclass
class RaisingReporter:
    """A bus that is down, from the session's point of view."""

    attempts: int = 0

    async def publish_station(
        self,
        station_id: str,
        state: LinkState,
        *,
        last_datagram_age_ms: int | None = None,
        queue_depth: int | None = None,
        losses: Any = None,
        lag_s: float | None = None,
    ) -> None:
        self.attempts += 1
        raise RuntimeError("bus unavailable")


async def test_a_reporter_that_raises_does_not_break_ingest() -> None:
    """The console is a view of the record, never a condition of it."""
    store = InMemoryIngestStore()
    reporter = RaisingReporter()
    async with (
        running(
            store=store,
            station_reporter=reporter,
            station_report_interval_s=0.02,
            ack_interval_s=0.05,
        ) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(status())
        await asyncio.sleep(0.1)
        await connection.send(batch(0, 4))
        ack = await read_until(connection, "ack")

    assert ack["seq"] == 3
    assert reporter.attempts >= 3, "the reporter stopped after the first failure"
    assert len(store.records[(STATION, EPOCH)]) == 4


# --- a gap is checked against the resume point and the hello (S-08) --------


async def send_gap(connection: Any, from_seq: int, to_seq: int) -> None:
    await connection.send(
        json.dumps(
            {
                "type": "gap",
                "epoch": EPOCH,
                "from_seq": from_seq,
                "to_seq": to_seq,
                "reason": "queue_cap",
            }
        )
    )


async def expect_closed(connection: Any) -> websockets.ConnectionClosed:
    with pytest.raises(websockets.ConnectionClosed) as caught:
        for _ in range(20):
            await asyncio.wait_for(connection.recv(), timeout=5.0)
    return caught.value


async def test_a_gap_that_does_not_start_at_the_resume_point_is_refused() -> None:
    """§11: a gap begins where `welcome` asked the relay to resume. One that
    starts elsewhere would advance the resume point over records that may
    still exist, so it is a protocol error and nothing is recorded."""
    store = OrderRecordingStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(batch(0, 10))
        await read_until(connection, "ack")
        await send_gap(connection, 12, 40)
        closed = await expect_closed(connection)

    assert closed.rcvd is not None and closed.rcvd.code == 1008
    assert "gap:12-40" not in store.calls
    assert (STATION, EPOCH) not in store.gaps


async def test_a_gap_ending_past_the_newest_record_held_is_refused() -> None:
    """The records past `newest_seq_held + 1` were never assigned, so a gap
    claiming them is confusion about the epoch, not loss."""
    store = OrderRecordingStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello(newest_seq_held=50))
        await send_gap(connection, 0, 52)
        closed = await expect_closed(connection)

    assert closed.rcvd is not None and closed.rcvd.code == 1008
    assert (STATION, EPOCH) not in store.gaps


async def test_a_gap_ending_exactly_one_past_the_newest_record_is_accepted() -> None:
    """The presence half, on the boundary: the relay lost everything it held,
    including the newest record, and says so with `to_seq = newest + 1`."""
    store = OrderRecordingStore()
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello(newest_seq_held=50))
        await send_gap(connection, 0, 51)
        ack = await read_until(connection, "ack")

    assert ack["seq"] == 50
    assert "gap:0-51" in store.calls


async def test_a_gap_on_a_resumed_session_starts_at_the_durable_resume_point() -> None:
    """The check uses what `welcome` said, not a watermark that starts at -1
    on every connection. A relay resuming from 500 sends a gap from 500."""
    store = OrderRecordingStore()
    await store.store_records(
        STATION,
        EPOCH,
        [Record(seq=n, recv_utc_ns=0, datagram=b"x") for n in range(500)],
    )
    async with (
        running(store=store, ack_interval_s=0.05) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        welcome = await handshake(connection, hello())
        assert welcome["resume_from_seq"] == 500
        await send_gap(connection, 500, 600)
        ack = await read_until(connection, "ack")

    assert ack["seq"] == 599
    assert "gap:500-600" in store.calls


# --- the path carries the protocol version (S-08, protocol §14) ------------


async def test_a_connection_to_another_path_is_refused_with_http_404() -> None:
    """A relay speaking a version this Gateway does not serve is told so at
    the upgrade, before its token is looked at."""
    async with running() as server:
        with pytest.raises(websockets.InvalidStatus) as caught:
            async with connect(
                f"ws://127.0.0.1:{server.port_in_use}/relay/v2",
                additional_headers=auth(),
            ):
                pass

    assert caught.value.response.status_code == 404


async def test_the_relay_v1_path_is_served() -> None:
    """The presence half: `url()` is `/relay/v1`, and it is welcomed."""
    async with (
        running() as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        welcome = await handshake(connection, hello())

    assert welcome["type"] == "welcome"
