"""Import UAS operators and UAS from a registry export. U-01.

    python tools/import_uas_registry.py --operators operators.csv --uas uas.csv
    python tools/import_uas_registry.py --operators operators.json --dry-run
    python tools/import_uas_registry.py --uas uas.csv --json > report.json

**The `uas.gov.ge` export format is not confirmed.** No export has been seen
yet. The columns below are this tool's own contract, chosen to carry what
2019/947 Art. 14 registers; when a real export arrives, it is mapped onto
them (or this contract changes) before anything is imported. Until then,
records are entered by hand through the API.

## Files

CSV (UTF-8, a header row; a byte-order mark is tolerated) or JSON (an array
of objects with the same keys), chosen by the extension.

Operators, keyed by `registration_number`:

| column | |
|---|---|
| `registration_number` | required; checked against `UAS_OPERATOR_REGISTRATION_PATTERN` |
| `legal_name` | required |
| `operator_type` | required: `natural_person` or `legal_person` (`natural`, `legal` accepted) |
| `contact_email`, `contact_phone`, `postal_address` | optional |
| `valid_until` | optional; ISO 8601. A date means valid through that day, UTC; a time must carry a zone |
| `status` | optional: `active` (default), `suspended` or `revoked` |

UAS, keyed by `serial`:

| column | |
|---|---|
| `serial` | required; CTA-2063-A for classes C1, C2, C3, C5, C6 |
| `operator_registration_number` | required; an operator registered already or in the same run |
| `class_label` | optional: `C0`-`C6`; empty or `none` for no class label |
| `mtom_g` | required for a new UAS; grams |
| `model` | optional |
| `status` | optional: `active` (default), `suspended` or `revoked` |

## What an import does

- **Upsert by key.** A new key is registered; a known one has the fields
  that differ updated, and its status moved to the file's. A record that
  matches the file is left alone and nothing is written, so running the same
  file twice changes nothing the second time.
- **A column absent from the file leaves that field as it is**; an empty
  cell clears an optional field.
- **A record the file does not mention is not touched.** An export may be
  partial; revoking what it leaves out would be a guess.
- **Revoked is final.** A revoked record the file lists as anything else is
  refused, not reactivated.
- Records are written through the registry, as changes through the API
  are, and audited in `events` as done by the `import` actor named by
  `--by`. A record is up to two transactions, not one: its field changes,
  then its status change. If the second fails, the first stands and the
  record is reported refused; running the import again finishes it. A
  refused record does not stop the rest. Operators go first, so a UAS may
  name an operator from the same run.
- **`--dry-run` writes nothing** and reports what would be done. It checks
  everything this tool checks, but not what only the database refuses (a
  duplicate label, say); those surface on the real run.

Exit status: 0 when every record was imported or unchanged, 1 when any was
refused, 2 when the files could not be read.

In tools/ because it answers the person at the terminal; only tools/ and
tests may print (tests/test_layout.py).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from api.actors import Actor
from api.config import ApiSettings
from api.registry import RegistryError
from api.uas_registry import (
    OperatorType,
    RecordSource,
    RegistrationStatus,
    UasRegistry,
)
from common import load_settings
from common.uas_identity import ClassLabel, normalize_registration_number
from gateway.binding import BindingResolver
from gateway.registry_projection import IdentityProjection

Row = Mapping[str, Any]

_OPERATOR_TYPES = {
    "natural_person": OperatorType.NATURAL_PERSON,
    "natural": OperatorType.NATURAL_PERSON,
    "legal_person": OperatorType.LEGAL_PERSON,
    "legal": OperatorType.LEGAL_PERSON,
}
_OPTIONAL_OPERATOR_TEXT = ("contact_email", "contact_phone", "postal_address")


class RowError(ValueError):
    """A record the file itself gets wrong."""


@dataclass
class Outcome:
    kind: str  # "operator" or "uas"
    key: str
    action: str  # "create", "update", "unchanged" or "refused"
    detail: Any = None


@dataclass
class Report:
    dry_run: bool
    outcomes: list[Outcome] = field(default_factory=list)

    def add(self, kind: str, key: str, action: str, detail: Any = None) -> None:
        self.outcomes.append(Outcome(kind, key, action, detail))

    def counts(self) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = {}
        for kind in ("operator", "uas"):
            totals[kind] = {
                action: sum(
                    1
                    for outcome in self.outcomes
                    if outcome.kind == kind and outcome.action == action
                )
                for action in ("create", "update", "unchanged", "refused")
            }
        return totals

    @property
    def refused(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.action == "refused")

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "counts": self.counts(),
            "outcomes": [asdict(outcome) for outcome in self.outcomes],
        }


# --- reading ------------------------------------------------------------------


def read_records(path: Path) -> list[dict[str, Any]]:
    """The records of a CSV or JSON file, keys stripped. Raises RowError."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [
                {(key or "").strip(): value for key, value in row.items()}
                for row in csv.DictReader(handle)
            ]
    if suffix == ".json":
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(loaded, list) or not all(
            isinstance(item, dict) for item in loaded
        ):
            raise RowError(f"{path}: expected a JSON array of objects")
        return [
            {str(key).strip(): value for key, value in item.items()} for item in loaded
        ]
    raise RowError(f"{path}: expected a .csv or .json file")


