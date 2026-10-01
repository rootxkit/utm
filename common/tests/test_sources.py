"""The source switch as every follower reads it. U-15."""

from __future__ import annotations

import asyncio
import logging

import pytest

from common.sources import (
    BY_DEFAULT,
    BY_INSTANCE,
    BY_TYPE,
    RELAY,
    REMOTE_ID,
    Control,
    SourceControlFollower,
    SourceControlState,
    source_of_telemetry,
)


def control(source_type: str, instance_id: str | None, *, enabled: bool) -> Control:
    return Control(
        source_type=source_type,
        instance_id=instance_id,
        enabled=enabled,
        reason="test",
        changed_by="test-admin",
        changed_at="2026-10-01T12:00:00+00:00",
    )


def state(
    *controls: Control, version: int = 1, default_deny: bool = False
) -> SourceControlState:
    return SourceControlState(
        version=version, default_deny=default_deny, controls=controls
    )


# --- the rule ---------------------------------------------------------------


def test_with_nothing_published_every_source_is_enabled() -> None:
    nothing = SourceControlState()
    assert nothing.enabled(RELAY, "station-1")
    assert nothing.enabled(REMOTE_ID, None)


def test_an_instance_switched_off_is_disabled_and_only_it() -> None:
    held = state(control(RELAY, "station-1", enabled=False))

    assert held.why_disabled(RELAY, "station-1") == BY_INSTANCE
    assert held.enabled(RELAY, "station-2")
    assert held.enabled(REMOTE_ID, "station-1")
    assert held.enabled(RELAY, None)


def test_a_type_switched_off_disables_every_instance_whatever_its_own_row() -> None:
    held = state(
        control(REMOTE_ID, None, enabled=False),
        control(REMOTE_ID, "rx-1", enabled=True),
    )

    assert held.why_disabled(REMOTE_ID, "rx-1") == BY_TYPE
    assert held.why_disabled(REMOTE_ID, "rx-unknown") == BY_TYPE
    assert held.why_disabled(REMOTE_ID, None) == BY_TYPE
    assert held.enabled(RELAY, "rx-1")


def test_default_deny_disables_only_instances_without_a_row() -> None:
    held = state(control(RELAY, "station-1", enabled=True), default_deny=True)

    assert held.enabled(RELAY, "station-1")
    assert held.why_disabled(RELAY, "station-new") == BY_DEFAULT
    # The type itself is not an instance: default deny does not switch it off.
    assert held.enabled(RELAY, None)


def test_an_instance_switched_back_on_is_enabled() -> None:
    held = state(control(RELAY, "station-1", enabled=True))
    assert held.enabled(RELAY, "station-1")


# --- the wire form ---------------------------------------------------------------


def test_a_state_survives_its_json() -> None:
    held = state(
        control(RELAY, "station-1", enabled=False),
        control(REMOTE_ID, None, enabled=True),
        version=42,
        default_deny=True,
    )
    assert SourceControlState.from_json(held.to_json()) == held


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'{"version": "1"}',
        b'{"version": true}',
        b'{"version": 1, "default_deny": "no"}',
        b'{"version": 1, "controls": {}}',
        b'{"version": 1, "controls": [{"source_type": "relay"}]}',
        b'{"version": 1, "controls": [{"source_type": "Relay!", "enabled": true}]}',
        b'{"version": 1, "controls": [{"source_type": "relay", "enabled": "yes"}]}',
        b'{"version": 1, "controls": [{"source_type": "relay", "instance_id": "",'
        b' "enabled": true}]}',
    ],
)
def test_anything_but_a_state_is_refused(payload: bytes) -> None:
    with pytest.raises(ValueError):
        SourceControlState.from_json(payload)


# --- which source a message came from ----------------------------------------------


def test_relay_telemetry_names_its_station() -> None:
    assert source_of_telemetry({"station_id": "station-1"}) == (RELAY, "station-1")


def test_a_remote_id_observation_names_its_receiver() -> None:
    message = {"source": "remote_id", "station_id": "rx-1"}
    assert source_of_telemetry(message) == (REMOTE_ID, "rx-1")


# --- the follower --------------------------------------------------------------------


class Bucket:
    def __init__(self) -> None:
        self.value: bytes | None = None
        self.fail = False

    async def read(self) -> bytes | None:
        if self.fail:
            raise ConnectionError("broker away")
        return self.value


async def test_the_follower_applies_what_the_bucket_holds_and_says_so() -> None:
    bucket = Bucket()
    changes: list[tuple[int, int]] = []

    async def on_change(before: SourceControlState, after: SourceControlState) -> None:
        # The state is already the new one when the owner is told.
        assert follower.state is after
        changes.append((before.version, after.version))

    follower = SourceControlFollower(read=bucket.read, on_change=on_change)
    assert await follower.refresh()
    assert follower.enabled(RELAY, "station-1")
    assert changes == []

    bucket.value = state(
        control(RELAY, "station-1", enabled=False), version=5
    ).to_json()
    assert await follower.refresh()

    assert not follower.enabled(RELAY, "station-1")
    assert changes == [(0, 5)]


async def test_an_older_state_never_replaces_a_newer_one() -> None:
    follower = SourceControlFollower(read=Bucket().read)
    newer = state(control(RELAY, "station-1", enabled=True), version=10)
    older = state(control(RELAY, "station-1", enabled=False), version=9)

    assert await follower.apply(newer, origin="push")
    assert not await follower.apply(older, origin="read")

    assert follower.enabled(RELAY, "station-1")
    assert follower.ignored_older == 1


