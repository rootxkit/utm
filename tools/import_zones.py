"""Import an authority's ED-269 geographical zones. P5-18.

    python -m tools.import_zones local/zones/gcaa-2026-09.json --source GCAA
    python -m tools.import_zones FILE --source GCAA --dry-run

Reads DATABASE_URL (the relational database). Replaces the zones of that
source with the file's, in one transaction, and logs the change to
`events`; the airspace monitor picks them up within a minute. Prints what
changed, and the bounding box of what was read: check it is where the
authority's zones should be (longitude first, then latitude).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from airspace.ed269 import Ed269Error
from airspace.zone_import import ImportReport, import_zones


def describe(report: ImportReport, *, dry_run: bool) -> str:
    lines = [
        f"source {report.source}, version {report.version or '(none given)'}, "
        f"sha256 {report.sha256[:16]}...",
        f"{report.zones} zone volume(s) read",
    ]
    if report.bounds is not None:
        min_lon, min_lat, max_lon, max_lat = report.bounds
        lines.append(
            f"bounds: lon {min_lon:.4f} to {max_lon:.4f}, "
            f"lat {min_lat:.4f} to {max_lat:.4f}"
        )
    for identifier, why in report.skipped:
        lines.append(f"skipped {identifier}: {why}")
    if not report.imported and not dry_run:
        lines.append("this file is already the current version: nothing changed")
        return "\n".join(lines)
    lines.append(
        f"added {len(report.added)}, removed {len(report.removed)}, "
        f"changed {len(report.changed)}, unchanged {report.unchanged}"
    )
    for label, names in (
        ("added", report.added),
        ("removed", report.removed),
        ("changed", report.changed),
    ):
        for name in names:
            lines.append(f"  {label}: {name}")
    lines.append("dry run: nothing written" if dry_run else "imported")
    return "\n".join(lines)


async def run(args: argparse.Namespace, url: str) -> int:
    engine = create_async_engine(url)
    try:
        report = await import_zones(
            engine,
            args.file.read_bytes(),
            source=args.source,
            file_name=args.file.name,
            by=args.by,
            dry_run=args.dry_run,
        )
    except Ed269Error as error:
        print(f"refused, nothing changed: {error}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(describe(report, dry_run=args.dry_run))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.import_zones")
    parser.add_argument("file", type=Path)
    parser.add_argument("--source", required=True, help="the publisher, e.g. GCAA")
    parser.add_argument(
        "--by",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        help="who is importing, recorded in events",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    return asyncio.run(run(args, url))


if __name__ == "__main__":
    sys.exit(main())
