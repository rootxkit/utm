"""The registry follower in an adapter, without a database. U-02.

The database round trip is `api/tests/test_uas_identity_projection_pg.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError

from common.uas_identity import RegistrationStatus
from gateway import registry_projection
from gateway.registry_projection import (
    OperatorFacts,
    RegistryFollower,
    RegistrySnapshot,
    UasFacts,
    operator_key,
)

ONE = RegistrySnapshot(
    uas=(UasFacts(UUID(int=1), "a", "SN-1", RegistrationStatus.ACTIVE, None),),
    operators=(OperatorFacts(UUID(int=2), "GEOx1", RegistrationStatus.ACTIVE),),
)


def follower(monkeypatch: pytest.MonkeyPatch, reads: list[Any]) -> RegistryFollower:
    async def load(_: Any) -> RegistrySnapshot:
        result = reads.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, RegistrySnapshot)
        return result

    monkeypatch.setattr(registry_projection, "load_snapshot", load)
    now = [100.0]
    return RegistryFollower(engine=None, refresh_s=0.01, clock_s=lambda: now[0])  # type: ignore[arg-type]


async def test_a_read_replaces_the_snapshot_and_tells_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[RegistrySnapshot] = []
    registry = follower(monkeypatch, [ONE])
    registry.on_change = seen.append

    assert await registry.refresh()

    assert registry.snapshot is ONE
    assert seen == [ONE]
    assert registry.loaded
    assert registry.status() == {
        "registry_loaded": 1,
        "registry_uas": 1,
        "registry_operators": 1,
        "registry_read_failures": 0,
        "registry_age_s": 0,
    }


async def test_a_failed_read_keeps_what_is_held(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A database hiccup must not make every registered aircraft unknown."""
    registry = follower(
        monkeypatch, [ONE, OperationalError("SELECT", {}, OSError("refused"))]
    )
    await registry.refresh()

    assert not await registry.refresh()

    assert registry.snapshot is ONE
    assert registry.read_failures == 1
    assert "could not read the registry projection" in caplog.text


async def test_nothing_read_yet_is_said_so(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = follower(monkeypatch, [OSError("refused")])

    assert not await registry.refresh()

    assert not registry.loaded
    assert registry.status()["registry_age_s"] == -1
    assert registry.snapshot.by_serial == {}


async def test_the_loop_rereads_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = follower(monkeypatch, [ONE] * 50)
    stop = asyncio.Event()
    task = asyncio.create_task(registry.run(stop))
    while registry.reads < 3:
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert registry.reads >= 3


def test_registration_numbers_compare_trimmed_and_upper_case() -> None:
    assert operator_key(" geo-op-sitl ") == "GEO-OP-SITL"
    assert operator_key(" geoab1-x9z ") == "GEOAB1"
    assert ONE.find_operator("geox1") is ONE.operators[0]
    assert ONE.find_operator("GEOx2") is None
