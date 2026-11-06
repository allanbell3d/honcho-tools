# Operations

Day-to-day use of a running stack, and the recurring tasks. Everything here goes through the dashboard or the live
folders; nothing needs a rebuild, and only the actions marked **restart** need a Honcho restart.

Where things are, as seen from the host (the folder names are the `deploy/.env` variables):

| What | Where |
|---|---|
| Behaviour | `CONFIG_DIR/settings.json` (`docs/SETTINGS.md`), `CONFIG_DIR/models.json` (the model catalogue), `CONFIG_DIR/rates.json` (prices for the Cost page) |
| Copies before every write | `CONFIG_DIR/backups/{settings,models,instructions,workspace,config-toml}/` |
| Instruction files | `DATA_DIR/instructions/` |
| Logs | `LOGS_DIR/`: `policy-used.log`, `runs.log`, `hook-status.json` (hook), `dream-nas.log` (scheduler), `bridge-calls.jsonl` (bridge), `restart.log` (dashboard), `maintenance.log` (telemetry), `backup.log` (backup) |
| Telemetry | `TELEMETRY_DIR/events-YYYY-MM-DD.jsonl` (UTC day; gzipped after a day, deleted after 90 by default) |
| Database dumps | `BACKUP_DIR/honcho-YYYY-MM-DD.dump` with `.sha256` |
| Honcho's config | `HONCHO_ROOT/config.toml`, edited only from the Honcho config page |

## The edit flow

