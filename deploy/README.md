# deploy

The stack's compose file, the `.env` template and the lines to add to Honcho's own compose file.

| File | What it is |
|---|---|
| `compose.yaml` | The `honcho-tools` compose project: telemetry, backup, scheduler, dashboard (port 8010), bridge (network only), docker-proxy and docker-proxy-action. |
| `.env.example` | Copy to `deploy/.env`: the folders, ports and names. No secret. |
| `honcho-mounts.yaml` | The lines to add to Honcho's `api` and `deriver` services, once. Not a compose project. |

Containers get compose's default names, `honcho-tools-<service>-1`.

## Folders

`deploy/.env` names the folders. Relative paths are read from this `deploy/` folder; the defaults are the live folders
beside `src/` at the repository root. Compose mounts each at a fixed path inside the containers, and the code's
defaults are those paths, so no other path setting is needed.

| Variable | Default | Mounted at | Services | Holds |
|---|---|---|---|---|
| `CONFIG_DIR` | `../config` | `/config` | dashboard (read-write), scheduler (read-write), bridge (read-only); Honcho's api and deriver (read-only, through `honcho-mounts.yaml`) | `settings.json`, `models.json`, `rates.json`, and `backups/` (the copies taken before every save). |
| `HONCHO_ROOT` | none | `/honcho/config.toml` (dashboard only, one file, read-write) | dashboard; every service loads its `.env` through `env_file` | Honcho's project folder. Only `.env` and `config.toml` are used. |
| `DATA_DIR` | `../data` | `/data` | dashboard; the deriver mounts `instructions/` read-only | `instructions/`. Nothing else. |
| `LOGS_DIR` | `../logs` | `/logs` | every service; the deriver through `honcho-mounts.yaml` | Every log of the stack. |
| `TELEMETRY_DIR` | `../telemetry` | `/data` (telemetry), `/telemetry` (dashboard, read-only) | telemetry, dashboard | The event files. |
| `BACKUP_DIR` | `../backups` | `/backups` | backup | The database dumps. |
| `SECRETS_DIR` | `../cli-secrets` | `/home/node/.codex`, `/home/node/.claude` | bridge; every service and Honcho's api and deriver load its `tokens.env` through `env_file` | `tokens.env` (written by `setup.sh`) and the Codex and Claude CLI logins (`codex/`, `claude/`). Sensitive. |

Mount the config folder, never `settings.json` alone. A bind of the file keeps the old inode after a temp-file rename,
and hot reload stays on the previous file until the container is recreated. `config.toml` is the exception: the
dashboard writes it in place (no rename), so the single-file bind sees every change, and Honcho keeps reading it from
its own folder.

The scheduler mounts the config folder read-write because peer discovery appends newly seen peers to `settings.json`.
Before each such write it copies the previous file to `backups/settings/settings.json.bak-<stamp>`, the same folder the
dashboard uses, and keeps the newest 20 of its own copies.

Inside the containers `CONTROL_FILE` is `/config/settings.json`, set in the compose `environment:` block, not in
`.env`. `DATA_DIR`, `TELEMETRY_DIR`, `MAINTENANCE_LOG` and `BACKUP_LOG` are set there too where a service reads them,
because `env_file` also passes in the host paths under the same names.

Every service lists `${HONCHO_ROOT}/.env` first in `env_file`, then `${SECRETS_DIR}/tokens.env`, then `deploy/.env`.
The Honcho project's `.env` holds Honcho's secrets and the provider keys (`POSTGRES_PASSWORD`, `HONCHO_API_TOKEN`,
`CONTROL_PASSWORD`, `RERANK_API_KEY`, provider keys); `tokens.env` holds the stack's tokens (`BRIDGE_TOKEN`,
`TELEMETRY_TOKEN`, `TELEMETRY_HEADERS`), written once by `setup.sh`; `deploy/.env` holds paths, ports and names only.
Compose reads all three on the host, so no key becomes a file inside a container. This compose file has no secret values.

## What each container sees

`env_file` cannot pass part of a file, so every container is created with the whole Honcho `.env`. What each process
keeps of it differs:

