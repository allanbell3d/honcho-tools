# Interfaces

The HTTP endpoints, files, logs and records each component exposes. The ones a human or another program would
use; internal details stay in the component READMEs. Everything here is what the code does at this version; the
tests named in `tests/README.md` cover each endpoint against fakes.

## Bridge (`bridge_cli.py`, port 8793 on the Honcho network)

OpenAI-compatible, standard library only. With `BRIDGE_TOKEN` set, every route needs `Authorization: Bearer
<token>`; `GET /health` stays open but reduced.

| Route | Answer |
|---|---|
| `POST /v1/chat/completions` | An OpenAI chat completion, streamed as server-sent events when the request has `stream: true` (one content chunk, one usage chunk, `[DONE]`). The backend is chosen from the body: top-level `backend`, else `extra_body.backend` (`<backend>`, `<backend>:<model>` or a `@<stage>` alias), else `defaults.backend`, else `providers.active`. `usage` adds `backend`, `resolved_model`, `upstream_model` (when the provider reports one), `billing_route` (`api` or `cli`) and, for Anthropic, `prompt_tokens_details.cached_tokens`. |
| `GET /v1/models` | `{"object": "list", "data": [...]}`: one entry per backend (`id` the backend name, `owned_by` its kind) and one `<backend>:<model>` per listed model. |
| `GET /health` | `ok`, `settings_ok`, `settings_error`, `active`, `concurrency`, `timeout_seconds`, `log_ok`, `log_write_errors`, `auth_required`, `unauthorized` (refused requests since start), `routing` (per backend: calls, errors, the last call's route evidence) and the call counters. Without the token, when one is set: `ok`, `settings_ok`, `log_ok` only. |

Status codes: 400 (bad body, unknown backend or stage alias, unknown effort style), 401 (token), 422 (a failure
Honcho's client must not resend: timeout including the wait for a slot, empty answer, invalid JSON after one repair,
missing key variable, CLI not installed), 502 (the provider or the CLI failed), 503 (settings unreadable on first
load). The accepted connection has a 60-second read/write timeout; a stalled client is dropped.

**Call log** `LOGS_DIR/bridge-calls.jsonl`: one JSON object per call with `ts` (UTC, `Z`), `secs`, `in`, `out`
(tokens), `effort` (asked), `sent_effort`, `asked_effort`, `tool_calls`, `final_chars`, `backend`, `billing_route`,
`requested_model`, `resolved_model`, `upstream_model`, `ok`; an error line has `error` instead. No message text, no
key. Rotates from the `logs` block of `settings.json`.

## Telemetry receiver (`receiver.py`, port 8765 on the Honcho network)

| Route | Answer |
|---|---|
| `POST /events` | One CloudEvent or an array of at most 1000, each with non-empty string `id`, `type`, `source`, `specversion`. `Authorization: Bearer <TELEMETRY_TOKEN>` required. 200 `{"accepted": N}` once the batch is on disk. 400 malformed, 401 token, 403 source not in `ALLOWED_SOURCES`, 411 no `Content-Length` or a `Transfer-Encoding`, 413 over `MAX_BODY_MB`, 503 low space, write failure or `{"error": "busy"}` (two bodies already being read). |
| `GET /health` | `status` (`ok` or `low_space`), `day`, `today_bytes`, `free_bytes`, `min_free_bytes`, `accepted`, `forward_sent`, `forward_failures`. |

**Event files** `TELEMETRY_DIR/events-YYYY-MM-DD.jsonl` (UTC day), one line per event:
`{"received_at": "<UTC ISO>", "event": {<the CloudEvent>}}`. Gzipped after `GZIP_AFTER_DAYS`, deleted after
`RETENTION_DAYS`; `maintenance.log` in `LOGS_DIR` records each action. With `VIEWER_URL` set, each accepted batch
is also POSTed there once (`X-Telemetry-Key` when `VIEWER_KEY` is set), best effort.

## Dream hook (inside Honcho's deriver)

**`LOGS_DIR/policy-used.log`**, one line per dream decision:

```
<observer>/<observed> <- <source> {"max_conclusions": 350, "reasoning_effort": "high", "deduction_rounds": 24, "induction_rounds": 20} mode=<stock|scheduler> backend=<backend> gate=<ran|skipped-mode|skipped-disabled|skipped-settings>
```

`<source>` is the block that supplied the effort (`pair`, `observer`, `observed`, `defaults`); the JSON holds the
dream's own limits when set and is JSON. Other lines (reload
notices, `rerank for …`, instruction warnings, deprecations) contain no `{` and no `<-`. Timestamps are UTC with a
`Z`. Rotates from the `logs` block.