Buttons and switches act at once (who starts dreams, the workspace switches, the Dreams switches on Peers and Pairs,
**Add now (dreams off)**). Every other edit waits: **Review (N)** in the top bar lists the pending changes with before and after
and an **Undo** per change, **Discard** drops them all, and **Write** then writes
`settings.json`, the instruction files, the workspace configuration and `config.toml`, in that order, each with a
backup and a read-back. A write that fails stops the rest, which stays staged. A file that changed since the page
loaded (another tab, the scheduler's discovery) is refused with 409: reload and review again. Leaving the page with
unsaved changes asks first.

A `settings.json` change is hot: the hook reads it when the next dream starts, the scheduler at its next poll, the
bridge at its next call, the rerank component at its next search. A `config.toml` change needs **Restart Honcho**
(Honcho config page), which restarts `api` and `deriver` through the action proxy.

## Who starts dreams

**Overview**, "Who starts dreams": `stock` or `scheduler` (`mode`). The same page has the workspace switches
(`dream.enabled`, `reasoning.enabled`, `summary.enabled`, read by Honcho per request) and the **Scheduler** box
(dream type, check interval, dreams at the same time), which the scheduler reads at its next poll.

In `scheduler` mode the scheduler queues a dream for each eligible pair that has unread conclusions, through Honcho's
schedule route, at most `max_parallel` at once; a pair whose last dream changed nothing waits 15 minutes, then
doubling up to 24 hours. The hook runs those dreams and skips the ones Honcho starts on its own
(`gate=skipped-mode` in `policy-used.log`). Switching back to `stock` lets Honcho's own triggers run again; the
scheduler sleeps.

Check: `logs/policy-used.log` ends each dream line with `mode=<mode> backend=<backend> gate=<ran|skipped-mode|skipped-disabled|skipped-settings>`.

## Peers and pairs

In `scheduler` mode new peers and pairs (each notebook in Honcho's `collections` table) are discovered from the
database: peers are appended to `peers` as `enabled: false`, pairs to `pairs` as `{}` and to `scheduler.skip` (Off); the
**Peers** page lists them with the names the Honcho API reports, and a name only the API knows can be added. Turn a
peer on there. A peer that is off is never dreamed, in either mode, and the scheduler queues a pair only when both
peers are listed and on.

**Peers** and **Pairs** set, per row, the brief, the deduction and induction efforts, rounds, the dream model override and (behind ⋯) the six
Honcho dream limits and the per-dream rerank keys. An empty field inherits (pair, observer, observed, defaults) and
shows the inherited value faded; **Reset** clears a row's own values. The **Defaults** row is `defaults`.

## Models and effort

After Route all (`docs/SETUP.md`, step 8) every job row on **Models** has a picker over the catalogue and an effort
select; both write `providers.stages` in `settings.json` and apply at the next call. The **Dreams fallback** row is
`providers.active`, used by a dream when no stage entry and no `backend` override applies. A dream pass's own effort
(Pairs, Peers, Defaults) wins over the job's effort on the Models page; **Peer card refresh** takes its effort from Models only. An effort left blank says "Model default" plus
the level it resolves to, here and on Peers and Pairs; alone, "Model default" means no level is set anywhere. **Add a model to the list**
adds a catalogue entry (`models.json`) for the dropdowns.

A backend itself (`providers.backends`) is added, renamed or removed in `settings.json` by hand; the dashboard
refuses a save that leaves `providers.active`, `defaults.backend`, a peer, a pair or a stage naming a backend that
no longer exists, and names the references.

Which model answered a call is a different question from which was configured: **Runs** shows the observed model,
backend and billing route from telemetry (`docs/EXECUTED-ROUTING.md`).

## Honcho's config.toml (restart)

The **Honcho config** page is the one place that writes `config.toml` (`docs/HONCHO-CONFIG.md`):

- **Route all jobs through the bridge**, once per unrouted job. The job's current model becomes its Honcho-native
  `fallback`, used on Honcho's final retry attempt when the bridge fails. A job that already has a fallback keeps
  it. The write keeps a copy of the file from before the first routing.
- **Undo routing (jobs only)** puts every job table back from that copy and keeps every other change made since;
  **Restore config.toml from before routing** puts the whole file back. Both delete the copy and need a restart.
- The **fallback table**: each job's Honcho-native model and effort, with a plain-words column saying what happens
  when the bridge is down. Give every job you rely on a fallback; the install check in `deploy/README.md` proves it.
- The **dream knobs** (idle minutes, hours between dreams, the limits that per-dream settings fall back to), the
  **summary output headroom** (needs the optional patch) and **Restart Honcho**.

Restart Honcho is refused while any model block has no `model` or `transport` (Honcho would start on its built-in
default), and the Overview lists what each job resolves to.

## Instruction files

**Instructions** has one box for dream and one for peer-card: the default file, and only the backends and peers that
have their own; **Start from the All peers text** begins a peer's own text from the default. The hook reads, per dream, `peers/<observer>/`, then
`peers/<observed>/`, then `backends/<backend>/`, then `default/`, then the database value; an empty file clears the
category. Files are read on every dream, so a saved file applies at the next one. Over Honcho's token cap (2000 by
default) a file is still applied and `policy-used.log` gets a warning.

The intake (reasoning), summary and dialectic prompts are not dream-time: the page writes them into Honcho's
workspace configuration through the API, after a diff, with a copy in `backups/workspace/`.

## Reranking

**Rerank**, "Global": the switch, provider, model, pool size, cap and the rest of the global block; hot for the api
and the deriver. "Per dream": the Defaults row, every peer and pair, with the keys a dream may replace. "Rerank calls"
summarises the `rerank.call.completed` events of today and the last 7 UTC days: outcomes, callers, tokens, the
fallback rate, latency percentiles and the last observed daily counter. Every outcome but `reranked` means the search
kept the vector order (`src/rerank/README.md`, "Outcomes"); `skipped_cap` and `skipped_cooldown` are expected under a
cap or after provider failures.

Keep `PGOPTIONS` on `api` and `deriver` at `hnsw.ef_search` at least `candidates`; the page warns when it can read a
lower value, and the benchmark (`bench/README.md`) shows what the index misses at Honcho's default.

## Cost and runs

**Runs** lists each completed dream from `runs.log`, joined with its telemetry by run id: status, duration, model,
efforts, searches reranked, conclusions, tokens, cost and failed calls. Filter and sort it, and click a run for its
passes, settings, costs and every model and rerank call with its own tokens and cost.

**Cost** prices the calls in telemetry at the list prices in `rates.json` (`setup.sh` copies
`src/config/rates.example.json` beside `settings.json`; keep it current; a model without a rate shows `no rate`). It has
one line for each row of the Models page, plus Rerank and Embeddings. A line opens by provider and model, then day, then
run. The calls add up on the page: OK, failed and skipped equal the total. Failed counts every failed attempt, including
those a retry or fallback recovered. A subscription call shows `$0.00 subscription`, and a skipped rerank `$0.00 skipped`.

Telemetry does not mark a peer card refresh, so its line shows `none recorded` and a refresh counts under Dream deduction
and Dream induction. Embedding tokens are Honcho's estimates. Telemetry is sampled by Honcho
(`HIGH_VOLUME_SAMPLE_RATE`), so counts are a lower bound, and the estimates are never invoices.

## Logs and status

**Logs** shows the newest lines of the dream log and the scheduler log, and the container panel (state, started,
restart, stop, start for the containers in `RESTART_ALLOWED`). The status bar at the bottom shows the container
summary and the last read time; the Overview's hook line reads `hook-status.json` and says `hook active`,
`settings saved … apply at the next dream` (a newer file, pending), `hook failed — dreams blocked`,
`hook failed — running stock Honcho`, or why the status cannot be read.

Every log rotates on its own: the hook's, the scheduler's, the bridge's and `restart.log` from the `logs` block of
`settings.json` (10 MB, one previous copy by default), `backup.log` at 5 MB, the container json logs at 10 MB times 3.
Logs hold no key, prompt or memory text; `bridge-calls.jsonl` records models, backends, token counts and timings.

## Backups and restore

The backup service dumps the database at `BACKUP_AT` (default 03:30 in `TZ`), verifies the dump with `pg_restore
--list` and a checksum, and keeps the newest 7 daily and 4 weekly dumps. `docker compose -f deploy/compose.yaml exec
backup bash /app/backup.sh --now` takes one now; `backup.log` has every attempt. `src/backup/restore.md` has the
commands to check a dump and load it into a scratch database beside the live one, compare counts, and drop it.

Before anything risky (a Honcho upgrade, a restore, a change to the database service): take a dump, and check that
yesterday's dump exists with an `OK` from `sha256sum -c`.

The dashboard's own copies are kept in `CONFIG_DIR/backups/`; **Advanced** lists the settings copies, and its
**Restore** writes the chosen copy back at once (the current file is copied first). The scheduler's discovery copies land in
the same folder. `config.toml` copies are the safety net of the in-place write: after a crash during a write, the
newest `config.toml.bak_*` is the file to put back.

## Restarts and recreates

- A **settings.json** change: no restart, ever.
- A **config.toml** change: **Restart Honcho** on the Honcho config page (api and deriver).
- A change to **Honcho's `.env`** (a new provider key, `BRIDGE_TOKEN`, `RERANK_API_KEY`): recreate the containers
  that read it (`docker compose up -d` for the stack, and Honcho's `api` and `deriver` for keys they use). A restart
  does not re-read `env_file`.
- A change to **`deploy/.env`** (a folder, a port, the backup schedule): `docker compose -f deploy/compose.yaml up -d`
  recreates the services whose configuration changed.
- A change to **`bridge_cli.py`, `dream_nas.py`, `receiver.py`, `backup.sh` or the dashboard's `app/`**: they are
  bind-mounted; restart that one service. A change to a `requirements.txt` or a Dockerfile: rebuild (`up -d --build`).

Restart Honcho when its dream queue is empty: a dream in flight is cut and runs again later.

## Secrets and access

- Rotate a provider key by changing it in Honcho's `.env` and recreating the bridge (and `api`/`deriver` for a key
  they use). Nothing else holds it.
- `BRIDGE_TOKEN` (optional): put it in Honcho's `.env`, make every routed block name it as `api_key_env` (Route all
  uses `BRIDGE_TOKEN` when no block is routed yet), recreate the bridge and restart `api` and `deriver`. Until both
  sides have it, calls fail with 401.
