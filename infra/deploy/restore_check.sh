#!/usr/bin/env bash
# Restore the newest backup into throwaway databases and check it (P0-09).
#
# A backup nobody has restored is a hope. This starts two scratch containers
# from the same images as production, on no network, restores both dumps,
# counts rows in the tables that matter, and removes the containers. The
# production databases are not touched.
set -euo pipefail
root=/var/backups/utm
# Directories are named by date (YYYY-MM-DD), so the last in order is newest.
latest=$(find "$root" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
echo "restore_check: $latest"
(cd "$latest" && sha256sum -c SHA256SUMS)

password=$(openssl rand -hex 16)
cleanup() { docker rm -f utm-restore-pg utm-restore-ts >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name utm-restore-pg --network none \
  -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=scratch postgis/postgis:16-3.4 >/dev/null
docker run -d --name utm-restore-ts --network none \
  -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=scratch timescale/timescaledb-ha:pg16 >/dev/null

wait_ready() {
  # The images' entrypoints run a temporary server for initialisation, stop
  # it, and start the real one. Ready is the second "ready to accept
  # connections", not the first.
  for _ in $(seq 1 90); do
    starts=$(docker logs "$1" 2>&1 | grep -c "ready to accept connections" || true)
    if [[ $starts -ge 2 ]] &&
       docker exec "$1" psql -U postgres -d scratch -tAc "select 1" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "restore_check: $1 did not start" >&2
  return 1
}
wait_ready utm-restore-pg
wait_ready utm-restore-ts
# An empty database from template0: the images pre-install extensions into
# their default database (PostGIS its topology schema), which the dumps
# create themselves.
for container in utm-restore-pg utm-restore-ts; do
  docker exec "$container" createdb -U postgres -T template0 restored
done

docker exec -i utm-restore-pg pg_restore -U postgres -d restored --no-owner \
  < "$latest/relational.dump"
docker exec utm-restore-ts psql -U postgres -d restored -qc \
  "CREATE EXTENSION IF NOT EXISTS timescaledb; SELECT timescaledb_pre_restore();" >/dev/null
docker exec -i utm-restore-ts pg_restore -U postgres -d restored --no-owner \
  < "$latest/telemetry.dump"
docker exec utm-restore-ts psql -U postgres -d restored -qc \
  "SELECT timescaledb_post_restore();" >/dev/null

echo "relational:"
docker exec utm-restore-pg psql -U postgres -d restored -tAc \
  "select 'alembic ' || version_num from alembic_version_relational
   union all select 'operators ' || count(*) from operators
   union all select 'airspace_policy ' || count(*) from airspace_policy
   union all select 'events ' || count(*) from events"
echo "telemetry:"
docker exec utm-restore-ts psql -U postgres -d restored -tAc \
  "select 'alembic ' || version_num from alembic_version_telemetry
   union all select 'known_drones ' || count(*) from known_drones
   union all select 'drone_state ' || count(*) from drone_state
   union all select 'remote_id_observations ' || count(*) from remote_id_observations"
echo "restore_check: restored and read back"
