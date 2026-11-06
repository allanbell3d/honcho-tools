# Architecture

```
CODE (src/)                 CONFIG (CONFIG_DIR, /config)            DATA (DATA_DIR, /data)
  scheduler  ── rw ──┐        settings.json, models.json,             instructions/
  dashboard  ── rw ──┼──────  rates.json, backups/                  LOGS (LOGS_DIR, /logs)
  bridge     ── ro ──┤        + HONCHO_ROOT/config.toml (dashboard,   every log of the stack
  hooks, rerank ─ ro ┘          one file bind at /honcho/config.toml)
                       (hooks and rerank are mounted into Honcho's api and deriver)
  telemetry  ── POST /events ◄── Honcho api + deriver (TELEMETRY_ENDPOINT) ── TELEMETRY_DIR
  backup     ── pg_dump ◄── Honcho database ── BACKUP_DIR
```

## Principles
1. One live file, `settings.json`, holds every operational choice: mode (stock Honcho dreaming or forced scheduler),
   providers and models, per-peer and per-pair dream settings, schedule. Consumers re-read it when its mtime changes.
2. No restarts in daily operation. Only a change to a container's environment (an address, a port, a mount) needs one,
   and those are migration-time changes. That action is the dashboard's Restart Honcho button and the container panel on Logs.
3. Code and data are separate roots. The code is under `src/`; the live folders come from `deploy/.env`:
   `CONFIG_DIR` holds `settings.json`, `models.json` and the backups, `DATA_DIR` the instruction files, `LOGS_DIR`
   every log, `TELEMETRY_DIR` and `BACKUP_DIR` the events and dumps, `SECRETS_DIR` `tokens.env` and the CLI logins. `HONCHO_ROOT`
   is used only for Honcho's `.env` and `config.toml`.
4. Two secrets files: the Honcho project's `.env` (`${HONCHO_ROOT}/.env`: Honcho's own secrets and the provider keys)
   and `${SECRETS_DIR}/tokens.env` (`BRIDGE_TOKEN`, `TELEMETRY_TOKEN`, `TELEMETRY_HEADERS`, written once by
   `setup.sh`). Every service of this stack loads both through `env_file`, and so do Honcho's `api` and `deriver`
   (`honcho-mounts.yaml`); this project's own `deploy/.env` holds paths, ports and names only. `settings.json`
   names env vars, never values. Telemetry, backup and the scheduler start again with only the variables they read;
   the dashboard (which locks fields by the names it sees) and the bridge (whose `key_env` names a variable ending in `_KEY`) keep
   the whole file. `deploy/README.md` has the table and the limits of this.
5. Honcho's stock dreaming and the forced scheduler never both fire: the hook enforces `mode` and logs which gate each
   dream passed.

## Mode
- `stock`: Honcho's own idle-timeout dreaming runs. The scheduler process stays up and sleeps. The hook does not skip a dream for its trigger reason.
- `scheduler`: a dream runs when `scheduled_by` is `honcho-scheduler`, or when that attribute is absent and the payload's `trigger_reason` is `manual` (the value Honcho's schedule route writes). Honcho's own trigger reasons are skipped and logged `gate=skipped-mode`.

`scheduled_by` is still sent. The request drops it, and `DreamPayload` has no such field, so it does not reach the deriver today. The hook reads it first when the attribute is present. Installation also replaces `src.deriver.consumer.process_dream` when that module is already loaded, so a chained entrypoint cannot keep the original binding. The trigger reason is what reaches the deriver now.

## Lookup
Brief, the two efforts (`deduction_effort`, `induction_effort`) and rounds resolve field by field: pair, `peers[observer]`, `peers[observed]`, `defaults`. A null or absent field falls through. An empty pair does not hide the peer blocks. The log source is the block that supplied the first effort. A peer card refresh has no per-dream effort: its `peer_card` stage entry sets it.

