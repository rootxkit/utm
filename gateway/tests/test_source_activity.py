"""What an adapter says about its sources, and how it counts refusals. U-15."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from common.sources import RELAY, Control, SourceControlState
from gateway.rate_limit import RateLimiter
from gateway.source_activity import (
    SourceActivity,
    publish_periodically,
    source_subject,
)

AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@dataclass
class Holder:
    state: SourceControlState


def off(instance_id: str | None) -> SourceControlState:
    return SourceControlState(
        version=1,
        controls=(
            Control(
                source_type=RELAY,
                instance_id=instance_id,
                enabled=False,
                reason="test",
                changed_by="test-admin",
                changed_at=AT.isoformat(),
            ),
        ),
    )


def activity(state: SourceControlState | None = None, **options: Any) -> SourceActivity:
    return SourceActivity(
        source_type=RELAY,
        switch=None if state is None else Holder(state),
        wall=lambda: AT,
        **options,
    )


def test_without_a_switch_everything_is_taken() -> None:
    sources = activity()
    assert sources.admit("station-1", 4)
    assert sources.instances["station-1"].accepted == 4
    assert sources.status()["refused_source_disabled"] == 0


def test_a_disabled_instance_is_refused_and_counted_and_others_taken() -> None:
    sources = activity(off("station-1"))

    assert not sources.admit("station-1", 3)
    assert sources.admit("station-2", 2)

    assert sources.instances["station-1"].refused_disabled == 3
    assert sources.instances["station-1"].last_refused_at == AT
    assert sources.instances["station-1"].last_seen_at is None
    assert sources.instances["station-2"].accepted == 2
    assert sources.status() == {
        "refused_source_disabled": 3,
        "sources_disabled": 1,
        "source_type_disabled": 0,
    }


def test_the_snapshot_lists_known_heard_and_connected_instances() -> None:
    sources = activity(
        off("station-1"),
        known=["station-1", "station-3"],
        connected=lambda: {"station-4"},
    )
    sources.admit("station-2")
    sources.seen("station-3")

    snapshot = sources.snapshot()

    assert snapshot["source_type"] == RELAY
    assert snapshot["enabled"] is True
    assert snapshot["control_version"] == 1
    by_name = {i["instance_id"]: i for i in snapshot["instances"]}
    assert sorted(by_name) == ["station-1", "station-2", "station-3", "station-4"]
    assert by_name["station-1"]["enabled"] is False
    assert by_name["station-1"]["disabled_by"] == "instance"
    assert by_name["station-1"]["last_seen_at"] is None
    assert by_name["station-2"]["accepted"] == 1
    assert by_name["station-3"]["last_seen_at"] == AT.isoformat()
    assert by_name["station-4"]["connected"] is True
    assert by_name["station-3"]["connected"] is False


def test_a_whole_type_switched_off_shows_on_every_instance() -> None:
    sources = activity(off(None), known=["station-1"])
    snapshot = sources.snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["instances"][0]["disabled_by"] == "type"
    assert sources.status()["source_type_disabled"] == 1


def test_refusals_are_logged_once_per_interval_with_a_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = [0.0]
    sources = activity(
        off("station-1"), refusals=RateLimiter(interval_s=60.0, clock=lambda: clock[0])
    )
    with caplog.at_level(logging.WARNING, logger="gateway.source_activity"):
        for _ in range(5):
            sources.admit("station-1")
        clock[0] = 61.0
        sources.admit("station-1")

    lines = [r for r in caplog.records if r.getMessage() == "source disabled; refused"]
    assert [getattr(r, "suppressed", None) for r in lines] == [0, 4]


def test_the_instance_table_is_bounded() -> None:
    sources = activity(max_instances=3)
    for n in range(5):
        sources.admit(f"station-{n}")
    assert list(sources.instances) == ["station-2", "station-3", "station-4"]


class Bus:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.fail:
            raise ConnectionError("bus down")
        self.sent.append((subject, json.loads(payload)))


async def test_the_snapshot_is_published_until_stopped_and_the_status_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = Bus()
    stop = asyncio.Event()
    sources = activity(off("station-1"), known=["station-1"])
    with caplog.at_level(logging.INFO, logger="gateway.source_activity"):
        task = asyncio.create_task(
            publish_periodically(sources, bus, stop, every_s=0.01, status_every_s=0.03)
        )
        for _ in range(200):
            if len(bus.sent) >= 4:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await task

    assert {subject for subject, _ in bus.sent} == {source_subject(RELAY)}
    status_lines = [r for r in caplog.records if r.getMessage() == "source status"]
    assert status_lines
    assert getattr(status_lines[-1], "disabled", None) == ["relay/station-1"]


async def test_a_bus_that_fails_does_not_end_the_publisher() -> None:
    bus = Bus(fail=True)
    stop = asyncio.Event()
    task = asyncio.create_task(
        publish_periodically(activity(), bus, stop, every_s=0.01)
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await task
