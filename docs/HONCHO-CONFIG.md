# Honcho config

The dashboard page "Honcho config" is the one place that writes Honcho's `config.toml` (the file in the Honcho folder, `HONCHO_ROOT/config.toml`, bound as one file into the dashboard at `/honcho/config.toml`). The one exception is the Overview job switch: switching dreams or summaries on there also sets `dream.enabled` or `summary.enabled` to true in the file when it was false; switching off never touches the file. Every other edit on this page waits for Review, is written by Write, and takes effect after a Honcho restart: Honcho reads `config.toml` only at start. Nothing else in the repository reads this file.

## What the page holds

**Bridge routing.** One button, "Route all jobs through the bridge", stages one `config.toml` write for every job that is not yet routed (intake, summaries, the five dialectic levels, dream deduction, dream induction; the peer card shares dream deduction's block). For each of them the current primary (model, transport, effort, token caps, overrides) is copied into Honcho's stock `fallback` table of that block, the "Honcho native" model, and the primary becomes `transport = "openai"`, `model = "stage-route"` with the bridge URL and key variable (copied from a job already routed, else `http://bridge:8793/v1` and `BRIDGE_TOKEN`) and `extra_body.backend = "@<stage>"`; dream blocks get `structured_output_mode = "json_object"`. A job whose block already has a `fallback` keeps it untouched, and the box lists those jobs ("Kept your own fallback: …"). On jobs that are already routed, a `thinking_effort` or an older `extra_body.output_config.effort` left on the primary is cleared, because it would reach the bridge as the request's effort and beat the per-job effort chosen on the Models page. `settings.json` gets a `providers.stages` entry for each newly routed stage (an existing entry is kept), and `peer_card` is set from `dream.deduction` when it is unset. Review, then Write, then restart Honcho.

The Write that carries these edits first copies the file to `config.toml.before-route-all` in the config-toml backup folder, only when no such copy exists; either undo removes it. Two buttons read that copy, and are disabled while it does not exist ("No copy from before routing"):

- **Undo routing (jobs only)**: every job table above is replaced by the same table from the copy (a table absent in the copy is removed); everything else in the current file stays as it is now.
- **Restore config.toml from before routing**: the whole file is put back from the copy, byte for byte. The hint says it also undoes every other `config.toml` change made since the copy was taken.

Both write through the normal path (a backup of the current file first), answer 409 when the file changed since the page loaded, and need a Honcho restart. Neither touches `settings.json`: the stage entries stay, and an unrouted job ignores them.

**Fallback if the bridge is down.** One row per job block: the Honcho-native model of that block's `fallback` (a picker over `models.json` "tasks" by transport), its `thinking_effort` (blank = the model's default), and a plain-words column: "Bridge down → Intake uses Claude Haiku 5.5 (Honcho native), effort medium", "No fallback: if the bridge is down this job fails", or "Not routed: Honcho calls this model itself (Honcho native)". Honcho uses the fallback on the final retry attempt when the primary fails. A provider change on a fallback also drops its `structured_output_mode` (Honcho 3.5.0 refuses it on any transport but `openai`) and the old provider's overrides (`base_url`, `api_key_env`, `provider_params`).

**Dreams (Honcho, all peers).** Honcho's own values used by every dream: idle minutes before a dream, hours between dreams, and the limits (tokens for new conclusions, tool-call cap, working notes and their size, budget display, rounds left to write). These are the fallback for the per-dream settings on Peers and Pairs.

**Honcho summary setting.** `[summary] output_headroom`, the one patch the component design keeps in Honcho itself.

**Restart Honcho.** Restarts the `api` and `deriver` containers through the restart proxy (`POST /api/honcho-config/apply`), after a confirm. The button is replaced by a hint while edits wait for Review.

## What it never writes

The page writes no model choice for a routed job: after Route all, every model and per-job effort pick on the Models page writes only `settings.json`. It writes nothing under `[rerank]` or `[db]` in `config.toml` (those belong to the rerank component: the `rerank` block of `settings.json` and `PGOPTIONS`), and no key: `api_key_env` is the name of an environment variable, the dashboard stores the name. The embedding model is set once in config.toml at install; changing it later means re-embedding all stored memory, so the dashboard does not offer it. A key that is already in the file and is not in the generated schema is left unchanged and listed as "not in schema"; a write does not delete it. The schema (`src/config/honcho-config.schema.json`) leaves out `AuthSettings`, `SentrySettings`, `DBSettings` and `RerankSettings` (`EXCLUDED_CLASSES` in the generator) and any field whose name is `SECRET` or ends in `_KEY`, `_SECRET`, `_PASSWORD`, `_TOKEN` or `_URI`; those values stay in the environment or in `.env`.

## Precedence

Honcho loads configuration in this order: the environment, then `.env`, then `config.toml`, then the built-in default. A value set in the environment wins over the file. The dashboard cannot change that. `HONCHO_ENV_OVERRIDES` is the comma-separated list of environment names the compose file is known to set. The default is `API_WORKERS,DERIVER_WORKERS,DB_POOL_SIZE,DB_MAX_OVERFLOW,TELEMETRY_ENDPOINT,TELEMETRY_HEADERS`. Those fields are read-only and marked "set in compose".

Honcho reads `config.toml` only when the process starts. Review and Write update the file. Restart Honcho (`POST /api/honcho-config/apply`) restarts the `api` and `deriver` containers through the existing restart action, after a confirm, and only when the restart proxy is set and the dashboard's constant container list (`RESTART_ALLOWED`) has one container for each.

## Write

The write parses the existing file, changes the edited keys, and writes it back with the comments and the key order kept. The previous file is copied to `config.toml.bak_<YYYYMMDD_HHMMSS>` in `backups/config-toml/` in `CONFIG_DIR` (`HONCHO_CONFIG_BAK_DIR` overrides it). The newest 10 copies are kept, plus any younger than 7 days. The file is read back after the write; if anything fails after the file was opened for writing, the backup taken at the start is written back the same way and the request fails. Restart Honcho is refused while a `model_config` block has no `model` or no `transport`. The file itself is rewritten in place, not renamed: Honcho's containers hold it as a single-file bind mount, and a rename would leave that mount on the old inode. A value that fails the schema (type, enum, or bound) is refused and the file is not touched.

## Regenerate after a Honcho upgrade

From the repository root, with the path of the new `src/config.py`:

```
python src/config/gen_honcho_schema.py <path-to-honcho>/src/config.py
```

That rewrites `src/config/honcho-config.schema.json`. The version string comes from the `pyproject.toml` beside `src`. The generator parses the file; it does not import Honcho. Restart the dashboard process so it re-reads the schema.

The committed files were last built from the fork checkout (Honcho 3.5.0 plus `[summary] OUTPUT_HEADROOM`, the one patch the deployment keeps, `src/rerank/patches/summary-output-headroom.patch`; the `fork_sha` and heading in the schema still name that checkout and the old fork's `FORK.md`, and change at the next regeneration). `src/config/honcho-prompts.json` is rebuilt the same way with `python src/config/gen_honcho_prompts.py <path-to-honcho>/src`. Neither generator needs Honcho's Python environment.

The `[summary]` fields in this file take effect after a Honcho restart, like every other `config.toml` key. Rerank is not a `config.toml` setting: the top-level `rerank` block of `settings.json` (Rerank page, "Global (api and deriver, hot)") is read by the rerank component and needs no restart, and the per-dream `rerank` blocks replace its keys for one dream.
