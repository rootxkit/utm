"""archive_segments index for the retention listing

Revision ID: 0008_archive_retention_index
Revises: 0007_remote_id_serial_match
Create Date: 2026-09-30

S-07: retention now lists segment files past their age with

    SELECT relative_path, ... FROM archive_segments s
    WHERE deleted_at IS NULL AND stored_at < :cutoff
      AND NOT EXISTS (SELECT 1 FROM archive_segments n
                      WHERE n.relative_path = s.relative_path
                        AND n.deleted_at IS NULL AND n.stored_at >= :cutoff)
    GROUP BY relative_path, station_id, epoch
    [HAVING (min(stored_at), relative_path) > (?, ?)]
    ORDER BY min(stored_at), relative_path LIMIT ?

and, under the station lock, re-reads one path's live rows by
`relative_path`. The table has one row per stored batch - about 860k per
station per day at 100 ms batches - and nothing indexed `stored_at` or
`relative_path`, so both were full scans of every live row.

Two partial indexes, both `WHERE deleted_at IS NULL` so rows retention has
already marked cost nothing:

- `archive_segments_live_by_stored_at` on `(stored_at)`: the candidate scan.
  In the steady state `stored_at < cutoff` matches only what has aged past
  the cutoff since the previous pass, an hour's worth, not the window's.
- `archive_segments_retention_by_path` on `(relative_path, stored_at)`: one
  probe per candidate path for a newer live row, and the re-read under the
  lock.

Plain `CREATE INDEX`, not `CONCURRENTLY`. This repository's `env.py` runs
every migration inside `context.begin_transaction()` over an asyncpg
connection via `run_sync`, and `CREATE INDEX CONCURRENTLY` cannot run inside
a transaction; Alembic's `autocommit_block` is the usual escape, but it
depends on the connection's autocommit semantics, which the asyncpg adapter
does not offer through `run_sync` reliably. The table is modest on staging
today (a few days of a handful of stations), so the write lock a plain build
takes is seconds. On a production table already holding months, build the
index by hand with `CONCURRENTLY` before applying this revision; the
migration is idempotent to that (`IF NOT EXISTS`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_archive_retention_index"
down_revision: str | None = "0007_remote_id_serial_match"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BY_PATH = "archive_segments_retention_by_path"
BY_STORED_AT = "archive_segments_live_by_stored_at"


def upgrade() -> None:
    op.create_index(
        BY_PATH,
        "archive_segments",
        ["relative_path", "stored_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
        if_not_exists=True,
    )
    op.create_index(
        BY_STORED_AT,
        "archive_segments",
        ["stored_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(BY_STORED_AT, table_name="archive_segments", if_exists=True)
    op.drop_index(BY_PATH, table_name="archive_segments", if_exists=True)