Backend uses the same chain, then `providers.active` when every backend is null or absent. The instruction directory for a backend is that key, without a `:model` suffix.

The top-level `rerank` block holds the global settings of the rerank component (`src/rerank/honcho_rerank`) (`enabled`, `provider`, `model`, `base_url`, `candidates`, `max_documents`, `max_query_chars`, `timeout_seconds`, `callers`, `min_score`, `daily_request_cap`, `failures_before_cooldown`, `cooldown_seconds`). The component reads it in both the API and the deriver and re-reads it when the file changes; it is off by default, and while it is off, or when `settings.json` is missing, Honcho's searches are unchanged. The optional `rerank` block in `defaults`, a peer or a pair replaces `enabled`, `provider`, `model`, `base_url`, `candidates`, `max_documents`, `timeout_seconds`, `min_score` and `daily_request_cap` for one dream. Each key resolves like a dream limit: pair, `peers[observer]`, `peers[observed]`, `defaults`; a key set nowhere keeps the global value. A per-dream cap may lower the global cap; a higher cap is clamped and logged by the hook and refused by the dashboard. A per-dream `enabled` may turn reranking off, never on: with the global switch off, nothing reranks. A block that names another `provider` or `base_url` than the global one runs without `RERANK_API_KEY`, which belongs to the global provider and host. With no global cap, a per-dream cap is allowed. Callers and the cooldown fields stay global because their state is shared across dreams. No block holds a key: `RERANK_API_KEY` stays in the environment of both containers, and the schema rejects an `api_key` key. The hook patches the rerank component (`src.honcho_rerank`) when that package is installed. Without it the dream hook still runs and logs that reranking is skipped. On reload the hook checks global and per-dream rerank blocks with the component's `RerankSettings`, falling back to the JSON schema when the component cannot be imported offline. An invalid rerank block is logged as the exception class without values and the last good rerank settings stay; the rest of the file is applied. A rejected reload of `mode`, `defaults`, `peers` or `pairs` keeps the whole last good settings object and logs the exception class without values.

The hook skips a dream only when the observer or the observed is listed with `enabled: false`. An unlisted peer is not disabled. The scheduler will not queue a pair unless both peers are listed and not disabled.

## Provider routing
Model overrides do not require a separate allowlist. The dashboard catalog includes manually added models;
validation and the bridge enforce a backend's `models` list only when one is explicitly configured.

Honcho's dream model config points at the bridge permanently. With `BRIDGE_TOKEN` set in Honcho's `.env` the bridge
requires it as a Bearer token on every route (constant-time compare, checked before the body is read) and `/health`
answers only `ok`, `settings_ok` and `log_ok` without it; Honcho sends it through `api_key_env = "BRIDGE_TOKEN"` in each
bridge model block. Unset, the bridge is open on the Honcho network. The bridge picks the backend per request:
top-level `backend` or `extra_body.backend`, then `defaults.backend`, then `providers.active`. A reference may be
`<backend>` or `<backend>:<model>`. Kinds: `cli-codex`, `cli-claude`, `openai-compat` (OpenRouter, DeepSeek, GLM, and
the same shape for any other OpenAI-compatible host), `anthropic-api`. Effort precedence: the request's effort (the hook's per-dream effort), then the stage entry's `effort`
(`providers.stages`, a per-job pick on the Models page), then the backend's `effort` or `thinking_effort`. The provider (backend `effort_style`, else the `base_url` host, else generic
`reasoning_effort`) decides the effort field, the nearest documented level, `max_completion_tokens` for OpenAI and
`json_object` for providers without `json_schema`; see `src/bridge/README.md`. A dream in flight finishes on the backend it started with. A backend is added, edited or removed in `settings.json`;
the dashboard refuses a save that leaves `providers.active`, `defaults.backend`, a peer, a pair or a stage naming a
backend that does not exist, and the error lists those references.