def _text(row: Row, name: str) -> str | None:
    value = row.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required(row: Row, name: str) -> str:
    value = _text(row, name)
    if value is None:
        raise RowError(f"{name} is required")
    return value


def parse_valid_until(value: str) -> datetime:
    """A date is valid through that day, UTC; a time must carry a zone."""
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            return datetime.combine(day + timedelta(days=1), time(), tzinfo=UTC)
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RowError(
            f"valid_until {value!r} is not an ISO 8601 date or time"
        ) from error
    if parsed.tzinfo is None:
        raise RowError(f"valid_until {value!r} must carry a zone, e.g. a trailing Z")
    return parsed.astimezone(UTC)


def parse_status(row: Row) -> RegistrationStatus:
    value = _text(row, "status")
    if value is None:
        return RegistrationStatus.ACTIVE
    try:
        return RegistrationStatus(value.lower())
    except ValueError as error:
        raise RowError(
            f"status {value!r} is not active, suspended or revoked"
        ) from error


def parse_operator(row: Row) -> tuple[str, dict[str, Any], RegistrationStatus]:
    """(registration number, the fields the file gives, status)."""
    number = normalize_registration_number(_required(row, "registration_number"))
    fields: dict[str, Any] = {"legal_name": _required(row, "legal_name")}
    kind = _required(row, "operator_type").lower()
    if kind not in _OPERATOR_TYPES:
        raise RowError(f"operator_type {kind!r} is not natural_person or legal_person")
    fields["operator_type"] = _OPERATOR_TYPES[kind]
    for name in _OPTIONAL_OPERATOR_TEXT:
        if name in row:
            fields[name] = _text(row, name)
    if "valid_until" in row:
        given = _text(row, "valid_until")
        fields["valid_until"] = None if given is None else parse_valid_until(given)
    return number, fields, parse_status(row)


def parse_uas(row: Row) -> tuple[str, str, dict[str, Any], RegistrationStatus]:
    """(serial, operator registration number, the fields the file gives, status)."""
    serial = _required(row, "serial")
    operator = normalize_registration_number(
        _required(row, "operator_registration_number")
    )
    fields: dict[str, Any] = {}
    if "class_label" in row:
        given = _text(row, "class_label")
        if given is None or given.lower() == "none":
            fields["class_label"] = None
        else:
            try:
                fields["class_label"] = ClassLabel(given.upper())
            except ValueError as error:
                raise RowError(f"class_label {given!r} is not C0-C6 or none") from error
    if "mtom_g" in row and _text(row, "mtom_g") is not None:
        raw = _required(row, "mtom_g")
        try:
            mtom_g = int(raw)
        except ValueError as error:
            raise RowError(f"mtom_g {raw!r} is not a whole number of grams") from error
        if mtom_g <= 0:
            raise RowError(f"mtom_g {mtom_g} must be positive")
        fields["mtom_g"] = mtom_g
    if "model" in row:
        fields["model"] = _text(row, "model")
    return serial, operator, fields, parse_status(row)


