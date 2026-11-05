# Troubleshooting

Symptoms, where to look, what to do. Paths are the host folders from `deploy/.env`; inside the containers they are
`/config`, `/logs`, `/data` and `/telemetry`. `docker compose -f deploy/compose.yaml logs <service>` shows a
service's own output.

## The dashboard

**Every page says "Loading…"**: the first load has not finished. A notice at the top of the page names the source that fails; the
Overview section for a missing source says what is missing. A page that keeps loading after a reload means the
dashboard container itself is not answering: `docker compose logs dashboard`.

**"This page needs settings.json, which cannot be used: invalid json"** (or `secret-like string`, `settings must be
an object`): the file in `CONFIG_DIR` is broken. Honcho config, Runs, Cost and Logs still work. Fix the file by hand
(a copy from before the last write is in `CONFIG_DIR/backups/settings/`), or `python src/config/validate.py` on it
to see what is wrong. `secret-like string` means a value that looks like a key: a key never belongs in this file, a
backend names its environment variable instead.

**Review says the file changed; reload and review the diff again (409)**: another writer saved `settings.json` or
`config.toml` since the page loaded: a second tab, or the scheduler's peer discovery. Reload, stage the edits again.

**A write is refused with a schema error**: the message names the key. The schema is `src/config/settings.schema.json`
and `docs/SETTINGS.md` describes each key's range. A `key_env` must end in `_KEY`; a `base_url` is `http` or
`https`; a model reference must be in the backend's `models` list when it has one.

**The Containers section shows an error, or Restart Honcho does nothing**: the two Docker socket proxies are not
reachable (`docker compose ps`), or the container names differ from the constant `RESTART_ALLOWED` in
`src/dashboard/app/app.py` (compose default names `honcho-api-1`, `honcho-deriver-1`, `honcho-tools-<service>-1`).
A name the proxy does not know is shown as `unknown`. Restart Honcho is also refused with 409 while a model block in
`config.toml` has no `model` or `transport`; the Overview lists the gap.

**Peers or the queue say the workspace or the token is unset**: set `HONCHO_WORKSPACE` in `deploy/.env` and
`HONCHO_API_TOKEN` in Honcho's `.env`, then `docker compose up -d dashboard`.

## The hook

Read `LOGS_DIR/hook-status.json` and the Overview's hook line.

**`hook failed — running stock Honcho`**: `install()` raised in the deriver; the exception class is in the status
file (`hook_failed`) and in the deriver's log. Intake runs, and Honcho dreams on its own because `mode` is `stock`
and no peer or pair is disabled. Usual causes: a Honcho upgrade that renamed a symbol the hook checks, or the old
fork module `src.rerank` still mounted (the message names it). Fix the cause and restart the deriver.

**`hook failed — dreams blocked`**: the same failure, but your settings need the hook (`mode` is `scheduler`, a
peer or pair is `enabled: false`, or `settings.json` cannot be read), so the entrypoint set `DREAM_ENABLED=false`: intake runs, no dream does. Fix the
cause and restart the deriver.

**`hook status missing or unreadable`**: the deriver has not started through the hook's entrypoint, or `LOGS_DIR`
is not the same folder on the deriver and the dashboard. Check the `entrypoint` and the `/logs` mount in Honcho's
compose file against `deploy/honcho-mounts.yaml`.

**`hook settings reload failed`**: the deriver has a last good copy and refused the newer file. `policy-used.log`
has a `policies reloaded` line with the exception class. Fix `settings.json`; the next dream re-reads it.

**`settings saved … apply at the next dream`**: not an error. The hook reads the file when a dream starts, not on a
timer.

**Dreams do not run in scheduler mode** (`gate=skipped-mode` lines): those are Honcho's own triggers, skipped on
purpose. A scheduled dream ends with `gate=ran`. If nothing is queued, read the scheduler section below.

**`gate=skipped-disabled`**: the observer or the observed is `enabled: false` on the Peers page.

**`gate=skipped-settings`**: `settings.json` exists but has never been accepted since the deriver started. Every
dream is skipped until a good file is saved (going on as stock would let Honcho dream on its own in a deployment set
to `scheduler`).

## The scheduler

`LOGS_DIR/dream-nas.log` is its log; the Logs page shows the newest lines.

**`sleeping`**: `mode` is `stock`. Expected.

**`settings.json unreadable; sleeping`**: the file is missing. **`settings.json not applied (<reason>)`**: the file
could not be read (`OSError`) or was refused (bad JSON, a key-shaped string, `peers`, `pairs` or `scheduler` not an object);
the reason is logged once per file version and the previous settings are kept when there are any.

**Nothing is queued in scheduler mode**: the scheduler needs `HONCHO_WORKSPACE`, `HONCHO_API_TOKEN`, the database
(`PGUSER`, `PGDATABASE`, `POSTGRES_PASSWORD`) and both peers of a pair listed and on. A database error is logged as
the driver type and SQLSTATE; a failed schedule call is logged too. A pair whose last dream changed nothing is on a hold
(15 minutes, doubling up to 24 hours); a new conclusion releases it.

**New peers or pairs do not appear**: discovery runs only in `scheduler` mode, and a name with a slash or whitespace is
refused.

## The bridge

`LOGS_DIR/bridge-calls.jsonl` has one line per call; `/health` (port 8793 on the Honcho network) reports
`settings_ok`, `log_ok`, counters and routes.

**Honcho's log shows 401 from the bridge**: `BRIDGE_TOKEN` is set on one side only. Put it in Honcho's `.env`, name
it as `api_key_env` on every routed block (Route all does this), recreate the bridge, restart `api` and `deriver`.