## Instruction files
Honcho reads custom instructions from the workspace configuration in its database. Files in `instructions/` in `DATA_DIR` (mounted at `/data/instructions` in the dashboard, and read-only at the same path in the deriver) use one tree:

```
instructions/default/<kind>.md
instructions/backends/<backend>/<kind>.md
instructions/peers/<peer>/<kind>.md
```

`<kind>` is `dream`, `peer-card`, `reasoning`, `summary` or `dialectic`. The hook swaps only `dream` and `peer-card`, per dream: `peers/<observer>/<kind>.md`, then `peers/<observed>/<kind>.md`, then `backends/<backend>/<kind>.md`, then `default/<kind>.md`, then the database value. The file is stat'd on every dream; the text is reused when the mtime matches. An empty file clears that category. A missing folder is not an error. A file over the deriver token cap is applied and a warning is written to `policy-used.log` (the count and the cap, not the text). The telemetry field `custom_instructions_source` is `settings.peer.<name>.<kind>`, `settings.backend.<name>.<kind>` or `settings.default.<kind>`.

For one release the hook still reads a flat name (`<backend>-dream-instructions.md`, `default-peer-card-instructions.md`, and the same pair for the other dream kind) when the new file at that level is absent, and logs one deprecation line. On start the dashboard moves a flat file into the tree when the new file does not exist.

`reasoning`, `summary` and `dialectic` are not dream-time. Honcho resolves them from the message, then the session, then the workspace, with no per-peer file. The dashboard shows any file of those kinds as informational and writes the chosen text into `workspace.configuration.<module>.custom_instructions` through the Honcho API, after a diff. The previous configuration is kept as `backups/workspace/workspace-config.json.bak_<YYYYMMDD_HHMMSS>` in `CONFIG_DIR`. The Peers section edits `peers/<peer>/dream.md` and `peer-card.md`, including create-from-default and create-from-backend. The bridge does not read these files. The hook's wrapper is on the orchestrator's `get_configuration`, so other callers keep the database value.

## Peer registry
`peers` holds every observer/observed seen in the database; new ones are appended as `enabled: false` by the scheduler
or the dashboard. A disabled peer is never dreamed, in either mode. The scheduler's own list is the configured pairs
plus each enabled peer with itself, minus `scheduler.skip`. Discovery also appends every observer/observed notebook
missing from `pairs` as `{}` and puts it in `scheduler.skip`, so a found pair starts Off. Discovery writes before it
queues, so the poll that finds a name does not dream it. The dashboard keeps the two switches in step: a pair switched
On turns its peers On (their other pairs stay Off), a peer whose pairs are all Off goes Off, and a peer switched Off puts
every pair with it in `scheduler.skip`.

## Telemetry
`src/telemetry/receiver.py` listens for Honcho. `POST /events` takes one CloudEvent or an array (`id`, `type`, `source`,
`specversion` required, non-empty strings, at most 1000 per post) and appends `{"received_at","event"}` lines to `events-YYYY-MM-DD.jsonl` in `TELEMETRY_DIR`,
one file per UTC day, fsync per batch. A bearer token (`TELEMETRY_TOKEN`) is required and checked before the body is read. A body is capped at `MAX_BODY_MB`
(default 64) and at most two are read at once (a third waits, then gets 503); chunked bodies, signed or repeated
`Content-Length` and anything the JSON parser refuses get 4xx. The cap must stay above Honcho's largest batch, because
Honcho re-sends a refused batch at the head of its buffer. `GET /health` reports today's
file size and free space and reports `low_space` below `MIN_FREE_GB` (posts are then refused with 503). Files other than today's are gzipped after
`GZIP_AFTER_DAYS` and deleted after `RETENTION_DAYS`. When `VIEWER_URL` is set, each accepted batch is forwarded once
to the viewer hub; a failure is counted and does not fail the post. The viewer is not modified. Each maintenance pass
appends its actions to `maintenance.log` in `LOGS_DIR` (`MAINTENANCE_LOG`).