**`LOGS_DIR/runs.log`**, one JSON record per completed dream: `run_id`, `workspace_name`, `observer`, `observed`,
`started`, `finished` (UTC), `mode`, `source`, `backend`, `stage_backends`, `brief`, `deduction_rounds`,
`induction_rounds`, and a `rerank` object (`source`, `enabled`, `provider`, `model`, `candidates`, `min_score`,
`daily_request_cap`, `base_url_set`). No URL, key, prompt or memory text. The dashboard's Runs page joins it with
telemetry by `run_id`.

**`LOGS_DIR/hook-status.json`**, replaced atomically after installation and each reload: `checks_passed`,
`installed_at`, `mode`, `settings_path`, `settings_mtime_applied`, `last_reload_error` (exception class or null),
and when the hook could not install, `hook_failed` (exception class) and `dreams_blocked`.

**Inside Honcho**, per dream: the hook stamps `thinking_effort` and `extra_body.output_config.effort` from the
dream's effort, and `extra_body.backend` (`@dream.deduction`, `@dream.induction` or `@peer_card`, or the explicit
backend override), `extra_body.observer` and `extra_body.observed` onto the dream model config the bridge receives.

## Scheduler

**`LOGS_DIR/dream-nas.log`**: `=== start`, `config changed`, `sleeping`, `scheduled <observer>/<observed>`,
`schedule … ->` (the API answer), `running`, `=== done`, each ending with `mode=<mode>`; UTC `Z` timestamps.
Rotates from the `logs` block.

**Honcho API calls**: `POST /v3/workspaces/<workspace>/schedule_dream` with `observer`, `observed`, `dream_type`
and `scheduled_by: honcho-scheduler` (Honcho drops `scheduled_by` and marks the queued dream
`trigger_reason: manual`). Peer and pair discovery reads the `collections` table (`observer` and `observed` columns)
for the workspace.

**Writes**: `settings.json` (appending discovered peers as `enabled: false` and discovered pairs as `{}` in
`scheduler.skip`), after copying the previous file to
`CONFIG_DIR/backups/settings/settings.json.bak-<stamp>` (newest 20 kept), with an mtime check before the replace.

## Backup

`BACKUP_DIR/honcho-YYYY-MM-DD.dump` (PostgreSQL custom format) and `honcho-YYYY-MM-DD.dump.sha256` (relative name,
for `sha256sum -c` from that folder); `LOGS_DIR/backup.log`. `backup.sh --now` exits 0 written, 1 failed, 2 lock
held; `backup.sh --retention-plan <today> <daily> <weekly> <dates…>` prints what the retention would keep.

## Dashboard (port 8010)

One page (`GET /`) and a JSON API under `/api`. With `CONTROL_PASSWORD` set every route needs HTTP basic auth
(`CONTROL_USER`, default `admin`). Every write is `POST` with `application/json`; a write that carries `mtime_ns`
is refused with 409 when the target file changed since that time. Secret-like strings are refused and never echoed.

