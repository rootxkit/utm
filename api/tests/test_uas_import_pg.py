"""The registry import against real databases (tools/import_uas_registry.py). U-01."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from api.actors import Actor
from api.uas_registry import RegistrationStatus, UasRegistry
from gateway.binding import BindingResolver
from tools.import_uas_registry import import_registry, parse_args, run

pytestmark = pytest.mark.postgres

ACTOR = Actor("import", "test")


@pytest.fixture
def registry(relational_engine: AsyncEngine, engine: AsyncEngine) -> UasRegistry:
    return UasRegistry(
        engine=relational_engine, projection=BindingResolver(engine=engine)
    )


def export() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    number = f"GEO{uuid4().hex[:13].upper()}"
    operators = [
        {
            "registration_number": number,
            "legal_name": "Imereti Drones",
            "operator_type": "legal",
            "contact_email": "a@example.ge",
            "valid_until": "2028-12-31",
        }
    ]
    uas = [
        {
            "serial": f"1A2BC{uuid4().hex[:12].upper()}",
            "operator_registration_number": number,
            "class_label": "C1",
            "mtom_g": "850",
            "model": "Mini",
        },
        {
            "serial": f"KIT-{uuid4().hex[:8]}",
            "operator_registration_number": number.lower(),
            "class_label": "",
            "mtom_g": "2400",
        },
    ]
    return operators, uas


async def event_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        count: int = (
            await connection.execute(sa.text("SELECT count(*) FROM events"))
        ).scalar_one()
    return count


def actions(report: Any) -> list[tuple[str, str]]:
    return [(outcome.kind, outcome.action) for outcome in report.outcomes]


async def test_importing_the_same_export_twice_changes_nothing_the_second_time(
    registry: UasRegistry, relational_engine: AsyncEngine
) -> None:
    operators, uas = export()

    first = await import_registry(registry, operators, uas, actor=ACTOR)
    after_first = await event_count(relational_engine)
    second = await import_registry(registry, operators, uas, actor=ACTOR)

    assert actions(first) == [
        ("operator", "create"),
        ("uas", "create"),
        ("uas", "create"),
    ]
    assert actions(second) == [
        ("operator", "unchanged"),
        ("uas", "unchanged"),
        ("uas", "unchanged"),
    ]
    assert await event_count(relational_engine) == after_first
    operator = await registry.operator_by_registration(
        operators[0]["registration_number"]
    )
    assert operator["source"] == "import"
    kit = await registry.uas_by_serial(uas[1]["serial"])
    assert kit["class_label"] is None
    assert kit["operator_registration_number"] == operator["registration_number"]


async def test_a_dry_run_reports_the_plan_and_writes_nothing(
    registry: UasRegistry, relational_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    operators, uas = export()
    before = await event_count(relational_engine)

    report = await import_registry(registry, operators, uas, actor=ACTOR, dry_run=True)

    # The UAS name an operator the same run would create: planned, not refused.
    assert actions(report) == [
        ("operator", "create"),
        ("uas", "create"),
        ("uas", "create"),
    ]
    assert await event_count(relational_engine) == before
    assert await registry.find_operator(operators[0]["registration_number"]) is None
    async with engine.connect() as connection:
        projected: int = (
            await connection.execute(
                sa.text("SELECT count(*) FROM known_drones WHERE serial = :s"),
                {"s": uas[0]["serial"]},
            )
        ).scalar_one()
    assert projected == 0


async def test_a_changed_export_updates_and_suspends_and_is_audited(
    registry: UasRegistry,
) -> None:
    operators, uas = export()
    await import_registry(registry, operators, uas, actor=ACTOR)
    operators[0]["contact_email"] = "new@example.ge"
    operators[0]["status"] = "suspended"
    uas[0]["mtom_g"] = "899"

    report = await import_registry(registry, operators, uas, actor=ACTOR)

    assert actions(report) == [
        ("operator", "update"),
        ("uas", "update"),
        ("uas", "unchanged"),
    ]
    assert set(report.outcomes[0].detail) == {"contact_email", "status"}
    operator = await registry.operator_by_registration(
        operators[0]["registration_number"]
    )
    assert operator["status"] == RegistrationStatus.SUSPENDED
    assert operator["contact_email"] == "new@example.ge"
    assert (await registry.uas_by_serial(uas[0]["serial"]))["mtom_g"] == 899
    trail = [
        (e["event_type"], e["actor_type"], e["actor_id"])
        for e in await _events(registry, "uas_operator", str(operator["id"]))
    ]
    assert trail == [
        ("registered", "import", "test"),
        ("updated", "import", "test"),
        ("suspended", "import", "test"),
    ]


async def test_a_revoked_record_is_refused_not_reactivated_and_the_rest_imported(
    registry: UasRegistry,
) -> None:
    operators, uas = export()
    await import_registry(registry, operators, uas, actor=ACTOR)
    operator = await registry.operator_by_registration(
        operators[0]["registration_number"]
    )
    await registry.set_operator_status(operator["id"], RegistrationStatus.REVOKED)
    uas[1]["model"] = "Kit 2"

    report = await import_registry(registry, operators, uas, actor=ACTOR)

    assert actions(report) == [
        ("operator", "refused"),
        ("uas", "unchanged"),
        ("uas", "update"),
    ]
    assert "final" in report.outcomes[0].detail["message"]
    assert report.refused == 1
    still = await registry.operator_by_registration(operators[0]["registration_number"])
    assert still["status"] == RegistrationStatus.REVOKED


async def test_bad_rows_are_refused_with_a_reason_and_good_ones_still_imported(
    registry: UasRegistry,
) -> None:
    operators, uas = export()
    operators.append(
        {"registration_number": "bad-1", "legal_name": "x", "operator_type": "legal"}
    )
    uas.append(
        {
            "serial": "NOT-CTA",
            "operator_registration_number": operators[0]["registration_number"],
            "class_label": "C2",
            "mtom_g": "3000",
        }
    )
    uas.append(
        {
            "serial": f"ORPHAN-{uuid4().hex[:6]}",
            "operator_registration_number": "GEO0000000000000",
            "mtom_g": "100",
        }
    )

    report = await import_registry(registry, operators, uas, actor=ACTOR)

    assert actions(report) == [
        ("operator", "create"),
        ("operator", "refused"),
        ("uas", "create"),
        ("uas", "create"),
        ("uas", "refused"),
        ("uas", "refused"),
    ]
    codes = [o.detail["code"] for o in report.outcomes if o.action == "refused"]
    assert codes == ["invalid_registration_number", "invalid_serial", "invalid_row"]


async def _events(
    registry: UasRegistry, entity_type: str, entity_id: str
) -> list[dict[str, Any]]:
    async with registry.engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT event_type, actor_type, actor_id FROM events "
                "WHERE entity_type = :t AND entity_id = :i ORDER BY id"
            ),
            {"t": entity_type, "i": entity_id},
        )
        return [dict(row._mapping) for row in rows]


async def test_the_command_imports_a_csv_and_reports_it(
    prepared_relational_database: str,
    prepared_database: str,
    registry: UasRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole command, settings to printed report: dry, real, then again."""
    operators, uas = export()
    operators_csv = tmp_path / "operators.csv"
    uas_csv = tmp_path / "uas.csv"
    for path, rows in ((operators_csv, operators), (uas_csv, uas)):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("DATABASE_URL", prepared_relational_database)
    monkeypatch.setenv("TELEMETRY_DATABASE_URL", prepared_database)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv("FEED_TICKET_SECRET", "dev-only-" + "0" * 40)
    files = ["--operators", str(operators_csv), "--uas", str(uas_csv), "--by", "cli"]

    dry = await run(parse_args([*files, "--dry-run"]))
    dry_out = capsys.readouterr().out
    first = await run(parse_args(files))
    first_out = capsys.readouterr().out
    second = await run(parse_args([*files, "--json"]))
    second_out = json.loads(capsys.readouterr().out)

    assert (dry, first, second) == (0, 0, 0)
    assert "dry run: nothing was written" in dry_out
    assert "uas: 2 created, 0 updated, 0 unchanged, 0 refused" in first_out
    assert second_out["counts"]["uas"]["unchanged"] == 2
    assert second_out["counts"]["operator"]["unchanged"] == 1
    number = operators[0]["registration_number"]
    found = await registry.operator_by_registration(number)
    trail = await _events(registry, "uas_operator", str(found["id"]))
    assert [(e["event_type"], e["actor_id"]) for e in trail] == [("registered", "cli")]
