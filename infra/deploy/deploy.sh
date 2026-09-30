#!/usr/bin/env bash
# Follow the `deploy` branch (P0-09). Run every minute by utm-deploy.timer.
#
# When origin/deploy has moved: check it out, build, and bring the stack up.
# `migrate` runs before any service starts. If the build fails, nothing is
# restarted and the running version stays. The same commit is not retried
# until deploy moves again; the failure is in the log. One run at a time
# (flock).
set -euo pipefail

# All in one function, called on the last line: bash has then read the whole
# script before `git checkout` replaces this file underneath it.
main() {
  exec 9>/run/utm-deploy.lock
  flock -n 9 || exit 0

  repo=/srv/utm
  log=/var/log/utm-deploy.log
  state=/var/lib/utm-deploy
  mkdir -p "$state"
  cd "$repo"

  git fetch -q origin deploy
  target=$(git rev-parse origin/deploy)
  last=$(cat "$state/last-attempt" 2>/dev/null || true)
  [[ "$target" == "$last" ]] && exit 0
  echo "$target" > "$state/last-attempt"

  {
    echo "[$(date -u +%FT%TZ)] deploying $target"
    git checkout -q --force "$target"
    cd infra/deploy
    if docker compose -f docker-compose.prod.yml build --quiet; then
      docker compose -f docker-compose.prod.yml up -d --remove-orphans
      echo "$target" > "$state/deployed"
      echo "[$(date -u +%FT%TZ)] deployed $target"
    else
      echo "[$(date -u +%FT%TZ)] build failed for $target; the running version stays"
    fi
  } >> "$log" 2>&1
}

main "$@"; exit
