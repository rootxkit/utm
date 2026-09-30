"""The retention schedule: the caller `sweep` never had. S-07.

`ArchiveRetention.sweep` and `purge_closed_epochs` were designed, tested
against a real database, and never scheduled. These tests assert that the
schedule *runs* them - repeatedly, through a failure, and until told to stop
- because a sweep nobody calls bounds the archive by the disk, silently.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from gateway.config import GatewaySettings
from gateway.ingest_store import StoreError
from gateway.retention import Hold, RetentionSchedule, SweepResult


@dataclass
class FakeRetention:
    calls: list[str | None] = field(default_factory=list)
    fail_first: bool = False
    expiring: list[Hold] = field(default_factory=list)

    async def sweep(
        self, *, now: datetime | None = None, only_station: str | None = None
    ) -> SweepResult:
        self.calls.append(only_station)
        if self.fail_first and len(self.calls) == 1:
            raise StoreError("database away")
        return SweepResult(deleted_by_age=1)

    async def holds_expiring_within(
        self, days: int = 14, *, now: datetime | None = None
    ) -> list[Hold]:
        return list(self.expiring)


@dataclass
class FakeStore:
    purges: int = 0

    async def purge_closed_epochs(self) -> int:
        self.purges += 1
        return 0


async def run_for(schedule: RetentionSchedule, seconds: float) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(schedule.run_until(stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_the_schedule_sweeps_and_purges_on_every_pass() -> None:
    retention = FakeRetention()
    store = FakeStore()
    schedule = RetentionSchedule(retention=retention, store=store, interval_s=0.01)

    await run_for(schedule, 0.1)

    assert schedule.passes >= 3, (
        "the schedule ran fewer passes than the interval allows"
    )
    assert len(retention.calls) == schedule.passes
    assert store.purges == schedule.passes
    assert schedule.failures == 0


async def test_a_failed_pass_does_not_end_the_schedule() -> None:
    """A database away for a minute must not return the archive to being
    bounded by the disk for the rest of the process's life."""
    retention = FakeRetention(fail_first=True)
    schedule = RetentionSchedule(
        retention=retention, store=FakeStore(), interval_s=0.01
    )

    await run_for(schedule, 0.1)

    assert schedule.failures == 1
    assert schedule.passes >= 3
    assert len(retention.calls) == schedule.passes


async def test_the_schedule_stops_promptly_when_told() -> None:
    """The presence half of shutdown: a long interval must not hold the
    Gateway's shutdown for an hour."""
    schedule = RetentionSchedule(
        retention=FakeRetention(), store=FakeStore(), interval_s=3600.0
    )
    stop = asyncio.Event()
    task = asyncio.create_task(schedule.run_until(stop))
    await asyncio.sleep(0.02)
    stop.set()

    await asyncio.wait_for(task, timeout=1.0)

    assert schedule.passes == 1


async def test_the_schedule_scopes_the_sweep_to_the_station_it_was_given() -> None:
    retention = FakeRetention()
    schedule = RetentionSchedule(
        retention=retention, store=FakeStore(), interval_s=0.01, only_station="s-1"
    )
    await schedule.run_once()
    assert retention.calls == ["s-1"]


async def test_production_sweeps_every_station() -> None:
    retention = FakeRetention()
    schedule = RetentionSchedule(
        retention=retention, store=FakeStore(), interval_s=0.01
    )
    await schedule.run_once()
    assert retention.calls == [None]


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_the_sweep_can_be_switched_off(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    for name, setting in (
        ("TELEMETRY_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5433/db"),
        ("REDIS_URL", "redis://127.0.0.1:6379/0"),
        ("NATS_URL", "nats://127.0.0.1:4222"),
        ("RETENTION_SWEEP_ENABLED", value),
        ("RETENTION_SWEEP_INTERVAL_S", "120"),
    ):
        monkeypatch.setenv(name, setting)

    settings = GatewaySettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.retention_sweep_enabled is expected
    assert settings.retention_sweep_interval_s == 120.0
