# Setup

Installing Honcho Tools beside an existing Honcho 3.5.0 deployment, from an empty checkout to a dashboard that routes
Honcho's model calls through the bridge. Each step ends with what to check. The whole thing is one compose project
(`deploy/compose.yaml`) plus a one-time edit of Honcho's own compose file.

Every path below is a placeholder. Nothing in the code assumes a host, a share or a user. Container and network
names follow a Honcho compose project named `honcho` and this stack's project `honcho-tools` (`RESTART_ALLOWED`),
and the network default is `honcho_default`; see `docs/TROUBLESHOOTING.md` when yours differ.

## Before you start

- A host that runs Honcho 3.5.0 with Docker Compose v2. The stack joins Honcho's compose network (by default
  `honcho_default`) so the containers reach `api`, `deriver` and `database` by their service names.
- Honcho's project folder, called `HONCHO_ROOT` here. Only two of its files are used: `.env` (Honcho's secrets and the
  provider keys, loaded on the host by compose) and `config.toml` (bound as one file into the dashboard).
- Honcho's compose network name (`docker network ls`; the default is `<project>_default`).
- The names your Honcho database uses: Honcho's own compose example uses user and database `postgres`; the stack's
  defaults are `honcho` and `honcho` (`PGUSER`, `PGDATABASE` in `deploy/.env`).
- Optional: an API key per provider you will route to (OpenRouter, DeepSeek, GLM, Anthropic, ...), a Voyage, Cohere or
  Jina key for the reranker, and a Codex or Claude Code subscription for the CLI backends.

## 1. Check out and prepare the files

On the Docker host, clone the repository beside Honcho's project folder (the folder with Honcho's compose file and
`.env`) and run the setup script:

```bash
git clone <repository-url> honcho-tools
cd honcho-tools
sudo ./setup.sh                    # Honcho's folder is ../honcho
sudo ./setup.sh /path/to/honcho    # Honcho's folder is elsewhere
```

`setup.sh` only writes files, and keeps any that already exist:

1. `deploy/.env` from `deploy/.env.example`, with `HONCHO_ROOT` set to Honcho's folder.
2. `config/settings.json` from `src/config/settings.example.json`, and `config/rates.json` from
   `src/config/rates.example.json` (public list prices for the Cost page).
3. `data/instructions/`, `cli-secrets/codex/` and `cli-secrets/claude/` if missing, and the logs and `cli-secrets`
   folders owned by uid 1000, the user the bridge runs as (needs `sudo`; without it the script prints the
   `chown` command).
4. The lines for step 5, printed with this host's paths in place of `<CODE_ROOT>`, `<CONFIG_DIR>`, `<LOGS_DIR>` and
   `<DATA_DIR>`.
5. `cli-secrets/tokens.env` (in `SECRETS_DIR`): `BRIDGE_TOKEN`, `TELEMETRY_TOKEN` and the matching
   `TELEMETRY_HEADERS`, new random values unless Honcho's `.env` already has them (then the same values). Every
   service of this stack loads it, and so do Honcho's `api` and `deriver` through the `env_file` lines of step 5, so
   Honcho's `.env` is never edited.

It builds nothing, starts nothing, and never edits Honcho's files.

**By hand, or with other paths**: copy the three files yourself, set `HONCHO_ROOT` (and any folder you want
elsewhere) in `deploy/.env`, run `sudo chown -R 1000:1000 logs cli-secrets` (or the folders you chose), and
fill in the placeholders of `deploy/honcho-mounts.yaml` yourself in step 5:

```bash
cp deploy/.env.example deploy/.env
cp src/config/settings.example.json config/settings.json
cp src/config/rates.example.json config/rates.json
```

The six folders beside `src/` (`config`, `data`, `logs`, `telemetry`, `backups`, `cli-secrets`) ship empty; git
ignores what the stack writes into them. Each is a variable in `deploy/.env` (`CONFIG_DIR`, `DATA_DIR`,
`LOGS_DIR`, `TELEMETRY_DIR`, `BACKUP_DIR`, `SECRETS_DIR`) and can live anywhere; relative paths are read from the
`deploy/` folder.

`settings.json` starts as the example: `mode` is `stock` (Honcho dreams on its own, the scheduler sleeps), reranking
is off, and the backends are examples to edit. Honcho behaves as before until you change something.

Check: `python src/config/validate.py config/settings.json` exits 0 (it needs `jsonschema`:
`pip install -r requirements-dev.txt`).

## 2. Fill in `deploy/.env`

Open `deploy/.env`. Only `HONCHO_WORKSPACE` must be set; the rest have working defaults:

| Variable | Set it to |
|---|---|
| `HONCHO_ROOT` | Honcho's project folder (absolute path). `setup.sh` fills it in. |
| `SECRETS_DIR` | `tokens.env` (written by `setup.sh`) and the bridge's CLI logins (`codex/` and `claude/` inside it). Default `cli-secrets/`, shipped beside `src/`; its contents are ignored by git. |
| `HONCHO_NETWORK` | Honcho's compose network, if it is not `honcho_default`. |
| `HONCHO_WORKSPACE` | The Honcho workspace the scheduler and the dashboard work on. One per stack: in `scheduler` mode, other workspaces do not dream (see the README, Known limitations). |
| `BIND_IP` | Which of the host's addresses the dashboard listens on. Empty: all of them. You open the page at `http://<host address>:8010` either way. |
| `PGUSER`, `PGDATABASE` | Your database's user and name, if they are not `honcho`. |
| `INSTALL_CODEX`, `INSTALL_CLAUDE` | `1` to install that CLI in the bridge image at build time; `0` leaves it out. |

Everything else has a working default. The file holds paths, ports and names only; no secret goes here.

## 3. Put the secrets in Honcho's `.env`

Add to `${HONCHO_ROOT}/.env` (never to `deploy/.env` or the compose file):

```
TELEMETRY_TOKEN=<random token>          # only without setup.sh's tokens.env; python -c "import secrets; print(secrets.token_urlsafe(32))"
CONTROL_PASSWORD=<dashboard password>    # optional; the page is open without it
HONCHO_API_TOKEN=<Honcho API token>      # Honcho API calls from the scheduler (queues dreams) and the dashboard
OPENROUTER_API_KEY=<key>                 # one per backend named in settings.json (key_env), names end in _KEY
RERANK_API_KEY=<key>                     # only when you turn the reranker on with a hosted provider
BRIDGE_TOKEN=<random token>              # only without setup.sh's tokens.env; the bridge refuses calls without it when set
```

`POSTGRES_PASSWORD` is already there: Honcho uses it, and so do the backup and the scheduler. Compose reads this file
on the host, so no key becomes a file inside a container.

Check: `deploy/.env.example` ends with the list of every name the stack reads from this file.

## 4. Build and start the stack

```bash
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml ps
```

Seven containers come up, named `honcho-tools-<service>-1`: `telemetry`, `backup`, `scheduler`, `dashboard`,
`bridge`, `docker-proxy`, `docker-proxy-action`. None depends on another; each starts without `settings.json` and
without the others, and each can be stopped alone (`deploy/README.md`, "Stopping one service").

Check:

- `telemetry` and `bridge` are healthy (they have healthchecks). The dashboard answers on `http://<BIND_IP>:8010`.
- `logs/dream-nas.log` has a `=== start` line from the scheduler and then `sleeping` (stock mode).
- `logs/backup.log` names the zone and the next run.

## 5. Connect Honcho: the one-time compose change

Honcho's `api` and `deriver` services need the lines in `deploy/honcho-mounts.yaml`: the two entrypoints, the config
folder mounted read-only at `/config`, the rerank package and the hook mounted read-only, the logs folder, the
instructions folder, and `PGOPTIONS`. The file is a template with `<CODE_ROOT>`, `<CONFIG_DIR>`, `<LOGS_DIR>` and
`<DATA_DIR>` to replace by the host folders you chose; it is not a compose project of its own.

Two choices in that file:

- **With the reranker** (default): the entrypoints are `python -m src.honcho_rerank.api_main` on `api` and
  `python -m src.honcho_rerank.deriver_main /app/policies/deriver_main.py` on `deriver`. Reranking stays off until
  `settings.json` turns it on; the mounts cost nothing meanwhile.
- **Hook only**: the deriver entrypoint is `python /app/policies/deriver_main.py` (the "Hook without rerank" block).
  The dream hook then runs without the rerank proxy.

Then point Honcho's telemetry at the receiver, on both services (`src/telemetry/README.md`). `TELEMETRY_HEADERS`
comes from `tokens.env`; without it, add it here too (`'{"Authorization": "Bearer ${TELEMETRY_TOKEN}"}'`):

```yaml
    TELEMETRY_ENDPOINT: "http://telemetry:8765/events"
```

Recreate `api` and `deriver` (a changed compose file needs `docker compose up -d`, not a restart). Do it when the dream
queue is empty: a dream in flight is cut.

Check:

- `logs/hook-status.json` exists with `"checks_passed": true`, and the dashboard's Overview says `hook active`.
- A new line appears in `telemetry/events-YYYY-MM-DD.jsonl` (UTC day) once Honcho handles a request.
- `logs/policy-used.log` has no `FATAL` line from the hook, and Honcho's container logs have no `rerank hook not
  installed` error. If the hook could not install, Honcho still
  runs: the Overview says `hook failed — running stock Honcho` or `hook failed — dreams blocked`
  (`docs/TROUBLESHOOTING.md`).

## 6. Open the dashboard

`http://<BIND_IP>:8010`, with the login from `CONTROL_PASSWORD` when set (user `admin`, or `CONTROL_USER`). The
Overview shows who starts dreams, the workspace switches, the scheduler box and the health of each piece; a section
whose data source is missing says so and the rest of the page works. The login is HTTP basic over plain HTTP: keep the
port on a trusted network or behind a TLS proxy.