async def test_the_same_state_again_changes_nothing() -> None:
    calls: list[int] = []

    async def on_change(_: SourceControlState, after: SourceControlState) -> None:
        calls.append(after.version)

    follower = SourceControlFollower(read=Bucket().read, on_change=on_change)
    held = state(control(RELAY, "station-1", enabled=False), version=3)
    assert await follower.offer(held.to_json(), origin="push")
    assert not await follower.offer(held.to_json(), origin="read")
    assert calls == [3]


async def test_a_failed_read_keeps_the_state_held() -> None:
    """Take the bucket away: a source switched off must stay off."""
    bucket = Bucket()
    bucket.value = state(control(REMOTE_ID, None, enabled=False)).to_json()
    follower = SourceControlFollower(read=bucket.read)
    await follower.refresh()

    bucket.fail = True
    assert not await follower.refresh()

    assert not follower.enabled(REMOTE_ID, "rx-1")
    assert follower.read_failures == 1


async def test_a_malformed_push_is_ignored_and_counted() -> None:
    follower = SourceControlFollower(read=Bucket().read)
    assert not await follower.offer(b"{", origin="push")
    assert follower.ignored_malformed == 1
    assert follower.state == SourceControlState()


async def test_an_owner_that_fails_to_react_does_not_undo_the_state() -> None:
    async def on_change(_: SourceControlState, __: SourceControlState) -> None:
        raise RuntimeError("could not close a session")

    follower = SourceControlFollower(read=Bucket().read, on_change=on_change)
    held = state(control(RELAY, "station-1", enabled=False))
    assert await follower.apply(held, origin="push")
    assert not follower.enabled(RELAY, "station-1")


async def test_the_poller_reads_again_until_stopped() -> None:
    bucket = Bucket()
    follower = SourceControlFollower(read=bucket.read, poll_s=0.01)
    await follower.start()
    try:
        bucket.value = state(control(RELAY, "station-1", enabled=False)).to_json()
        for _ in range(200):
            if not follower.enabled(RELAY, "station-1"):
                break
            await asyncio.sleep(0.01)
    finally:
        await follower.stop()

    assert not follower.enabled(RELAY, "station-1")
    assert follower.reads >= 2


async def test_an_equal_version_is_never_applied_even_if_it_differs() -> None:
    """Versions are a sequence: equal means the same publication. A second
    state under the same number is a fault, and is not taken."""
    follower = SourceControlFollower(read=Bucket().read)
    assert await follower.apply(
        state(control(RELAY, "s1", enabled=False), version=5), origin="push"
    )
    assert not await follower.apply(
        state(control(RELAY, "s1", enabled=True), version=5), origin="read"
    )
    assert not follower.enabled(RELAY, "s1")


async def test_a_newer_number_for_the_same_switches_reacts_to_nothing() -> None:
    calls: list[int] = []

    async def on_change(_: SourceControlState, after: SourceControlState) -> None:
        calls.append(after.version)

    follower = SourceControlFollower(read=Bucket().read, on_change=on_change)
    await follower.apply(
        state(control(RELAY, "s1", enabled=False), version=1), origin="push"
    )
    assert not await follower.apply(
        state(control(RELAY, "s1", enabled=False), version=2), origin="read"
    )
    assert calls == [1]
    assert follower.state.version == 2


class FlakyBucket(Bucket):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def read(self) -> bytes | None:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("JetStream not ready")
        return self.value


async def test_start_retries_a_failed_first_read() -> None:
    bucket = FlakyBucket(failures=2)
    bucket.value = state(control(RELAY, "s1", enabled=False)).to_json()
    follower = SourceControlFollower(
        read=bucket.read, poll_s=3600, start_attempts=3, start_backoff_s=0.001
    )
    await follower.start()
    try:
        assert not follower.enabled(RELAY, "s1")
        assert not follower.state_unknown
        assert follower.status()["source_control_read_ok"] == 1
    finally:
        await follower.stop()


async def test_start_without_a_readable_state_serves_with_everything_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    follower = SourceControlFollower(
        read=FlakyBucket(failures=10).read,
        poll_s=3600,
        start_attempts=2,
        start_backoff_s=0.001,
    )
    with caplog.at_level(logging.WARNING, logger="common.sources"):
        await follower.start()
    try:
        assert follower.enabled(RELAY, "s1")
        assert follower.status()["source_control_state_unknown"] == 1
        assert follower.read_failures == 2
        assert any(
            r.levelno == logging.WARNING and "state unknown at start" in r.getMessage()
            for r in caplog.records
        )
    finally:
        await follower.stop()


async def test_a_run_of_failed_reads_is_logged_once_per_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = [0.0]
    bucket = FlakyBucket(failures=5)
    follower = SourceControlFollower(
        read=bucket.read, failure_log_every_s=60.0, clock_s=lambda: clock[0]
    )
    with caplog.at_level(logging.INFO, logger="common.sources"):
        for _ in range(4):
            await follower.refresh()
        clock[0] = 61.0
        await follower.refresh()
        await follower.refresh()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [getattr(r, "suppressed", None) for r in errors] == [0, 3]
    assert any(
        r.getMessage() == "source control state readable again" for r in caplog.records
    )
    assert follower.status()["source_control_read_ok"] == 1