| Service | Secrets it needs | What its process holds |
|---|---|---|
| telemetry | `TELEMETRY_TOKEN` (`VIEWER_KEY` if set) | Only the variables `receiver.py` reads, plus `PATH`, `HOME`, locale, `TZ`, `PYTHON*`, `SSL_CERT_*` and the proxy variables: at start it replaces itself with a process that has just those. |
| backup | `POSTGRES_PASSWORD` (or `PGPASSWORD`) | Only `PG*`, `POSTGRES_PASSWORD`, `BACKUP_*`, `KEEP_DAILY`, `KEEP_WEEKLY`, `TZ`, `PATH`, `HOME` and locale: `backup.sh` starts again with just those, so `pg_dump` never holds a provider key. |
| scheduler | `HONCHO_API_TOKEN`, `POSTGRES_PASSWORD` | The variables `dream_nas.py` reads, `PG*` (libpq settings) and the generic names above, the same way. |
| dashboard | `HONCHO_API_TOKEN` | The whole file. It locks every `config.toml` field that an environment variable overrides, and it learns which by the variable names it has, so it needs Honcho's names; it never shows a value. |
| bridge | the provider keys named by `settings.json` `key_env`, `BRIDGE_TOKEN` | The whole file: `key_env` names a variable ending in `_KEY`, and a new backend must work without a compose change. The CLIs it starts get its environment without any name ending in `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIALS` or `_URI`. |

