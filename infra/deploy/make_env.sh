#!/usr/bin/env bash
# Write infra/deploy/.env from env.example with fresh secrets. P0-09.
# Refuses to overwrite an existing .env: its passwords are the databases'.
set -euo pipefail
cd "$(dirname "$0")"
if [[ -e .env ]]; then
  echo "make_env: .env exists; not overwriting it" >&2
  exit 1
fi
umask 077
postgres_password=$(openssl rand -hex 24)
timescale_password=$(openssl rand -hex 24)
feed_secret=$(openssl rand -hex 32)
sed \
  -e "s/^POSTGRES_PASSWORD=CHANGE_ME$/POSTGRES_PASSWORD=${postgres_password}/" \
  -e "s/^TIMESCALE_PASSWORD=CHANGE_ME$/TIMESCALE_PASSWORD=${timescale_password}/" \
  -e "s/^FEED_TICKET_SECRET=CHANGE_ME$/FEED_TICKET_SECRET=${feed_secret}/" \
  -e "s/\${POSTGRES_PASSWORD}/${postgres_password}/" \
  -e "s/\${TIMESCALE_PASSWORD}/${timescale_password}/" \
  env.example > .env
if grep -q "=CHANGE_ME" .env; then
  echo "make_env: a CHANGE_ME survived; .env removed" >&2
  rm -f .env
  exit 1
fi
echo "make_env: wrote .env (mode 600)"
