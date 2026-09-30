# Staging server

P0-09. The system on the owner's DigitalOcean droplet (Frankfurt, 2 vCPU,
4 GB), under Docker Compose, from `infra/deploy/`.

| Address | What |
|---|---|
| `https://utm.chikox.net` | Operator console (`/app`), sign-in, replay, API |
| `wss://ingest.chikox.net/relay/v1` | relay-v1: stations deliver telemetry here |

Only Caddy listens publicly, on 80 and 443, and it obtains and renews the
certificates itself. Both databases, Redis and NATS are on the internal
Compose network with no published ports.

## Layout on the server

```
/srv/utm                      a clone of the repository at the deployed commit
/srv/utm/infra/deploy/.env    secrets, written once by make_env.sh (mode 600)
/srv/utm/infra/deploy/secrets/gateway.tokens   station tokens (uid 10001, mode 400)
/srv/utm/infra/deploy/data/   basemap, terrain tiles, geoid (read-only mounts)
/var/backups/utm/<date>/      nightly dumps, 14 days
```

## Deploying: merge to main

1. A merge to `main` runs CI.
2. When CI passes, `.github/workflows/deploy.yml` moves the `deploy`
   branch to that commit. It only moves forward.
3. On the server, `utm-deploy.timer` runs `infra/deploy/deploy.sh` every
   minute. When `origin/deploy` has moved, the script checks it out,
   builds, and brings the stack up. `migrate` applies both migration trees
   first, and every service waits for it.
4. If the build fails, nothing restarts and the running version stays.

Log: `/var/log/utm-deploy.log`. The deployed commit is in
`/var/lib/utm-deploy/deployed`.

The server reads the repository with a **read-only deploy key**
(`/root/.ssh/utm_deploy_github`, registered on the repository under
Settings → Deploy keys). GitHub holds no credential for the server: the
server pulls.

By hand, for the same result:

```
cd /srv/utm && git fetch origin deploy && git checkout --force origin/deploy
cd infra/deploy && docker compose -f docker-compose.prod.yml up -d --build
```

## First start, once

```
./make_env.sh                                    # .env with fresh passwords
echo "tbilisi-base-1: $(openssl rand -hex 32)" > secrets/gateway.tokens
chown 10001 secrets/gateway.tokens && chmod 400 secrets/gateway.tokens
```

The station gets the token in its relay configuration (`token_path`), with
`gateway_url = "wss://ingest.chikox.net/relay/v1"`.

An operator account (the password is typed at the prompt, never in a
command line):

```
docker compose -f docker-compose.prod.yml exec api python tools/operators.py create-admin <name>
```

## Backups

`/etc/cron.d/utm-backup` (from `utm-backup.cron`):

- `backup.sh` every night at 02:30 UTC. It writes a custom-format `pg_dump`
  of each database, checksums the dumps and keeps them 14 days.
- `restore_check.sh` every Sunday. It restores the newest dumps into two
  throwaway containers with no network and reads the tables back. It never
  touches production.

Log: `/var/log/utm-backup.log`. The dumps live on the droplet only. Copying
them off it (DigitalOcean Spaces) is still to do, and until then a lost
droplet loses its backups.

## Security settings (the owner applies these)

```
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/; s/^#\?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sshd -t && systemctl reload ssh
```

Before the second one, check that key login works. The DigitalOcean web
console works either way.