The trade-off: the filtering protects against code running inside a container reading its own environment (a bug in
the receiver's input handling, a hostile answer to `pg_dump`). It does not change what Docker stores: `docker inspect`
still shows the whole file for every container, and so does the read proxy (below), which the dashboard can call. Per
service secret files (`env_file` entries or Docker secrets with only the needed lines) would close that too, at the
cost of keeping several copies of the same values in step; that is a deployment change and is not made here. Restore
runs (`docker compose run ... backup`, see `src/backup/restore.md`) are one-off containers and keep the whole file.

## The Docker socket

Only `docker-proxy` and `docker-proxy-action` mount `/var/run/docker.sock`. The `:ro` on that mount does not make the
Docker API read-only (a write is a request on the socket, not a write to the file); the proxy's rules are the limit.

| Proxy | Settings | Allows | The dashboard uses |
|---|---|---|---|
| `docker-proxy` | `CONTAINERS=1`, `POST=0` | Every `GET` under `/containers`: the list, inspect (each container's configuration, including its environment, so every secret given to any container on the host), logs, `archive` and `export` (files of a container). | `GET /containers/json` and `GET /containers/<name>/json` (only the `PGOPTIONS` entry of `Config.Env` is read). |
| `docker-proxy-action` | `POST=1`, `CONTAINERS=0`, `ALLOW_RESTARTS`, `ALLOW_START`, `ALLOW_STOP` | `restart`, `start` and `stop` of any container on the host, by name or id (by the image's documentation `ALLOW_RESTARTS` also covers `kill`; not checked here). | `POST /containers/<name>/restart`, `start`, `stop`. |

Both proxies see every container on the host; which names the dashboard acts on is its own constant list
(`RESTART_ALLOWED`). `EVENTS`, `PING` and `VERSION` are off. Neither publishes a port; only the dashboard shares their
internal network `restart`. The image's environment switches cannot narrow `CONTAINERS=1` to the two paths the
dashboard needs: that needs a proxy with per-path rules, a change to test on the host before relying on it. Until then,
whoever can reach the dashboard can, through a dashboard bug, reach what the read proxy allows, which is why the
dashboard should not be reachable from untrusted networks (`BIND_IP`).

## Stopping one service

Each service can be stopped (`docker compose stop <service>`) without stopping or breaking another; none has
`depends_on`, `links`, `volumes_from` or a shared network namespace. What stops working meanwhile:

| Stopped | Effect | Others |
|---|---|---|
| dashboard | No page, no edits, no restart buttons. | Honcho, the hook and rerank keep reading `settings.json`; the scheduler, bridge, telemetry and backup run as before. The proxies sit idle. |
| scheduler | In `scheduler` mode no dream is queued and new peers and pairs are not discovered (the hook still skips Honcho's own triggers in that mode, so no dream runs at all). In `stock` mode nothing changes. | The dashboard shows the last scheduler log lines; everything else is unaffected. |
| telemetry | Honcho's events are not received. Honcho keeps up to `TELEMETRY_MAX_BUFFER_SIZE` events in memory and sends them when the receiver is back; past that it drops the oldest. Honcho's own calls do not fail. | The dashboard's Runs, Cost and Rerank call pages show what is already on disk. |
| backup | No nightly dump. | Nothing else uses it. |
| bridge | A Honcho model call that points at the bridge fails after Honcho's retries, unless the job's block has a Honcho-native `fallback` (Route all on the Honcho config page writes one from the old primary): then the final retry attempt completes on that model. Dreams and any stage without a fallback fail until the bridge is back. Not a stack dependency, but a Honcho one. | The dashboard's bridge status shows it unreachable; the rest of the stack is unaffected. |
| docker-proxy, docker-proxy-action | The dashboard cannot list containers, read the containers' `PGOPTIONS` (it falls back to its own, else shows `ef_search` as `unknown`) or restart; it answers with an error. | Nothing else uses them. |

## Install check: the Honcho-native fallback

After Route all jobs through the bridge and the restart, check once that a job survives a bridge outage:

1. `docker compose stop bridge`.
2. Run one summary or dialectic call (a session summary, or a question to a peer through Honcho's API).
3. Confirm it completes on the "Honcho native" fallback: the answer arrives after Honcho's retries (two waits of a few seconds), and the dashboard's Runs page or Honcho's log shows the fallback model rather than the bridge. A job whose plain-words column on Honcho config reads "No fallback" fails instead; give it one there.
4. `docker compose start bridge`.

## Services start alone

No service has `depends_on` on another. The dashboard and the bridge start without `settings.json` (they log one line
and use their defaults until the file appears) and without the scheduler. The scheduler starts without `settings.json`,
the database or the API, and sleeps until they are there. The tests in `tests/test_stack_layout.py` check each case.

## Services

- **scheduler**: built from `../src/scheduler/Dockerfile` (`python:3.12-slim`, direct packages in
  `../src/scheduler/requirements.txt`: `httpx` and `psycopg[binary]`
  installed once); `dream_nas.py` is bind-mounted, so a script edit needs no rebuild.
- **dashboard**: built from `../src/dashboard/Dockerfile`, on `${BIND_IP}:8010`. The shipped templates
  (`../src/config`) are mounted read-only at `/templates`. See `../src/dashboard/README.md`. After a crash during a
  dashboard write of Honcho's `config.toml`, the newest `config.toml.bak_*` in the config-toml backup folder
  (`backups/config-toml/` in `CONFIG_DIR`) is the copy to put back.
- **bridge**: built from `../src/bridge/Dockerfile` (`node:22-slim` plus `python3` and `git`; the `codex` and
  `claude` CLIs at the latest version only when `INSTALL_CODEX` or `INSTALL_CLAUDE` is `1`); `bridge_cli.py` is bind-mounted. Not published on the host: Honcho reaches it as
  `http://bridge:8793/v1` on the Honcho network. See `../src/bridge/README.md`.
- **telemetry**: `python:3.12-slim` (standard library only) running `../src/telemetry/receiver.py`. See `../src/telemetry/README.md`.
- **backup**: `pgvector/pgvector:pg15` running `../src/backup/backup.sh`. Not pinned on its own: it follows the tag of
  Honcho's `database` service, so `pg_dump` and `pg_restore` match the server major. See `../src/backup/README.md`.
- **docker-proxy** (read-only listing) and **docker-proxy-action** (restart, start and stop only; the image denies every
  non-GET unless `POST=1`, and `POST=1` with `CONTAINERS=1` would also allow create and exec) are the only services that
  mount the Docker socket (see "The Docker socket" for what each one allows). They publish no ports and join only the
  internal network `restart`. `dashboard` keeps `honcho` and also joins `restart`. Which containers the dashboard may act on is a
  constant in `../src/dashboard/app/app.py` (`RESTART_ALLOWED`).

## Honcho's compose file

`honcho-mounts.yaml` lists what to add to Honcho's `api` and `deriver` services: the two rerank entrypoints (the
deriver's chains the policy hook), `CONFIG_DIR` at `/config` read-only, the `src/rerank/honcho_rerank` and `src/hooks`
mounts, `LOGS_DIR` at `/logs` with `POLICY_LOG` and `POLICY_STATUS` for the hook, `DATA_DIR/instructions` at
`/data/instructions`, and `PGOPTIONS` on both. It is a one-time change. The dashboard reads `ef_search` from the
containers' `PGOPTIONS` through the read-only proxy, else its own `PGOPTIONS`; unreadable shows as unknown.