## Backup
`src/backup/backup.sh` runs in `pgvector/pgvector:pg15`, the same image as Honcho's `database` service, so the
custom-format dump matches the server major. The container timezone is `TZ` from `.env`. At `BACKUP_AT` (default 03:30)
it runs `pg_dump -Fc --no-password` against `PGHOST`, `PGPORT`, `PGUSER` and `PGDATABASE` (defaults `database`, `5432`, `honcho`, `honcho`) and publishes `honcho-YYYY-MM-DD.dump` plus
`honcho-YYYY-MM-DD.dump.sha256` in `BACKUP_DIR` (mounted at `/backups`) once `pg_restore --list` reads the dump's table
of contents and finds a table and the checksum matches. A failed attempt is retried up to `BACKUP_ATTEMPTS` (default 8)
with a doubling wait from `BACKUP_RETRY_SECONDS` (default 30) to 15 minutes; a wrong password, user or database, or a
dump with no table, is not retried. `backup.sh --now` runs one dump on demand. `src/backup/restore.md` gives the manual
steps to check a dump and load it into a scratch database. It keeps the
newest `KEEP_DAILY` dumps (default 7) and the newest dump of each of the last `KEEP_WEEKLY` ISO weeks (default 4).
A file that meets both rules is kept once. Every other `honcho-YYYY-MM-DD.dump` is deleted with its checksum. Files
that do not match that name stay. `backup.log` is in `LOGS_DIR` (`/logs/backup.log`, `BACKUP_LOG` overrides it); it
rotates at 5 MB and keeps one previous file. The script writes only under `/backups` and that log.

## Settings file
`CONFIG_DIR` is the host directory mounted at `/config`: it holds `settings.json`, `models.json`, `rates.json` and `backups/`, the copies in `backups/settings/`, `backups/models/`, `backups/instructions/<relative path>/`, `backups/workspace/` and `backups/config-toml/`. `DATA_DIR`, mounted at `/data` in the dashboard, holds `instructions/` only. Dashboard copies are `<file>.bak_<YYYYMMDD_HHMMSS>`; the newest 10 per file are kept, plus any younger than 7 days. Files that do not match that name are never deleted by the rotation. Every write is read back and compared. On start, and when History is listed, the dashboard moves any older `*.bak-*` still beside an instruction file into that folder; it never deletes one and never overwrites a copy already there. The scheduler writes `settings.json.bak-<stamp>` straight into `backups/settings/` when it appends peers and keeps the newest 20 of its own; History lists both forms. Every consumer mounts the config folder. A bind of the file alone keeps the old inode after a temp-file rename, and hot reload stays on the previous content. `CONTROL_FILE` is the in-container path `/config/settings.json`, set in the compose `environment:` block. `src/config/settings.schema.json` is the
contract. `src/config/settings.example.json` is the committed shape. `python src/config/validate.py` checks a file against the
schema and the cross-references (backends, models, stages); `src/config/README.md` has the usage.