| Route | Purpose |
|---|---|
| `GET /api/settings`, `POST /api/settings` | Read `settings.json` (with `errors` and `mtime_ns`; `settings` is null when the file cannot be used) and write it after the schema, reference and cap checks. |
| `GET /api/history`, `POST /api/history/restore` | The backup copies, and loading one back (refused on a stale `mtime_ns`). |
| `GET /api/peers`, `GET /api/card` | Peers from the file merged with Honcho's API; a peer card preview. |
| `GET /api/status` | Honcho's queue counts, the dream and scheduler log tails, the telemetry file status and the hook state (`hook.active`, `hook.message`). |
| `GET /api/control`, `POST /api/workspace` | The workspace switches and prompts (Honcho's workspace configuration). |
| `GET /api/instructions`, `POST /api/instructions`, `…/from-default`, `…/from-backend`, `…/workspace`, `…/remove`, `…/switch`, `…/restore` | The instruction files and their copies. |
| `GET /api/containers`, `POST /api/restart/{name}`, `POST /api/stop/{name}`, `POST /api/start/{name}` | The container panel through the Docker socket proxies; `name` must be in `RESTART_ALLOWED` (403 otherwise). Each action is appended to `LOGS_DIR/restart.log`. |
| `GET /api/prompts` | Honcho's prompt texts (`honcho-prompts.json`). |
| `GET /api/models`, `POST /api/models/add` | The model catalogue (`models.json`) and adding one entry. |
| `GET /api/runtime` | The listed environment values (`PGOPTIONS`) of the allowed containers, read through the read-only proxy. |
| `GET /api/honcho-config`, `POST /api/honcho-config/review`, `POST /api/honcho-config/write` | `config.toml` as fields with sources and defaults (`before_route_all_at` says whether a copy from before routing exists), a dry-run diff, and the write (`before_route_all: true` takes that copy first; the write keeps a `config.toml.bak_*` and puts it back on failure). |
| `POST /api/honcho-config/undo-routing` | `{"scope": "jobs" | "file", "mtime_ns": …}`: puts the job tables, or the whole file, back from the copy taken before routing; 409 on a stale mtime or without a copy. |
| `POST /api/honcho-config/apply` | Restart Honcho's `api` and `deriver` (409 while a model block lacks `model` or `transport`, or the container list does not name exactly one of each). |
| `GET /api/bridge-status` | Health of the bridges named in Honcho's dream model config: counters and route evidence, no URL or key. |
| `GET /api/rerank/stats`, `GET /api/rerank/warnings` | The `rerank.call.completed` summary of today and the last 7 UTC days, and the configuration warnings (`callers`, `ef_search`). |
| `GET /api/runs`, `GET /api/cost` | Completed dreams joined with telemetry; list-price cost estimates from `rates.json`. |

**Files the dashboard writes** (each with a copy in `CONFIG_DIR/backups/` first): `settings.json`, `models.json`,
the instruction files, Honcho's workspace configuration (through the API, copy as
`backups/workspace/workspace-config.json.bak_*`), `config.toml` (in place, copies in `backups/config-toml/`,
plus `config.toml.before-route-all` while a routing can be undone), and `LOGS_DIR/restart.log`.

## Rerank component (inside Honcho's api and deriver)

No endpoint. Each search reranking applies to emits one `rerank.call.completed` CloudEvent through Honcho's own
telemetry (`caller`, `workspace_name`, `run_id`, `provider`, `model`, `outcome`, `candidates_sent`,
`results_returned`, `duration_ms`, `tokens`, `billed_units`, `daily_requests`, `daily_request_cap`), and the
routing telemetry extension adds `model`, `requested_model`, `resolved_model`, `backend`, `billing_route`,
`routing_evidence` and `usage_complete` to Honcho's `llm.call.completed` and `llm.call.traced` events
(`docs/EXECUTED-ROUTING.md`). Log lines name the caller, provider and outcome, never the query, a document, a URL or
a key.

## The settings file

`settings.json` is the one interface every component shares; `docs/SETTINGS.md` describes it. `models.json`
(seeded from `src/config/models.json`) and `rates.json` (from `src/config/rates.example.json`, copied by `setup.sh`) sit beside it and are
read by the dashboard only.