- `CONTROL_PASSWORD` protects the dashboard with HTTP basic over plain HTTP. Keep port 8010 on a trusted network
  (`BIND_IP`) or behind a TLS proxy: whoever reaches it can edit `config.toml`, restart Honcho, and read what the
  read-only Docker proxy allows (`deploy/README.md`, "The Docker socket").
- The CLI logins in `SECRETS_DIR` are session credentials for paid subscriptions; keep the folder private.

## Upgrading Honcho

1. Take a dump. Wait for an empty dream queue.
2. Re-apply `src/rerank/patches/summary-output-headroom.patch` to the new source if you use the headroom setting
   (`apply.sh` checks first), rebuild Honcho's image.
3. Keep the lines from `deploy/honcho-mounts.yaml` on `api` and `deriver`.
4. Regenerate `src/config/honcho-config.schema.json` (`docs/HONCHO-CONFIG.md`) and `src/config/honcho-prompts.json`
   (`python src/config/gen_honcho_prompts.py <Honcho src folder>`) from the new tree and restart the dashboard.
5. After the start, check the Overview's hook line and the Rerank page. The start-up checks name every Honcho symbol
   the hook and the reranker rely on; when one differs, Honcho runs stock without the wrapper (the reranker) or with
   intake only (the hook), and the dashboard says so. The components are bound to Honcho 3.5.0; another version is
   untested.

## Upgrading this stack

```bash
git pull
docker compose -f deploy/compose.yaml up -d --build
```

Read `CHANGELOG.md` first: a changed `settings.schema.json` may need a key in your `settings.json` (the dashboard
reports what the schema refuses), and a changed `honcho-mounts.yaml` needs the edit repeated in Honcho's compose
file.