# --- importing ----------------------------------------------------------------


def _differs(existing: Mapping[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: {"from": existing[name], "to": value}
        for name, value in fields.items()
        if existing[name] != value
    }


async def import_registry(
    registry: UasRegistry,
    operators: Sequence[Row],
    uas: Sequence[Row],
    *,
    actor: Actor,
    dry_run: bool = False,
) -> Report:
    report = Report(dry_run=dry_run)
    # Operators this run registers, by upper-case number: a UAS may name one.
    planned: set[str] = set()
    for row in operators:
        await _import_operator(registry, row, report, planned, actor, dry_run)
    for row in uas:
        await _import_uas(registry, row, report, planned, actor, dry_run)
    return report


async def _import_operator(
    registry: UasRegistry,
    row: Row,
    report: Report,
    planned: set[str],
    actor: Actor,
    dry_run: bool,
) -> None:
    key = str(row.get("registration_number") or "").strip() or "?"
    try:
        number, fields, status = parse_operator(row)
        key = number
        registry.valid_registration_number(number)
        existing = await registry.find_operator(number)
        if existing is None:
            if not dry_run:
                await registry.create_operator(
                    registration_number=number,
                    status=status,
                    source=RecordSource.IMPORT,
                    actor=actor,
                    **fields,
                )
            planned.add(number.upper())
            report.add("operator", key, "create")
            return
        field_changes = _differs(existing, fields)
        moving = existing["status"] != status
        if moving and existing["status"] == RegistrationStatus.REVOKED:
            raise RowError("the registration is revoked, which is final")
        if not field_changes and not moving:
            report.add("operator", key, "unchanged")
            return
        changes = dict(field_changes)
        if moving:
            changes["status"] = {"from": existing["status"], "to": status}
        if not dry_run:
            operator_id: UUID = existing["id"]
            if field_changes:
                await registry.update_operator(
                    operator_id,
                    {name: fields[name] for name in field_changes},
                    source=RecordSource.IMPORT,
                    actor=actor,
                )
            if moving:
                await registry.set_operator_status(
                    operator_id, status, reason="import", actor=actor
                )
        report.add("operator", key, "update", changes)
    except (RowError, RegistryError) as error:
        report.add("operator", key, "refused", _reason(error))


async def _import_uas(
    registry: UasRegistry,
    row: Row,
    report: Report,
    planned: set[str],
    actor: Actor,
    dry_run: bool,
) -> None:
    key = str(row.get("serial") or "").strip() or "?"
    try:
        serial, operator_number, fields, status = parse_uas(row)
        key = serial
        owner = await registry.find_operator(operator_number)
        if owner is None and operator_number.upper() not in planned:
            raise RowError(f"no UAS operator {operator_number!r}")
        existing = await registry.find_uas(serial)
        if existing is not None and existing["serial"] != serial:
            raise RowError(
                f"differs only in case from the registered serial {existing['serial']!r}"
            )
        if existing is None:
            class_label = fields.get("class_label")
            registry.valid_serial(serial, class_label)
            if "mtom_g" not in fields:
                raise RowError("mtom_g is required for a new UAS")
            if not dry_run:
                # Registered earlier in this run if it was not found before.
                owner = owner or await registry.operator_by_registration(
                    operator_number
                )
                await registry.register_uas(
                    serial=serial,
                    class_label=class_label,
                    mtom_g=fields["mtom_g"],
                    uas_operator_id=owner["id"],
                    model=fields.get("model"),
                    status=status,
                    actor=actor,
                )
            report.add("uas", key, "create")
            return
        changes = _differs(existing, fields)
        if "class_label" in changes:
            registry.valid_serial(serial, fields["class_label"])
        current_owner = existing["operator_registration_number"]
        if current_owner is None or current_owner.upper() != operator_number.upper():
            changes["operator"] = {"from": current_owner, "to": operator_number}
        moving = existing["registration_status"] != status
        if moving and existing["registration_status"] == RegistrationStatus.REVOKED:
            raise RowError("the registration is revoked, which is final")
        if not changes and not moving:
            report.add("uas", key, "unchanged")
            return
        if moving:
            changes["status"] = {"from": existing["registration_status"], "to": status}
        if not dry_run:
            updates = {name: fields[name] for name in changes if name in fields}
            if "operator" in changes:
                owner = owner or await registry.operator_by_registration(
                    operator_number
                )
                updates["uas_operator_id"] = owner["id"]
            if updates:
                await registry.update_uas(existing["id"], updates, actor=actor)
            if moving:
                await registry.set_uas_status(
                    existing["id"], status, reason="import", actor=actor
                )
        report.add("uas", key, "update", changes)
    except (RowError, RegistryError) as error:
        report.add("uas", key, "refused", _reason(error))


def _reason(error: Exception) -> dict[str, str]:
    code = error.code if isinstance(error, RegistryError) else "invalid_row"
    return {"code": code, "message": str(error)}


# --- the command --------------------------------------------------------------


def render(report: Report) -> str:
    lines = []
    for outcome in report.outcomes:
        if outcome.action == "unchanged":
            continue
        detail = ""
        if outcome.action == "refused":
            detail = f": {outcome.detail['message']}"
        elif outcome.action == "update":
            detail = f": {', '.join(sorted(outcome.detail))}"
        lines.append(f"{outcome.action:9} {outcome.kind:8} {outcome.key}{detail}")
    for kind, counts in report.counts().items():
        lines.append(
            f"{kind}: {counts['create']} created, {counts['update']} updated, "
            f"{counts['unchanged']} unchanged, {counts['refused']} refused"
        )
    if report.dry_run:
        lines.append("dry run: nothing was written")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="import_uas_registry",
        description="Import UAS operators and UAS from a registry export (U-01).",
    )
    parser.add_argument("--operators", type=Path, help="operators, .csv or .json")
    parser.add_argument("--uas", type=Path, help="UAS, .csv or .json")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change; write nothing"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--by",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        help="who is importing, recorded as the actor in events",
    )
    args = parser.parse_args(argv)
    if args.operators is None and args.uas is None:
        parser.error("give --operators, --uas or both")
    return args


async def run(args: argparse.Namespace) -> int:
    try:
        operators = read_records(args.operators) if args.operators else []
        uas = read_records(args.uas) if args.uas else []
    except (OSError, RowError, json.JSONDecodeError, UnicodeDecodeError) as error:
        print(f"could not read the export: {error}", file=sys.stderr)
        return 2

    settings = load_settings(ApiSettings)
    engine = create_async_engine(str(settings.database_url))
    telemetry = create_async_engine(str(settings.telemetry_database_url))
    registry = UasRegistry(
        engine=engine,
        projection=BindingResolver(engine=telemetry),
        registration_pattern=settings.registration_pattern,
        identity=IdentityProjection(engine=telemetry),
    )
    try:
        report = await import_registry(
            registry,
            operators,
            uas,
            actor=Actor("import", args.by),
            dry_run=args.dry_run,
        )
    finally:
        await asyncio.gather(engine.dispose(), telemetry.dispose())
    print(
        json.dumps(report.as_dict(), default=str, indent=2)
        if args.json
        else render(report)
    )
    return 1 if report.refused else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