Check: the Overview shows the hook line and the scheduler state, and the status bar at the bottom reports the
containers.

## 7. Describe your backends

On the **Models** page, the **Dreams fallback** row picks the backend and model used when no stage provider is set
(`providers.active`); **Add a model to the list** adds a model to the catalogue (`models.json`). The backends
themselves (`providers.backends`) are the example's: a Codex CLI, a Claude Code CLI, OpenRouter, DeepSeek, a proxy,
GLM and a disabled Anthropic API backend. Edit `config/settings.json` to rename, remove or add one (the shape is in `docs/SETTINGS.md`); the dashboard
checks the file against the schema and refuses a backend whose `key_env` does not end in `_KEY`.

For a CLI backend the login lives in `SECRETS_DIR`, mounted into the bridge: `claude/` is written by `claude login`
run once inside the container (`docker compose -f deploy/compose.yaml exec bridge claude login`), `codex/` holds the
Codex CLI's `auth.json`. The CLIs must have been built into the image (`INSTALL_CODEX`, `INSTALL_CLAUDE`); a call to
a CLI that is not installed answers 422 and names the build argument.

Check: `docker compose -f deploy/compose.yaml exec bridge python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8793/health').read().decode())"`
shows `settings_ok: true` and the backends. With `BRIDGE_TOKEN` set, the full answer needs the token; without it only
`ok`, `settings_ok` and `log_ok` are shown.

## 8. Route Honcho's jobs through the bridge

On the **Honcho config** page, click **Route all jobs through the bridge**, then **Review** and **Write**, then
**Restart Honcho**. For every job that still uses a Honcho-native model (intake, summaries, the five dialectic levels,
dream deduction and induction), the current model moves into Honcho's stock `fallback` table and the primary becomes
the bridge with a `@<stage>` alias; `settings.json` gets a `providers.stages` entry per job naming the model the
bridge should call. A job that already has its own fallback keeps it. The exact mechanics are in
`docs/HONCHO-CONFIG.md` and `docs/STAGE-ROUTING.md`.

From then on, a model or per-job effort pick on the **Models** page writes only `settings.json` and applies at the
next call, without a restart. With `BRIDGE_TOKEN` set, the routed blocks name it as `api_key_env`, so Honcho sends it.

Check:

- `logs/bridge-calls.jsonl` gains one line per call, with the backend that answered.
- The install check in `deploy/README.md`: stop the bridge, run a summary or dialectic call, confirm it completes on
  the Honcho-native fallback, start the bridge again.

## 9. Choose who starts dreams

Still on Overview, **Who starts dreams**: `stock` leaves Honcho's own triggers; `scheduler` makes the scheduler queue
dreams for the pairs in `settings.json` and the hook skip every dream Honcho starts by itself. In that mode the
scheduler discovers peers from the database and appends them as `enabled: false` (the **Peers** page can also add a
name the API knows); turn on the ones you want there, and set per-pair limits on **Pairs**. `docs/OPERATIONS.md`
covers the daily use.

Check: after a scheduled dream, `logs/policy-used.log` ends a line with `mode=scheduler ... gate=ran`, and
`logs/runs.log` has one JSON record for it.

## 10. Turn the reranker on (optional)

Set `RERANK_API_KEY` in Honcho's `.env` (recreate `api` and `deriver` once so they hold it), then on the **Rerank**
page switch **Global** on, pick the provider and model, and set a `daily_request_cap` if you want a hard ceiling. The
change is hot. Confirm `PGOPTIONS` on `api` and `deriver` sets `hnsw.ef_search` at least as high as `candidates`
(`src/rerank/README.md`, "Scan mode"); the page warns when it can read a lower value.

Check: the **Rerank calls** box on that page counts `reranked` outcomes once searches run; `skipped_missing_key`
means the key did not reach the containers.

## Upgrading, uninstalling

Upgrading Honcho: re-apply the optional patch, keep the mount lines, regenerate the two schema files
(`README.md`, "Upgrading Honcho"). Uninstalling: remove the lines from Honcho's compose file and recreate `api` and
`deriver` (Honcho is stock again; `config.toml` keeps what the dashboard wrote, so undo the routing first on the
Honcho config page if the bridge is going away), then `docker compose -f deploy/compose.yaml down`. The live folders
and `SECRETS_DIR` are yours to keep or delete.