## Honcho config
The dashboard edits Honcho's `config.toml` (`HONCHO_ROOT/config.toml`, bound as one file at `/honcho/config.toml`). The schema is generated from Honcho's settings classes and omits auth, Sentry, the database, rerank, and any field whose name is a secret (`_KEY`, `_SECRET`, `_PASSWORD`, `_TOKEN`, `_URI`); it records upstream v3.5.0 and its tag commit, and carries `[summary] output_headroom` from the separately applied `summary-output-headroom.patch`. The rerank and HNSW settings belong to the rerank component (the `rerank` block of `settings.json`, `PGOPTIONS`), not `config.toml` fields. Honcho's precedence is the environment, then `.env`, then `config.toml`, then the built-in default, and it reads the file only at process start. Fields named in `HONCHO_ENV_OVERRIDES` are read-only on the page. A write keeps comments and unknown keys, writes in place, reads the file back, and keeps `config.toml.bak_<YYYYMMDD_HHMMSS>` copies in `backups/config-toml/` in `CONFIG_DIR` under the same rotation (newest 10 plus any younger than 7 days). Restart Honcho (`POST /api/honcho-config/apply`) restarts `api` and `deriver` when the dashboard's constant container list (`RESTART_ALLOWED` in `src/dashboard/app/app.py`) names exactly one container for each service, either the bare service name or the compose name `<project>-<service>-<n>`; otherwise it is refused with 409 and nothing restarts. Both must restart: the api handles the dream schedule route and the deriver runs the dreams. The restart is refused with 409 while any `model_config` block (deriver, summary, each dialectic level, dream deduction and induction, embedding) has no `model` or no `transport` in the file and no compose override: Honcho would otherwise start on its built-in default model on the OpenAI key without saying so. The review lists those blocks, and the Overview shows what each task resolves to. Routing is written once by Route all jobs through the bridge on the Honcho config page, which moves each job's model into Honcho's stock `fallback` table and keeps a copy of the file for the two undo choices (`docs/HONCHO-CONFIG.md`); a write that fails after the file was opened puts the backup it took back. Dream model fields in that file do not choose the dream backend; `settings.json` does, through the bridge.

## Restarts
The dashboard is the one place a migration-time restart stays a button. `dashboard` calls two socket proxies (`tecnativa/docker-socket-proxy`) on the internal network `restart`. Both mount the Docker socket and publish no ports; the `:ro` on that mount does not limit the API, the proxy's rules do, and `CONTAINERS=1` allows every `GET` under `/containers` (inspect includes each container's environment), as `deploy/README.md` lists. `docker-proxy` lists containers (`CONTAINERS=1`, `POST=0`). `docker-proxy-action` allows restart, start and stop (`POST=1`, `CONTAINERS=0`, `ALLOW_RESTARTS`, `ALLOW_START`, `ALLOW_STOP`). The image denies every non-GET unless `POST=1`, and `POST=1` with `CONTAINERS=1` would also allow create and exec. Listing uses `DOCKER_PROXY_URL`. Actions use `DOCKER_PROXY_ACTION_URL`. Which container names are accepted is the constant `RESTART_ALLOWED` in `src/dashboard/app/app.py`: Honcho's `honcho-api-1` and `honcho-deriver-1`, and this stack's `honcho-tools-<service>-1`. With `CONTROL_PASSWORD` set the dashboard needs a basic login, and every write must be JSON; the container name is checked against that list. Each call is appended to the process log and to `restart.log` in `LOGS_DIR`, which follows the `logs` block (`max_mb` default 10, `keep` default 1). The log records the socket peer (`ip=`) as information.

## Dashboard
One page with a left menu (a drop-down menu below 760 px): Overview, Models, Honcho config, Peers, Pairs, Rerank, Runs, Cost, Instructions, Logs, Advanced. Each page is a set of collapsible boxed sections with counts. Light and dark themes follow the system and can be switched; the choice is kept in the browser. Overview holds the daily controls: who starts dreams, the workspace switches, and the Scheduler box (dream type, check interval, dreams at the same time), which the scheduler reads at its next check without a restart. Logs list the newest entry last. An open tab checks `settings.json` every 30 seconds and when the tab returns to view, and reloads unless it holds unsaved changes. Runs and Cost share one read of the telemetry files: each file is read once and then only its new lines, kept in memory for the last 7 days, and the first request after a start reads them all.

Prompt views show what a model receives. `GET /api/prompts` serves `honcho-prompts.json` from the templates (`src/config`, mounted at `/templates`), which `src/config/gen_honcho_prompts.py` builds from a Honcho source tree: it compiles Honcho's own prompt functions with a slot in place of the custom text, so the stock text is exactly what Honcho sends. For intake, summaries and questions the page shows "Honcho's prompt only" or "Honcho's prompt + my text". For dreams and the peer card it assembles each specialist's system prompt (deduction, induction, card refresh) with the brief, budget and notes sections that `[dream]` switches on, the effective tool-call and notes limits filled in, and the dream and peer-card texts that would apply for that peer. Regenerate the file when Honcho is upgraded.

