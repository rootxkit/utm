"""The audit log against the real `events` table: one row per aircraft,
stamped with the transition's time, not the write's. S-13."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.monitor import Alert, AlertKind, ClearReason, Severity
from airspace.service import ACTOR_TYPE, EventsAuditLog

pytestmark = pytest.mark.postgres


async def test_the_row_carries_the_transition_time_and_the_reason(
    relational_engine: AsyncEngine,
) -> None:
    """`events.ts` defaults to now(); a row written late by the background
    writer must still say when the alert cleared."""
    a, b = uuid4(), uuid4()
    alert = Alert(
        key=f"conflict:{a}:{b}",
        kind=AlertKind.CONFLICT,
        severity=Severity.CRITICAL,
        drone_ids=(a, b),
        labels=("PG-1", "PG-2"),
        detail={"t_cpa_s": 12.5},
    )
    cleared_at = datetime(2026, 9, 29, 20, 27, 7, tzinfo=UTC)

    await EventsAuditLog(relational_engine).record(
        alert, "cleared", at=cleared_at, reason=ClearReason.STALE
    )

    async with relational_engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT ts, actor_type, entity_id, event_type, payload "
                    "FROM events WHERE entity_id IN (:a, :b) ORDER BY entity_id"
                ),
                {"a": str(a), "b": str(b)},
            )
        ).all()
    assert sorted(row.entity_id for row in rows) == sorted([str(a), str(b)])
    for row in rows:
        assert row.ts == cleared_at
        assert row.actor_type == ACTOR_TYPE
        assert row.event_type == "airspace_alert_cleared"
        assert row.payload["reason"] == "stale"
        assert row.payload["key"] == alert.key
