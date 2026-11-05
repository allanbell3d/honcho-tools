# config

Templates only: nothing here is a live file. The dashboard mounts this folder read-only at `/templates`.

- `settings.example.json`: the starting live behaviour file. `mode` is `stock`, so Honcho keeps its own dreaming
  until the scheduler is chosen; backends name real catalogue models; the active backend is Honcho's stock model
  (OpenAI `gpt-5.4-mini`, `openai` backend, Honcho's own `LLM_OPENAI_API_KEY`) and `providers.stages` is empty: Route all
  fills each job's stage with the model `config.toml` gives it; `defaults` holds the values Honcho ships
  with (brief 50, deduction 12 and induction 10 rounds, no effort); `peers` and `pairs` are empty. Copy it to `settings.json` inside the folder named by
  `CONFIG_DIR` in `deploy/.env` (by default `config/` beside `src/`) and edit it there (or through the dashboard).
  The real file is git-ignored.
- `settings.schema.json`: JSON Schema (draft 2020-12). The dashboard and the consumers validate against it.
  `additionalProperties` is false. No string may contain a word that starts with `sk-`, `AIza`, `Bearer ` or `eyJ`
  (key-shaped text; `task-` or `risk-` inside a word are fine).
- `rerank` (optional, top level, `$defs/rerankGlobal`): the global settings of the rerank component (`src/rerank/`), read by Honcho's API and deriver and re-read when the file changes, off unless `enabled` is `true`: `enabled`, `provider`, `model`, `base_url`, `candidates`, `max_documents`, `max_query_chars`, `timeout_seconds`, `callers`, `min_score`, `daily_request_cap`, `failures_before_cooldown`, `cooldown_seconds`, with the names, types and ranges of the component's `RerankSettings`. Never a key: `RERANK_API_KEY` is in Honcho's `.env`.
- `rerank` (optional, in `defaults`, a peer or a pair, `$defs/rerankOverride`): the global fields a dream may replace (not `callers`, `max_query_chars` or the cooldown pair). Each key resolves pair, then peers, then defaults; a key set nowhere keeps the global value. Never an `api_key`.
- `validate.py`: check a file against the schema and its cross-references: every backend that `providers.active`,
  `defaults`, a peer, a pair or a stage names must exist (the active backend and the stages' backends must also be
  enabled), and a `<backend>:<model>` must be in that backend's `models` list when the backend has one. It also names a `rerank` key that must not be there (`api_key`, in the global block or a
  per-dream one). A key-shaped string is reported and not printed.

```
python src/config/validate.py src/config/settings.example.json
python src/config/validate.py path/to/settings.json
```

Exit status is 0 when the schema and the checks accept the file. Given two more files (an older per-peer policy file
and scheduler file, from before `settings.json` held both), it also prints every mapped value that differs; new
installations have no use for that form.

For an upstream source export, pass `--upstream-repo <local-upstream-repo>` and
`--upstream-tag v3.5.0` to `gen_honcho_schema.py` along with the exported `src/config.py`.
The generator resolves the tag locally and records `upstream_tag` and `upstream_sha`,
leaving fork provenance unset. The committed schema uses upstream v3.5.0 with the separate
`summary-output-headroom.patch` applied to a copy before generation; only that patched
source supplies `[summary] OUTPUT_HEADROOM`. No prompts file is produced by this generator.