`models.json` beside `settings.json` is the model catalogue for dropdowns (seeded from `src/config/models.json`); `POST /api/models/add` adds one dream model. Dream limits (`brief_tokens`, `tool_calls`, `notes`, `notes_tokens`, `budget`, `write_window`) resolve like brief and effort, pair, then peers, then `defaults`, and the hook substitutes them for Honcho's `[dream]` value for that one dream; a limit set nowhere keeps Honcho's value.

The Rerank page has three boxes. "Global (api and deriver, hot)" edits the top-level `rerank` block (every field of the rerank component, including the `enabled` switch, callers and the cooldown pair) and shows `ef_search` as the deployed `PGOPTIONS` carries it. "Per dream (settings.json)" is one table: the Defaults row, every peer and every pair, with the five main `rerank` keys as columns (`enabled`, `provider`, `model`, `candidates`, `daily_request_cap`) and a ⋯ dialog for the other four; a value not set shows faded with where it comes from (pair, observer, observed, default, global, then the component's own default). Both write through the same Stage, Review and Write flow as the dream limits; the rerank component and the hook apply them at the next search or dream, without a restart. The Honcho config page's "Honcho summary setting (config.toml)" box edits `[summary] output_headroom` through the Honcho config write and needs a Honcho restart. The dashboard writes nothing under `[rerank]` or `[db]` in `config.toml`.

`GET /api/rerank/warnings` warns when a block enables rerank while the global `callers` lack `dream` (the block has no effect) and when `candidates` (global or per block) exceeds `ef_search`. `ef_search` is read from the `PGOPTIONS` environment variable of the containers in `RESTART_ALLOWED`, through the read-only docker proxy (`Config.Env`, only that entry is read), else from the dashboard's own `PGOPTIONS`; when neither is readable the page shows `unknown` and no `ef_search` warning is made. Keep the same `PGOPTIONS` value on the API and the deriver.

"Rerank calls (telemetry)" on the same page is `GET /api/rerank/stats`: a read-only summary of the `rerank.call.completed` events in `TELEMETRY_DIR` for today and the last 7 UTC days (`events-YYYY-MM-DD.jsonl`, gzipped or not): counts by outcome, caller and provider, token and billed-unit totals, the fallback rate (every outcome but `reranked`), the number of calls that sent a request (`candidates_sent > 0`, whatever the outcome) and the p50 and p95 of their durations, the latest event, and the last observed request counter (`daily_requests` with its cap and time, from the last event that reported one; a skip reports none). Every number read from an event goes through a finite check (no NaN, infinity, bool or integer beyond 2^53); a bad value skips that metric of that event, the event still counts by outcome, and the response is serialised with `allow_nan=False`. Files are read in 64 KiB chunks; a line over `RERANK_LINE_LIMIT` (1 MB) is drained and skipped without being held; one request reads at most `RERANK_SCAN_BYTES` and keeps at most `RERANK_SCAN_EVENTS`, and each period keeps a reservoir sample of `RERANK_DURATION_SAMPLE` durations for the percentiles; when a limit stops the scan the response and the page say `truncated`. A past day's aggregate is cached by (path, size, mtime), so only today's file is re-read per request. An unreadable file (I/O, gzip header, corrupt deflate body) is listed, not fatal. Honcho samples these events by `HIGH_VOLUME_SAMPLE_RATE`, so the counts are a lower bound; the counter is Honcho's own, per UTC day across processes, and only as current as the event that carried it. No new dependency.

No edit writes on its own. An edit is a working value; the floating Stage bar shows how many edits are not staged, with Stage and Discard. Stage turns them into staged changes. Review (N) in the top bar lists the staged changes grouped by file, asks each target for its check (schema, token cap, model gaps), allows unstaging one change, one file or all, and Write then writes `settings.json`, the instruction files, the workspace configuration and `config.toml`, in that order, each with its backup. A failure stops the rest, which stays staged. A stale file (mtime changed since it was read) is refused with 409. Leaving the page with working or staged changes asks first.

An empty peer or pair field inherits and shows the inherited value faded; Reset clears a row's own values. In the Honcho config page a value the file does not set shows Honcho's default faded, and Reset removes a key the file sets. Help is a rich tooltip on the label or header: name, description, allowed values, and for Honcho fields the current source and the default.

Overview holds the mode and the workspace switches `dream.enabled`, `reasoning.enabled` and `summary.enabled` (`GET /api/control`, written with `POST /api/workspace`). Honcho reads them per request, so no restart. Honcho merges top-level workspace keys on update and cannot drop one, so the dashboard always sends each module block whole: a switch keeps the block's custom instructions, and "not set" removes only the `enabled` key. Dialectic has no enabled flag. `config.toml` `[dream] ENABLED` is shown read-only; it needs a restart.

The deriver compose service must set `entrypoint: ["/app/.venv/bin/python", "-m", "src.honcho_rerank.deriver_main", "/app/policies/deriver_main.py"]` when the rerank package is installed, or `python /app/policies/deriver_main.py` when it is not; the stock entrypoint runs no scheduler hook. The hook writes its log, run records and status file into `LOGS_DIR` (see `src/hooks/README.md` and `deploy/honcho-mounts.yaml`).

Completed dreams append a credential-free JSON record to `runs.log` beside `policy-used.log`, in `LOGS_DIR`. The Runs page joins this snapshot with rerank and dream telemetry by run ID (and workspace when recorded), with pair/date filters. Reads are bounded to 500 recent run lines and the telemetry scan budget; partial coverage is labelled. Missing telemetry, including conclusion counts absent from the current fork events, stays unknown. The record retains rerank settings already resolved for the dream; otherwise it resolves against the current global base at write time, including settings reloaded since hook installation. Failed observation does not change a successful dream result.


Cost estimates read `rates.json` beside settings.json (in `CONFIG_DIR`), or `RATES_FILE`. `setup.sh` copies `src/config/rates.example.json` there, which holds dated public list prices, with one official source link per provider, for the text models in `models.json` (OpenAI, Anthropic, Google Gemini, DeepSeek, xAI, Mistral, Qwen, Z.ai GLM, Kimi) and the Voyage rerankers; null/missing prices stay unknown. A model's optional `long_context` block has `above_input_tokens` and replacement `rates`, selected when the full prompt (including cached tokens) exceeds the threshold. An `estimate_note` is displayed with priced rows. A DeepSeek call is priced at the peak list rate inside its peak windows and at the off-peak factor outside them; Chinese public holidays are off-peak and are not modeled. Gemini uses standard global pricing, and Haiku cache writes assume a 5-minute TTL. Discounts, credits and historical rate changes are not applied. The legacy `voyage` model-price map is accepted read-only. LLM entries include input/output/cache-read rates and `cache_creation_per_million` when cache writes occur. OpenAI/Gemini prompt totals include cached tokens; Anthropic input is uncached. Unknown transport with cached usage is unpriced. Explicit event billing evidence wins a conflicting CLI map and is flagged. Otherwise models explicitly listed in `cli` are subscription tokens only. The mounted routing_telemetry extension preserves the executed model/backend/billing route from bridge responses in both LLM event streams; Runs shows observed calls separately from configured fallbacks. Streaming usage without complete SDK accounting stays unpriced. Historical unlabeled relays remain unknown-route potential API estimates, excluded from known spend. See docs/EXECUTED-ROUTING.md for the evidence contract and bounded bridge diagnostics. The Cost page has one line per Models row, plus Rerank and Embeddings, and shows that its calls add up; sampled telemetry can undercount. No estimates are invoices or spend controls.
