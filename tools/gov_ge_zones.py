"""Convert airspace.gov.ge's zone files to an ED-269 document. U-03.

    python tools/gov_ge_zones.py --points points.js --page index.html \\
        --rules gov_ge_rules.toml --out zones-ed269.json

airspace.gov.ge serves no data feed, only a Leaflet page whose zones are
JavaScript variables (`airspace/gov_ge.py` says what was found). Save
`/Airspace/leaflet/zone/points.js` and the page from a browser, write the
rules file with the authority (the restriction, limits and times of each
kind of zone, which the site does not publish), and convert. The output is
an ordinary ED-269 file: import it through the console or
`POST /airspace/zones/import?dry_run=true` first, then without the dry run.

This reads local files only; it never fetches the site. A refusal names
each zone and why, and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from airspace.ed269 import Ed269Error
from airspace.gov_ge import Rules, parse_circle_radii, parse_points, to_ed269


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--points", type=Path, required=True, help="points.js")
    parser.add_argument(
        "--page", type=Path, required=True, help="the page, for circle radii"
    )
    parser.add_argument(
        "--rules", type=Path, required=True, help="the authority's rules (TOML)"
    )
    parser.add_argument("--out", type=Path, required=True, help="the ED-269 file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rules = Rules.from_mapping(
            tomllib.loads(args.rules.read_text(encoding="utf-8"))
        )
    except KeyError as error:
        print(f"refused: the rules file has no {error}", file=sys.stderr)
        return 2
    try:
        document = to_ed269(
            parse_points(args.points.read_text(encoding="utf-8")),
            parse_circle_radii(args.page.read_text(encoding="utf-8")),
            rules,
        )
    except Ed269Error as error:
        print("refused, nothing written:", file=sys.stderr)
        for problem in error.problems:
            print(f"  {problem.field}: {problem.reason}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(f"wrote {len(document['features'])} zones to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