**400 `unknown backend` or `unknown stage alias`**: `settings.json` is missing (the bridge logs `running with
defaults`) or the `providers.stages` entry for that job is absent or names a backend that does not exist. **400
`key env … must end in _KEY`**: rename the variable.

**422**: the bridge gives up on purpose and Honcho's client does not resend: a timeout (the 570-second cap, the slot
wait included), an empty answer, invalid JSON after one repair, a key variable that is not in Honcho's `.env`, a tool-free call without a valid JSON schema, or a CLI that is not installed in the image (`INSTALL_CODEX=1` or
`INSTALL_CLAUDE=1` at build). The message in the call log says which.

**502**: the provider answered with an error, or a CLI failed. The call log has the backend; the provider's own
message is not logged.

**`settings_ok: false` on `/health`**: the file failed to parse; the bridge keeps its last good copy. Fix the file.

**The effort you chose is not what the provider got**: each provider receives the nearest level it documents
(`src/bridge/README.md`, the table); the call log records `effort` (asked) and `sent_effort`. On a routed job, an
effort left in `config.toml` (`thinking_effort` on the routed primary) reaches the bridge as the request's effort and
wins over the Models page; Route all clears it on jobs already routed.

**A fallback did not take over when the bridge was down**: Honcho uses the fallback on its final retry attempt
only, after two waits of a few seconds, and only for a block that has one. The plain-words column on the Honcho
config page says `No fallback` for a job without one.

## Honcho will not start after a config.toml write

Honcho validates `config.toml` at start. The dashboard writes only what the schema accepts, but two cases come from
the page's own logic: `structured_output_mode` on a transport other than `openai` (Honcho refuses it; the page drops
it when a fallback moves to another provider, and Route all drops it from the fallback it writes), and a model block
without `model` or `transport` (Restart Honcho refuses it beforehand). Read Honcho's container log for the field it
names, then either fix it on the Honcho config page, or put back `CONFIG_DIR/backups/config-toml/config.toml.bak_*`
(the newest copy is the file from before the last write) and restart.

**A write failed half-way**: the dashboard writes the backup it took back into `config.toml` before failing, and
answers 500. If the container died during the write instead, the newest `config.toml.bak_*` is the copy to put back.

**Undo routing is disabled ("No copy from before routing")**: the copy is taken by the Write that carries Route all's
edits and deleted by an undo. Without one, `backups/config-toml/` still has a timestamped copy from before every
write.

## Telemetry

`/health` on the receiver (port 8765 on the Honcho network) and `LOGS_DIR/maintenance.log`.

**No new lines in `events-YYYY-MM-DD.jsonl`**: Honcho's `TELEMETRY_ENDPOINT` is not `http://telemetry:8765/events`,
`TELEMETRY_HEADERS` does not carry the same `TELEMETRY_TOKEN` (401 in the receiver's container log), or the Honcho
services were not recreated after the change. The file is per UTC day.

**413 in the receiver's log**: a batch larger than `MAX_BODY_MB` (64 by default). Honcho resends it at every flush
and nothing behind it is delivered: lower Honcho's `TELEMETRY_BATCH_SIZE` or raise the cap.

**503 `low space`**: free space under `MIN_FREE_GB` on `TELEMETRY_DIR`. Posts are refused until space is freed; the
container stays up.

## Backup

`LOGS_DIR/backup.log`, one line per attempt.

**No dump, `password authentication failed` or `database "…" does not exist`**: not retried. Set `POSTGRES_PASSWORD`
in Honcho's `.env`, and `PGUSER`, `PGDATABASE` in `deploy/.env` to your database's names (Honcho's own compose
example uses `postgres` for both), then recreate `backup`.

**`could not connect`**: the backup joins Honcho's network and expects the service name `database`; set `PGHOST` if
yours differs, and `HONCHO_NETWORK` if the network is not `honcho_default`. Transient failures are retried up to 8
times with a doubling wait.

**A dump with no table**: the wrong database; not published, not retried.

**The schedule changed but the run time did not**: `BACKUP_AT`, `TZ` and the keep counts are read at start; recreate
the container.

## Reranking

The **Rerank calls** box names the outcome of every search reranking applied to.

| Outcome | Cause | What to do |
|---|---|---|
| `skipped_missing_key` | `RERANK_API_KEY` not in the api's and deriver's environment | Add it to Honcho's `.env`, recreate `api` and `deriver`. |
| `skipped_missing_url` | provider `tei` without `base_url` | Set it. |
| `skipped_cap` | the daily cap is reached or `0` | Expected; raise the cap if wanted. |
| `skipped_cooldown` | repeated failures paused the provider | Read the earlier failures. |
| `skipped_counter` | a cap is set and Honcho's Redis cache is on but unreachable | Fix Redis, or unset the cap. |
| `timeout`, `http_error` | the provider | Check `timeout_seconds`, the key, the provider status. |
| `malformed_response` | an answer the component refuses, including a server that ignores `top_n` and returns every row | Use a provider format the server honours. |

Nothing above makes a search fail: the vector order is returned. A warning about `ef_search` below `candidates`
means the index returns fewer true nearest rows than the pool asks for; raise `hnsw.ef_search` in `PGOPTIONS` on
`api` and `deriver` (same value on both) and recreate them.

## Tests

**`python -m pytest` fails on one bridge test with `CLI not installed`**: fixed in this version; the test stubs the
executable check. **`tests/measure_pages.py` fails**: it is a load check against a local server and is not in the
default run; it depends on the machine's load. **Everything under `tests/e2e` skips**: expected without a Honcho
3.5.0 tree (`HONCHO_TREE`), Playwright and Chromium.
