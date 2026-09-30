#!/usr/bin/env bash
# Nightly dump of both databases (P0-09). Run by /etc/cron.d/utm-backup.
#
# Custom-format pg_dump of each database into /var/backups/utm/<date>/,
# written under a temporary name and renamed only when complete, so a dump
# that died half way is never mistaken for a backup. Kept 14 days.
# restore_check.sh restores the newest into scratch databases.
set -euo pipefail
cd "$(dirname "$0")"
compose=(docker compose -f docker-compose.prod.yml)
root=/var/backups/utm
day=$(date -u +%F)
dir="$root/$day"
mkdir -p "$dir"
chmod 700 "$root"

dump() {  # service, user variable, database variable, file name
  local service=$1 user_var=$2 db_var=$3 name=$4
  "${compose[@]}" exec -T "$service" sh -c \
    "pg_dump -U \"\$$user_var\" -d \"\$$db_var\" -Fc" > "$dir/$name.part"
  mv "$dir/$name.part" "$dir/$name"
}

dump postgres POSTGRES_USER POSTGRES_DB relational.dump
dump timescale POSTGRES_USER POSTGRES_DB telemetry.dump
sha256sum "$dir"/*.dump > "$dir/SHA256SUMS"
find "$root" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
echo "backup: $(du -sh "$dir" | cut -f1) in $dir"
