# Changelog

## v0.1.1 — 2026-11-05

- First public release. The v0.0.N entries below are the history before the public repository.

## v0.0.56 — 2026-11-05

- Pairs page: every row has the observer and observed dropdowns again, a peer's own pair included (re-pointing an own pair that is not in the file yet adds the new pair). The list shows each peer's own pair first, in Peers page order, then the pairs between two peers, grouped by observer.

## v0.0.55 — 2026-11-05

- Bridge, effort none: never a value the model refuses. Claude Sonnet 5.5 and later get thinking `between_tools`; Claude Fable, Mythos and Opus 5.5 and later, whose thinking is always on, get the lowest effort (low) instead; `gpt-6-astra` and `gpt-6.1-sol`, which refuse `reasoning_effort: none`, get low.

## v0.0.54 — 2026-11-05

- Effort `none` (thinking off) is valid wherever an effort is accepted (settings, peers, pairs, stage entries, backend defaults, the hook) and in every effort list on the dashboard, labelled "none (thinking off)". The bridge sends each provider its own off switch: `reasoning_effort: "none"` to OpenAI-style and unknown hosts, `reasoning_effort: "none"` with no `thinking` object to DeepSeek (which no longer gets `thinking` enabled), `reasoning: {"effort": "none"}` to OpenRouter, `thinking: {"type": "disabled"}` to Z.ai GLM-4.6 and later except the GLM-5.3 series (which cannot be switched off, so it gets nothing) and to the Anthropic Messages API (no `output_config.effort`, no adaptive thinking; a `thinking` the job sets wins), and `low` to the codex and claude CLIs, which have no off level. The hook stamps `none` on bridge calls only; on a direct Anthropic or Gemini call Honcho cannot use it. The Honcho config fallback list offers `none` on the OpenAI transport only. Tests cover each mapping and the end-to-end harness checks the exact body per backend.

## v0.0.53 — 2026-11-05

- Bridge, Anthropic backend: a routed Anthropic job reaches Anthropic as Honcho's own client sends it. The system prompt is cached (`cache_control` ephemeral), the job's `cache_control` and `thinking` from `config.toml` pass through (prompt caching, thinking switched off), Claude 4.6 and later get adaptive thinking when an effort is sent, `tool_choice` is converted (a forced tool is relaxed to auto while thinking is on, as Honcho does), and `temperature`, `top_p`, `top_k` and `stop` are kept. Before, these were dropped, so routed Haiku calls lost prompt caching.
- Tests: end-to-end cases run a question level set like a live config (effort in `output_config`, thinking off, prompt caching) natively and after Route all, and the deriver with an effort both ways, and check that Anthropic gets the same fields.

## v0.0.52 — 2026-11-05

- Install: `setup.sh` writes `cli-secrets/tokens.env` with `BRIDGE_TOKEN`, `TELEMETRY_TOKEN` and `TELEMETRY_HEADERS` (new random values, or the ones Honcho's `.env` already has), so no token is made up or pasted. Every service of this stack loads it, and the lines for Honcho's `api` and `deriver` add it through `env_file` (with Honcho's `.env` first, which stays untouched).

## v0.0.51 — 2026-11-05

- Route all: each job keeps its effort too (thinking_effort or the provider's `output_config.effort`), moved into its stage; a switched-off backend that a job is routed to is switched on (the job already calls that provider, so its key is in Honcho's `.env`).
- Settings example: the `anthropic` and `gemini` backends read Honcho's own key names, `LLM_ANTHROPIC_API_KEY` and `LLM_GEMINI_API_KEY`.
- Honcho config, fallback table: a model behind its own base_url is listed under a provider named after that host (a proxy or DeepSeek) instead of under its transport.
- Peers: switching a peer Off also switches Off a peer left with no running pair.
- Pairs: in `stock` mode the page says that Honcho starts dreams itself and only a peer switch stops them.

## v0.0.50 — 2026-11-05

- Honcho config, Route all: when several backends serve a job's model (the Vertex proxy and the Gemini API both serve `gemini-3.5-flash-lite`), the backend on the job's own base_url host is picked instead of the active model. The example's `providers.stages` is empty again, so Route all fills each stage from the job's current model (on a stock Honcho, `gpt-5.4-mini`) instead of keeping a pre-filled `gpt-5.4-mini` over a job that used another model.
- Honcho config, fallback table: a fallback model reached through its own base_url (a proxy, DeepSeek) shows under its transport with the host instead of "Choose…"; an effort config.toml keeps in `extra_body.output_config.effort` shows and is edited there instead of "Model default".

## v0.0.49 — 2026-11-05

- Settings: the example's `defaults` hold the numbers Honcho ships with (brief 50, deduction 12 and induction 10 rounds) instead of being empty; effort stays unset, the model's own default. The dashboard no longer shows a bare "Honcho" for a value set nowhere: numbers show "—" and effort shows "Model default".
- Dashboard, Pairs: "Add pair" adds the pair picked beside it (observer → observed, starting on the first peer without a pair of its own) and says which pair it added, instead of adding an unnamed row.
- Dashboard, Rerank: a key set nowhere shows what the component uses, not a blank: the provider's API root, its documents per request (1000; 32 for tei), no minimum score, no daily cap.
- Dashboard, Models and Advanced: the schema line reads "Config fields from Honcho 3.5.0 (commit 0cd37dd)", the release the config fields were read from, instead of "Fork version unknown · SHA unknown" on a stock Honcho; a fork shows its name and commit.
- Catalogue: `models.json` lists each main provider's API text models as of 2026-11-05 (OpenAI, Anthropic, Google Gemini, DeepSeek, xAI, Mistral, Qwen, Z.ai GLM, Kimi; Claude CLI adds Sonnet 5.5, Opus 5.5 and Fable 5.1), and Honcho's own jobs get Gemini models. `rates.example.json` prices every listed model from each provider's official pricing page, plus the Voyage rerankers; Cohere and Jina rerankers stay unpriced (no official per-token price), as do Qwen cache reads. The example adds disabled backends for Gemini, xAI, Mistral, Qwen (its workspace endpoint has a `{WorkspaceId}` to fill in) and Kimi, and GLM's base URL is Z.ai's real one.
- Pairs found in Honcho: in `scheduler` mode the scheduler appends every observer/observed notebook (Honcho's `collections` table) missing from `pairs`, Off (in `scheduler.skip`), as it already did for peers. The Pairs page lists each peer's own pair too, and the two pages' switches stay in step: a pair switched On turns its peers On, keeping their other pairs Off; a peer whose pairs are all Off goes Off; a peer switched Off turns every pair with it Off. The bulk switch follows the same rules.

## v0.0.48 — 2026-11-05

- Settings: the example routes every stage, and the Dreams fallback (`providers.active`), to Honcho's stock model, OpenAI `gpt-5.4-mini`, through a new `openai` backend that reads Honcho's own `LLM_OPENAI_API_KEY`. "Route all jobs through the bridge" on a stock Honcho therefore keeps every job on the model it already had. `models.json` lists the model for dreams as well.

## v0.0.47 — 2026-11-05

- Settings: `settings.example.json` is a working stock start. Backends name real catalogue models, `rerank.model` is `rerank-3-lite`, and `defaults`, `peers` and `pairs` are empty, so a first boot shows no placeholder peer or pair and every dream keeps Honcho's own values. The schema makes every `defaults` key optional: a field set nowhere keeps Honcho's value from `config.toml`, and the dashboard shows it as "Honcho".
- `models.json` lists Honcho's stock model, OpenAI `gpt-5.4-mini`, for Honcho's own jobs.
- Install: `setup.sh` also copies `src/config/rates.example.json` to `config/rates.json`, so the Cost page has public list prices from the first boot.

## v0.0.46 — 2026-11-05

- Install: `setup.sh` prepares a fresh checkout on the Docker host. It writes `deploy/.env` (with `HONCHO_ROOT`, `../honcho` by default) and `config/settings.json` from the examples, keeps files that exist, gives the logs and CLI-login folders to uid 1000, and prints the lines for Honcho's compose file with this host's paths filled in. It builds nothing and never edits Honcho's files; `docs/SETUP.md` lists the same steps by hand.
- The six live folders (`config`, `data`, `logs`, `telemetry`, `backups`, `cli-secrets` with `codex/` and `claude/`) ship empty with a README each; git ignores what the stack writes into them. `SECRETS_DIR` defaults to `cli-secrets/`.
- Docs: the quick start is clone, `setup.sh`, build; `deploy/.env.example` explains `BIND_IP`, `BRIDGE_LOG_GID`, the optional commented lines and the one-workspace limit (in `scheduler` mode, dreams in other workspaces do not run), now also in Known limitations; `honcho-mounts.yaml` says to use one of the two deriver entrypoints.
- Docs: dashboard screenshots with demo data in `docs/images/`: the Models page at the top of the README, Overview, Honcho config, Peers and Runs in the dashboard README.

## v0.0.45 — 2026-11-05

Fixes and the dashboard's one-file model choices.

- Hook: runs without the rerank package (one log line, searches stay on the vector order). A settings file with a bad `rerank` block keeps dreams running; the scheduler applies `mode` and the peers when `providers` is invalid. The secret scan covers only the values each component reads. If the hook fails to start or cannot be imported, intake keeps running: in stock mode with every peer and pair on, Honcho dreams on its own; otherwise dreams are blocked (`DREAM_ENABLED=false` and the loaded flag) and the dashboard's hook line says so.
- Settings writes: the dashboard and the scheduler re-check the file's mtime before replacing it (409, or a retry at the next poll); the scheduler flushes and fsyncs; a settings restore with a stale mtime is refused. The scheduler closes a failed database connection before dropping it.
- Logs: `policy-used.log`, `runs.log`, `dream-nas.log`, `restart.log` and `bridge-calls.jsonl` rotate from the optional `logs` block (`max_mb` 10, `keep` 1); every hook, scheduler and bridge timestamp is UTC with a `Z` suffix.
- Rerank: a failing widened fetch falls back to the stock search and records `internal_error`; the README says the daily cap is shared only with Honcho's Redis cache.
- Bridge: the 570 s call budget starts when the request arrives, so a wait for a slot cannot push a call past Honcho's client timeout (422 instead, provider never called). Honcho's JSON request reaches every provider kind in its own form, including Anthropic. A backend `key_env` must end in `_KEY` (bridge and schema). Codex and Claude Code CLIs are opt-in at image build (`INSTALL_CODEX`, `INSTALL_CLAUDE`, default 0); a call to a missing CLI answers 422. No version pins: base images on minor tags, CLIs and packages at the latest at build, tested versions in the READMEs.
- Cost: DeepSeek calls are priced by their UTC timestamp with the off-peak factor from `rates.example.json`.
- Dashboard, Honcho config: a new page, the one place that writes `config.toml`. "Route all jobs through the bridge" moves each unrouted job's primary into Honcho's stock `fallback` (Honcho native) and routes the primary through the bridge, once, with undo; existing stage entries are kept. A per-job fallback table with model, effort and a plain-words column; the dream knobs, the summary output headroom and Restart Honcho moved there. A fallback provider change drops `structured_output_mode` and the old provider's overrides; undo writes only what the original block had.
- Dashboard, Models: a model or effort pick on a routed job writes only `settings.json`; a `providers.stages` entry may be `{"model": ..., "effort": ...}` and the bridge applies that effort between the request's and the backend's default. Honcho-native picks and the Embedding row are gone (the embedding model is set once in `config.toml` at install). The dashboard's reference check covers stage objects.
- Tests: `tests/e2e` drives the new flow; `tests/test_hook_entrypoint.py`, `tests/test_backup.py` and the dashboard tests cover the fixes above.
- Dashboard, Honcho config, round 2: Route all keeps a job's own `fallback` and says so ("Kept your own fallback: …"); on jobs already routed it clears a `thinking_effort` or `extra_body.output_config.effort` left on the primary, which reached the bridge as the request's effort and beat the per-job pick; it sets `providers.stages.peer_card` from `dream.deduction` when unset. The Write that carries Route all's edits keeps `config.toml.before-route-all` in the config-toml backup folder, and two buttons read it: "Undo routing (jobs only)" puts every job table back from the copy and keeps every other change, "Restore config.toml from before routing" puts the whole file back byte for byte; both need a Honcho restart and refuse a stale page or a missing copy. The field-by-field undo is gone. A failed `config.toml` write puts the backup taken at its start back in place before failing.
- Dashboard: with `settings.json` unreadable, the pages that need it (Overview, Models, Peers, Pairs, Rerank, Instructions, Advanced) show the server's reason in one box; Honcho config, Runs, Cost and Logs draw from their own data.
- Bridge: Anthropic answers carry `prompt_tokens_details.cached_tokens` from `cache_read_input_tokens`, so a cache read is no longer billed as fresh input by Honcho's usage parser. Accepted connections get a 60 s read/write timeout (`SOCKET_TIMEOUT_SECONDS`); a client that stalls after its headers is dropped and its thread released. The 570 s call budget is unchanged.
- Tests: the provider JSON test stubs the CLI presence check (passes without `codex` or `claude`); the policies tests restore `sys.modules`, so the entrypoint tests pass in any order; `tests/measure_pages.py` is out of the default run (`python -m pytest tests/measure_pages.py` runs it).
- Docs: `docs/HONCHO-CONFIG.md` describes the Honcho config page as built; `deploy/README.md` gains the install check that proves a job completes on its Honcho-native fallback while the bridge is stopped, and the copy to put back after a crash during a dashboard write.
- Fixes: Route all also drops an old `extra_body.output_config.effort` from the job it routes (the fallback keeps it); an effort picked for a fallback that Route all staged is the one shown and written; a Write that stops after `settings.json` can be finished with a second Write (`settings.json` is not sent again); the hook reads a `providers` value that is not an object as no stage entries instead of failing the dream.
- Dashboard, Honcho config: the copy from before routing is taken only when none exists and is removed after a successful undo, so a second Route all cannot replace it with an already routed file. Both undo buttons arm first (click again); Route all is disabled while `settings.json` cannot be used; the "every other change" warning names only the whole-file restore.
- Docs: new guides `docs/SETUP.md` (install, step by step), `docs/SETTINGS.md` (every key of `settings.json`), `docs/OPERATIONS.md`, `docs/TROUBLESHOOTING.md` and `docs/INTERFACES.md` (endpoints, files, logs); the README, `docs/ARCHITECTURE.md`, the component READMEs and `tests/README.md` describe the current flow (Route all, the Honcho-native fallback, hot picks, the undo copy, the three-level effort precedence, what happens when the hook or the reranker cannot install).

## v0.0.44 — 2026-11-04

- Docs: `docs/MIGRATION.md`, `deploy/MIGRATION.md` and a routing document left the repository, with every link to them. The README is the front page of Honcho Tools: what each component needs and what stops when it is stopped, quick start, configuration, status and limits, the benchmark headline. Component docs lost stale and installation-specific wording (dashboard page list, telemetry switch-over steps, the rerank response cap, the optional summary patch).

## v0.0.43 — 2026-11-04

- Bridge: optional shared token. With `BRIDGE_TOKEN` set, every route needs `Authorization: Bearer <token>` (constant-time compare, checked before the body is read; 401, connection closed). `/health` stays open but answers only `ok`, `settings_ok` and `log_ok` without the token. Unset or blank: open as before. Honcho sends it through `api_key_env = "BRIDGE_TOKEN"` in each bridge model block. A body that fails to read now closes the connection.
- Telemetry: the body cap is `MAX_BODY_MB` (default 64, was a fixed 100 MiB); at most two bodies are read at once (a third waits 30 s, then 503); `Transfer-Encoding` gets 411; a signed, non-digit or repeated `Content-Length` gets 400; at most 1000 events per post, each with non-empty `id`, `type`, `source` and `specversion`. Deep nesting and integers past Python's digit limit ended the handler thread without an answer; they now get 400.
- Backup: a failed dump is retried up to `BACKUP_ATTEMPTS` (8) with a doubling wait from `BACKUP_RETRY_SECONDS` (30) to 15 minutes; a wrong password, unknown role or database, a pg_hba refusal and a version mismatch are not retried. A dump is published only after `pg_restore --list` reads its table of contents and finds a table. `backup.sh --now` runs one dump on demand.
- Backup: `restore.sh` with `check`, `scratch` and `live --yes` (restores into a copy, then swaps the names in one transaction and keeps the old database). `restore.md` is rewritten around it.
- Rerank: a provider answer with more rows than requested is a malformed response, and the body is refused past 4 MB; both keep the vector order.
- Images: every base image is pinned to a patch tag (`python:3.12.16-slim-trixie`, `node:22.23.4-bookworm-slim`); the dashboard and scheduler install pinned `requirements.txt` files; the bridge's CLIs are build arguments (codex 0.166.0, claude 2.1.311) with the auto-updater off.
- Telemetry, backup and the scheduler start again with only the environment variables they read; Honcho's whole `.env` still reaches the container through `env_file`. The dashboard and the bridge keep the whole file, and `deploy/README.md` says why.
- `deploy/README.md`: what each container sees, what each Docker socket proxy allows, and what stops working when each service is stopped alone. The claim that a `:ro` socket mount makes the Docker API read-only is corrected.
- Benchmark: `bench/` holds the rerank benchmark: the replay, judging and scoring harness (`bench/scripts`), a seeded fictional dataset that runs the whole pipeline offline (`bench/data/synthetic`), an offline test suite (`bench/tests`), and the aggregate results of four studies with their method and provenance (`bench/README.md`, `bench/METHOD.md`, `bench/results`). `pytest.ini` collects `bench/tests` too.
- Tests: `tests/test_backup.py` runs both backup scripts against fake PostgreSQL tools; bridge token, receiver framing, rerank contract, pinned-image and stack-independence tests added.

## v0.0.42 — 2026-11-04

- Bridge: the provider decides how effort is sent: the backend's new `effort_style` field (`openai`, `deepseek`, `openrouter`, `glm`, `anthropic`, `none`), else the `base_url` host, else a generic `reasoning_effort`. `effort` and `thinking_effort` on a backend are only its default level; a `thinking_effort` key used to switch OpenAI, GLM and proxy backends to DeepSeek's field.
- Bridge: DeepSeek gets `thinking: {"type": "enabled"}` and a top-level `reasoning_effort`. The effort never reached it before, so every DeepSeek call ran at its default.
- Bridge: each provider gets the nearest level it documents: `minimal` becomes `low` for Anthropic, the Claude CLI, the Codex CLI and OpenAI; DeepSeek and GLM get `low`, `high` or `max`, GLM only from GLM-5.2; OpenRouter and unknown hosts pass the level through. The call log records `sent_effort`.
- Bridge: OpenAI requests carry `max_completion_tokens` instead of `max_tokens`, which its reasoning models refuse.
- Bridge: a `json_schema` response format to DeepSeek or GLM, which document `json_object` only, goes out as `json_object` with the schema in the system message.
- `docs/EXECUTED-ROUTING.md` describes the deployment steps without one particular setup.
- Tests: `tests/e2e/` drives every Models-page choice through the dashboard, Honcho 3.5.0's own settings and LLM backends, the hook and the bridge to fake providers and fake CLIs (35 tests; skipped without a Honcho tree, Playwright and Chromium).

## v0.0.41 — 2026-11-04

Fixes from an end-to-end run that pushed every Models-page choice through Honcho 3.5.0 and the bridge to fake providers.

- Models page: picking a direct Anthropic model on a dream row kept `structured_output_mode = "json_object"`, which Honcho 3.5.0 refuses at start-up on the anthropic transport. The pick now clears it, and a bridge pick on a dream row sets it back. Review and Apply list such a block as a gap, and Apply refuses it.
- Models page: effort is written as Honcho's own `thinking_effort` on every row. Direct rows wrote `extra_body.output_config.effort` instead, which overrode Honcho's mapping (so "minimal" reached the Anthropic API unmapped) and left the old `thinking_effort` in place; blank cleared only one of the two. An old `output_config.effort` is still shown and is removed on the next write.
- Models page: a direct pick kept the row's effort (it was set to none, turning adaptive thinking off) and removes the row's bridge stage from `providers.stages`, which otherwise kept naming the bridge model.
- Models page: with the hook active, the dream rows' effort cell says the effort comes from the per-dream settings (Peers, Defaults row); the value in `config.toml` was never used for dreams.
- Hook: a dream model on a direct transport no longer gets the bridge's `backend`, `observer` and `observed` fields, which the Anthropic Messages API refuses, nor a raw `output_config.effort`; the per-dream effort goes to `thinking_effort`, which Honcho maps.

## v0.0.40 — 2026-11-04

- Licence: GNU Affero General Public License v3.0, the licence of Honcho itself (the rerank patch and the extracted prompts and schema derive from Honcho). Added `THIRD_PARTY_NOTICES.md` and `CONTRIBUTING.md`.
- Dashboard: the hook line shows the time the settings were applied, not the raw nanosecond file time. A settings file newer than the hook's last reload now reads "settings saved … apply at the next dream": the hook re-reads `settings.json` when a dream starts, so the old "settings are stale" warning appeared after every save made between dreams.
- Dashboard, Runs: start times are shown in UTC as the page says. They were converted to the browser's zone (four hours off in a UTC+4 browser). The rerank cap time on the Rerank page's Today block is UTC as well.
- Dashboard, Advanced: the time limit per call says the bridge stops a call at 570 seconds, and turns red above it.
- `rates.example.json`: Voyage `rerank-3` at 0.05 USD per million tokens (list price, checked 2026-11-04). Calls to it showed "no rate" on the Cost page.

## v0.0.39 — 2026-11-04

- `bridge-calls.jsonl` moves to `bridge-calls.jsonl.1` at 10 MB and `dream-nas.log` to `dream-nas.log.1` at 5 MB, one previous file kept each. Both grew without end; a refused schedule call wrote one line per pair per poll.
- Rerank page: **Today** counts only events dated today. Late events dated earlier sit in today's file too, and were counted in Today as well as in the week.
- README: this layout is deployed and has run live dreams and rerank calls since 2026-11-03; the README said it was not deployed and that rerank had never met a real provider.

## v0.0.38 — 2026-11-04

- Backup: the database host, port, user and name come from `PGHOST`, `PGPORT`, `PGUSER` and `PGDATABASE` in `deploy/.env` (defaults `database`, `5432`, `honcho`, `honcho`), and the password from `PGPASSWORD` or `POSTGRES_PASSWORD`. They were hard-coded, and without `POSTGRES_PASSWORD` the job skipped every dump with one log line while the container looked healthy; Honcho's own compose example uses user and database `postgres`.
- `validate.py` checks `providers.stages`: a stage naming an unknown or disabled backend, or a model outside its `models` list, is reported. Such a stage passed and the bridge then refused every call of that stage.
- The dashboard binds Honcho's `config.toml` at `/honcho/config.toml`. Bound inside `/config`, Docker left an empty `config.toml` mountpoint in `CONFIG_DIR` beside `settings.json`.

## v0.0.37 — 2026-11-04

- Hook: a `settings.json` that exists but was never accepted since the deriver started skips every dream (`gate=skipped-settings`) until a good file is saved. Before, the hook fell back to `stock` and let all of Honcho's own dreams run in a deployment set to `scheduler`. A missing file is still stock Honcho.
- Hook: a pair's dream limits (`notes` among them) now hold for the whole of Honcho's `process_dream`, not only inside `run_dream`. A per-dream `notes` value with Honcho's notes and brief both off was seen inside the dream but not in the bookkeeping after it, which then cleared the stored cursor after every dream.
- Rerank: the global `enabled` switch now stops reranking everywhere; a per-dream block may turn it off, never on. Before, a block with `enabled: true` kept calling the provider after the global switch was turned off.
- Rerank: `RERANK_API_KEY` is sent only to the global block's provider and host. A per-dream block that named another provider or `base_url` received the key, over plain `http://` too.
- Rerank: a `model` must be printable text; a newline in it could write a forged record into `policy-used.log`.
- Route telemetry: an error while adding the executed model to an LLM call's telemetry is logged and the call goes on; it replaced the call's answer before. When a Honcho upgrade moves one of the names it wraps, the API and the deriver start without it (one warning) instead of stopping.

## v0.0.36 — 2026-11-04

- Bridge: a call is cut at 570 seconds at most, whatever `providers.timeout_seconds` says. Honcho's OpenAI client gives up after 600 seconds and sends the call again twice, and Honcho retries on top, so a CLI call longer than that ran up to nine times. The example's 900 is now 570.
- Bridge: failures that the same request would meet again (a timeout, an empty answer, invalid JSON after the one repair, a missing key variable) answer 422, which the OpenAI client does not resend; other upstream errors stay 502.
- Bridge: a CLI that times out is stopped with its whole process group; the npm launcher's native child kept running and spending before.
- Bridge: the Codex and Claude Code CLIs get the bridge's environment without keys, tokens, passwords or connection URIs. An `ANTHROPIC_API_KEY` in Honcho's `.env` made the claude CLI bill the API while the call was logged as `billing_route: cli`, and every key was readable by the CLI.
- Bridge: the effort field follows the backend's host first. Adding a `models` list to the DeepSeek backend switched its effort to OpenRouter's format.
- Bridge: a negative `Content-Length` is refused instead of holding the connection, bodies are capped at 16 MB, and a refused request's message is filtered like every other error.

## v0.0.35 — 2026-11-04

- Scheduler: a pair whose last dream left its unread conclusions as they were is no longer queued again at every poll. A failed dream, a card refresh, or a dream with briefs off never moves Honcho's cursor, and Honcho's schedule route has no time gate, so such a pair was dreamed back to back without end. It now waits 15 minutes, doubling up to 24 hours, until its unread conclusions change.
- Scheduler: `settings.json` backups are named from one clock reading. Seconds and nanoseconds read apart could sort a newer copy before an older one, and rotation then deleted the newer copy.
- Scheduler: a database that refuses the connection is logged as `database unavailable (<driver error> <SQLSTATE>)` like every other database error; the connect error named the host, port and user.
- Scheduler: a `settings.json` with a list where an object belongs is refused with the reason, instead of crashing the process at every start; an error in one scheduling pass is logged and the loop goes on. A refused file is logged once per saved version, with the reason.

## v0.0.34 — 2026-11-04

- Honcho config: choosing Honcho's default `model` or `transport` for a model block now writes it to `config.toml`. Before, a value equal to the default was skipped, so the block stayed incomplete and **Apply** stayed refused (409).
- A peer name longer than 200 bytes is refused (400) instead of failing with 500 on the file calls.
- A `models.json` or `settings.json` with an unexpected shape (a `dream` object instead of a list, a peer block that is not an object) no longer makes **Models**, **Add model** or the rerank warnings fail with 500.
- Every dashboard write is flushed to disk before the rename, so a power cut leaves the old or the new file, not an empty one.
- The rerank page's day cache drops files that left the 7-day window instead of growing for the life of the process.

## v0.0.33 — 2026-11-04

- Dashboard login: with `CONTROL_PASSWORD` set (Honcho's `.env`), every route needs an HTTP basic login as `CONTROL_USER` (default `admin`). Blank or unset, the page stays open with no login, as before. `CONTROL_USER` and `CONTROL_PASSWORD` were documented but not read.
- Every dashboard write must be `application/json`. A page on another site could post `text/plain` or bodyless requests (save settings, stop a container) from a browser on the same network; those now get 415. Responses refuse framing.
- A request body with `NaN` or `Infinity` is refused. Before, `temperature: NaN` was written to `config.toml` and the request then failed with 500.
- A deeply nested telemetry line no longer makes the Runs, Cost and rerank pages fail with 500 for the day; it is skipped like any bad line.
- `/api/status` no longer fails with 500 when `settings.json` cannot be read.

## v0.0.32 — 2026-11-04

- The secret check that every component runs on `settings.json`, instruction text, peer names and log lines now matches only a word that starts with `sk-`, `AIza`, `Bearer ` or `eyJ`. Before, `task-runner` or `risk-model` matched `sk-`: such a peer was never discovered, and one such word anywhere in `settings.json` made the hook, the scheduler and the dashboard reject the whole file.

- Added an optional bridge supplementary group for log permissions while retaining the node user and existing CLI working-directory ownership.

- Added response-proven model/backend/billing metadata to both LLM telemetry streams, including isolated streaming usage accounting, through the existing mounted runtime entrypoints. Runs shows observed model groups separately from configured fallbacks and aliases; Cost uses recorded route evidence and pricing aliases. Bridge health now exposes bounded per-backend call evidence and log-write failures without credentials.

- Fixed Peers and Pairs model labels to show configured dream stages, inherited overrides in runtime precedence order, and the fallback only when it applies. Hover text identifies individual stage models and overriding settings.

- Added verified public rates for Haiku 5.5, DeepSeek Flash, Gemini 3.5 Flash Lite and Voyage rerank-3-lite, with source links and visible estimate assumptions. Cost estimation supports Haiku's long-context rate tier.

- Added the Vertex proxy connection and Gemini 3.5 Flash Lite to the example providers and model catalog.

- Fixed model selection for backends without an explicit `models` list: dashboard and offline validation now accept model overrides consistently with the bridge, including manually added catalog models. Explicit model lists still apply.

## v0.0.31 — 2026-11-03

The project is now called honcho-tools: a set of independent components for a Honcho 3.5.0 deployment. Move an existing installation with `deploy/MIGRATION.md` before redeploying.

- The code moved under `src/`: `src/bridge`, `src/scheduler`, `src/dashboard` (was `control/`), `src/telemetry`, `src/rerank`, `src/backup`, `src/hooks` and `src/config` (templates only). The compose file, Honcho's mount lines and the tests use the new paths.
- The compose project is `honcho-tools`, so the containers are `honcho-tools-<service>-1`. The dashboard service is called `dashboard` (was `control`). The dashboard's restart list names the new containers; Honcho's `honcho-api-1` and `honcho-deriver-1` stay.
- `settings.json` and `models.json` live in their own config folder, `CONFIG_DIR` (default `config/` beside `src/`), mounted at `/config`: read-write for the dashboard and the scheduler (peer discovery writes the file), read-only for the bridge and Honcho's api and deriver. `CONTROL_FILE` defaults to `/config/settings.json` everywhere, the rerank component included.
- No container mounts the Honcho folder any more. `HONCHO_ROOT` is used only for Honcho's `.env` (loaded by compose on the host) and for `config.toml`, bound as one file into the dashboard at `/config/config.toml`.
- Every backup copy goes to `config/backups/`: settings, models, `config.toml`, instruction files and the workspace configuration. The scheduler writes its `settings.json.bak-*` copies there directly, so the dashboard no longer moves them on start. `DATA_DIR` holds only `instructions/`.
- The telemetry `maintenance.log` and the backup `backup.log` moved to the logs folder (`LOGS_DIR`), with every other log. The telemetry service now gets `DATA_DIR=/data` from compose instead of the host path from `.env`.
- Each live folder (`config/`, `data/`, `logs/`, `telemetry/`, `backups/`) is a variable in `deploy/.env` with a default beside `src/`, and the repository ignores them.
- The bridge is no longer published on the host: Honcho reaches it on the shared network. Its image installs the `codex` and `claude` CLIs again, so it works on its own.
- The standalone `telemetry/compose.yaml` and `backup/compose.yaml` are gone; the main stack runs both services.
- The rerank component no longer carries its own version number: `VERSION` is the only one. It is part of this repository under its GPL-3.0-or-later licence.
- Added `requirements-dev.txt` for the tests and `.gitattributes` for line endings.

## v0.0.30 — 2026-11-03

- Rerank is part of this project: the `rerank/` folder holds the component (`rerank/honcho_rerank`), its tests and the one Honcho source patch with its apply script. Mount `rerank/honcho_rerank` into Honcho's api and deriver instead of the separate honcho-rerank checkout. The code is unchanged.
- Rerank is configured only by the `rerank` block of `settings.json`. It is off by default (`"enabled": false`); while it is off, or when `settings.json` is missing, Honcho's searches are its own, unchanged, and a test checks both cases. The **Rerank** page switch writes `rerank.enabled`, and Honcho picks it up without a restart. The dashboard writes nothing under `[rerank]` or `[db]` in `config.toml`.
- One config folder: `settings.json`, `models.json` and `rates.json` live in the Honcho folder (`HONCHO_ROOT`) beside Honcho's `config.toml` and `.env`. Every service mounts that folder at `/control`. `CONTROL_DIR` is gone: move your files as `deploy/MIGRATION.md` lists before redeploying.
- One data folder, `DATA_DIR` (new in `deploy/.env`): the dashboard's `instructions/` and `backups/` live there, including the `config.toml` and `models.json` copies. The deriver reads the instruction files from it (`INSTRUCTIONS_DIR`, mounted read-only).
- One logs folder: every component writes only into `LOGS_DIR`, mounted at `/logs`: the hook's `policy-used.log`, `runs.log` and `hook-status.json`, the bridge's `bridge-calls.jsonl`, the dashboard's `restart.log` and the scheduler's `dream-nas.log`. The old fallbacks beside the code and in the control folder are gone. The deriver gets `POLICY_LOG` and `POLICY_STATUS` from `deploy/honcho-mounts.yaml`.
- The bridge is a service of this stack (`deploy/compose.yaml`), with its Codex and Claude CLI logins mounted from `SECRETS_DIR` (new in `deploy/.env`). No service depends on another. The bridge and the dashboard start without `settings.json` (one log line, defaults until the file appears) and without the scheduler.
- `restart-allow.json` is gone. The containers the dashboard may restart, stop or start are a fixed list in the dashboard code. **Apply** on the Honcho config page is now offered when the list holds compose names such as `honcho-api-1`; before, it needed the bare names `api` and `deriver`.
- `deploy/honcho-mounts.yaml` shows the one-time change to Honcho's compose file: the two entrypoints, the mounts and `PGOPTIONS`. A Honcho upgrade needs only the patch re-applied and these lines kept.
- `deploy/MIGRATION.md` is a checklist for moving a running installation into this layout. `deploy/control/` (the allow-file example and the instruction-folder note) left the repository; the note is now in `control/README.md`.
- The test suite no longer writes a status file or a restart log outside its temporary folders.

## v0.0.29 — 2026-11-03

- The policy hook also replaces the deriver consumer's process_dream binding when it was imported before installation, so chained entrypoints keep the scheduler payload gates effective.
- Completed dream run records read the current global rerank settings after a hot reload instead of the install-time snapshot, while retaining settings already resolved for that dream.

## v0.0.28 — 2026-11-03

- Regenerated the Honcho config schema from upstream v3.5.0 with the separate summary output-headroom patch applied. Export generation records the upstream tag and commit; rerank and database settings stay outside config.toml.
- Documented the wrapper source mount, API entrypoint, chained deriver entrypoint and PGOPTIONS on both Honcho services in the mounts example. The scheduler compose defines neither Honcho service.
- The policy hook validates global and per-dream rerank blocks through the wrapper's RerankSettings when available, or the JSON schema offline. A rejected reload logs one line and keeps the last good settings; regression tests cover rejection and recovery.

## v0.0.27 — 2026-11-03

- Rerank moved out of the Honcho fork into the `honcho_rerank` wrapper. The hook patches `src.honcho_rerank` and refuses to start, with a log line, when the wrapper is missing or only the old `src.rerank` fork module exists. Per-dream proxy, clamp and run line are unchanged.
- `settings.json` has a top-level `rerank` block (global defaults for the API and the deriver, hot) in the schema and example; the per-dream blocks keep their place and cannot raise its `daily_request_cap`. The Rerank page edits it as "Global (api and deriver, hot)". Warnings read `ef_search` from the deployed `PGOPTIONS` (container environment through the read-only proxy, else the dashboard's own), shown as unknown when unreadable.
- The dashboard and config schema no longer carry `[rerank]` or `[db]` HNSW fields; `[summary] output_headroom` stays. Deploy lines and the README show the chained deriver entrypoint, the API entrypoint, the `src/honcho_rerank` mount and `PGOPTIONS`. The opt-in integration test runs against the wrapper (`HONCHO_RERANK_DIR`).

## v0.0.26 — 2026-11-02

- CLI bridge backends support tool-free plain/JSON completions, pass requested schemas to native CLI output enforcement, and share one timeout across at most one JSON repair. Usage includes reported tokens across attempts. CLI token/sampling/cache limits are stated in Models.
- Scheduling off is labelled as retaining overlays; stock mode no longer performs peer discovery. The manual MCP/SDK scheduling gate behavior is documented.
- Bridge stage aliases support hot provider selection after one config alias setup/restart; dream deduction, induction and peer-card aliases remain independent unless a per-dream backend overrides them. Existing settings without stage entries keep their fallback behavior. Models merges provider parameters and labels hot routing versus initial restart.
- CLI model overrides work without a models list. Scheduler dream types match Honcho (omni/card_refresh). Actual environment overrides lock config fields; bridge health reports rejected settings while serving the last good copy.
- Cost views estimate priced API usage by UTC day/stage and run, separate subscription tokens and unknown-route potential estimates, and label missing rates/partial telemetry. Configure rates.json in the shared control volume (or RATES_FILE); cache writes require their own rate. Bridge usage and call logs identify CLI versus API billing routes.
- Completed dreams record their run ID and resolved policy/rerank settings without credentials or URL values. The Runs page filters by pair/date and joins bounded telemetry for outcome/token/duration metrics, preserving unknown outcomes and missing data. Saved rerank blocks warn when dream callers are disabled or an HNSW pool exceeds ef_search with scan off.
- Hook installation checks required fork fields and callable signatures before patching, refusing to start on incompatible forks. A shared status file reports applied settings and reload errors on the Overview. The install guide includes the required entrypoint and writable status mount.
- Generated Honcho config schemas record fork SHA and version heading, follow extracted settings modules, and show a warning when recent telemetry reports a different Honcho version.
- Per-dream rerank caps cannot raise Honcho's base cap: the hook clamps and logs the change, and dashboard validation refuses it. An unset base cap still permits a per-dream cap.
- Per-dream `failures_before_cooldown` and `cooldown_seconds` are rejected because cooldown state is shared across dreams. Configure these only in Honcho.
- The hook rejects dream rounds above 100 with a logged reason, matching the schema. The Rerank page clarifies when request counters are shared across processes.

## v0.0.25 — 2026-11-01

- One folder for the text logs: `LOGS_DIR` in `deploy/.env` holds `dream-nas.log`, `restart.log` and `policy-used.log`. The dashboard mounts it once at `/logs/stack`; the paths live in `.env`, not in the code. Add `LOGS_DIR` to `deploy/.env` before redeploying, or compose refuses to start.
- `restart.log` follows `RESTART_LOG` when it is set (compose sets `/logs/stack/restart.log`); unset, it stays beside the allow file as before.
- `deploy/honcho-mounts.yaml` shows the deriver lines that put `policy-used.log` in the same folder (`POLICY_LOG` plus a mount of that folder).

## v0.0.24 — 2026-10-31

- Rerank monitoring hardened. Every number read from an event is checked finite and within JSON range; a bad value (`1e309`, a huge integer, NaN) skips that metric for that event while the event still counts by outcome, aggregates are checked before they leave, and the route serialises with `allow_nan=False`, so one malformed event can no longer break the page for its seven-day window.
- Event files are read in bounded chunks: an oversized line is drained and skipped without being held whole; one request reads at most a fixed number of bytes and events, each period keeps a bounded sample of durations for p50/p95, and the response and the page say when a limit stopped the scan. A past day's aggregate is cached by file size and mtime, so only today's file is re-read per request.
- A corrupt gzip body (`zlib.error`, `EOFError`) is listed as unreadable like a bad header instead of failing the route.
- "Sent requests" and the duration percentiles count every call with candidates sent, whatever its outcome (cancelled and internal errors included), as the page claims.
- The request counter is shown as "Requests vs cap, last observed" with the time of the last event that reported it; a later skip no longer blanks it, and the page says it is per UTC day across processes and only as current as that event.

## v0.0.23 — 2026-10-31

- The `rerank.base_url` of `settings.json` follows Honcho's own rule in the hook and in the schema: http or https, a host, no credentials (`user:pass@`), no query, no fragment, no control characters. A file that breaks it is not applied. The dream log prints `base_url=set` instead of the URL, valid or not, so a credential typed into a URL never reaches `policy-used.log`.

## v0.0.22 — 2026-10-31

- Rerank monitoring on the **Rerank** page, from the telemetry folder's `rerank.call.completed` events (`GET /api/rerank/stats`, read-only, no new dependency): for today and the last 7 UTC days, calls by outcome, caller and provider, requests against the daily cap from the latest event, token and billed-unit totals, p50 and p95 duration of the calls that reached the provider, and the fallback rate. Gzipped day files are read too; an unreadable file is listed rather than failing the page.

## v0.0.21 — 2026-10-31

- Dashboard: a **Rerank** page. One table sets the per-dream `rerank` block for Defaults, every peer and every pair (rerank on/off, provider, model, candidates, daily request cap; ⋯ opens the other six keys), with inherited values faded and labelled with their source (pair, observer, observed, default, then Honcho's own `[rerank]`). Changes go through Stage, Review and Write like the dream limits and apply at the next dream. Two more boxes edit Honcho's `[rerank]`, the `[db]` HNSW fields and `[summary] output_headroom` from the regenerated schema through the config.toml write, marked as needing a Honcho restart. The config write now refuses a list item outside the schema's enum (`rerank.callers`), which Honcho would refuse at start.

## v0.0.20 — 2026-10-31

- `config/honcho-config.schema.json` and `config/honcho-prompts.json` regenerated from the fork checkout (Honcho 3.5.0): the schema gains `[rerank]` (every field but `API_KEY`, which the generator's secret rule leaves out), the `[db]` HNSW fields `hnsw_iterative_scan` and `hnsw_ef_search`, and `[summary] output_headroom`. `gen_honcho_schema.py` now emits `DBSettings` partially (`PARTIAL_CLASSES`: the two HNSW fields only; connection and pool fields stay in the environment). Upstream 3.4→3.5 differences come along: `deriver.workers` is capped at 100, `dialectic.max_query_length` and `summary.max_input_tokens` appear, the intake prompt asks for `source_indices`, and the dialectic prompt no longer asks the user which contradictory statement is right.

## v0.0.19 — 2026-10-31

- Deriver hook: `install()` imports the fork's `src.rerank` and replaces its `settings` with a proxy. While a dream runs, `RERANK` is Honcho's `RerankSettings` with the dream's `rerank` keys applied (pair, then peers, then defaults), validated with `model_validate`, so a value the class refuses keeps the base settings and is logged; outside a dream the base object is returned unchanged. The file is re-read on mtime as before. Each dream with rerank keys logs the block that supplied them (`rerank for <observer>/<observed> from <block>: …`), like the dream line's effort source. Without `src.rerank` (stock Honcho) one line is logged and nothing changes. Tests with fakes, and one integration test against the fork's real `src/rerank.py` that skips unless `HONCHO_FORK_DIR` points at the checkout.

## v0.0.18 — 2026-10-31

- `settings.json` takes an optional `rerank` block in `defaults`, each peer and each pair: the Honcho `[rerank]` fields a dream may change (`enabled`, `provider`, `model`, `base_url`, `candidates`, `max_documents`, `timeout_seconds`, `min_score`, `daily_request_cap`, `failures_before_cooldown`, `cooldown_seconds`), with the names, types and ranges of the fork's `RerankSettings`. Each key resolves like a dream limit (pair, then peers, then defaults); a key set nowhere keeps Honcho's `[rerank]` value. Never an `api_key`: the schema rejects the key and `validate.py` names it. `settings.schema.json`, `settings.example.json`, `validate.py` and the docs updated; `validate.py`'s source comparison now accepts the per-dream fields instead of listing each as a difference.

## v0.0.17 — 2026-10-31

- Prompt views: for intake, summaries and questions the page shows Honcho's stock prompt alone or with my text ("Honcho's prompt only" / "Honcho's prompt + my text"); for dreams and the peer card it shows each specialist's full system prompt (deduction, induction, card refresh) with the brief, budget and notes sections that `[dream]` turns on, the effective limits filled in, and the dream and peer-card texts that apply to the peer being edited. The stock text comes from `config/honcho-prompts.json`, built by `config/gen_honcho_prompts.py` from a Honcho source tree (`GET /api/prompts`).
- Dream limits per peer and pair, with global defaults on Models: tokens for new conclusions, tool-call cap, working notes and their size, the remaining-budget section and the final write rounds. The deriver hook substitutes them for Honcho's `[dream]` values for that dream; a limit set nowhere keeps Honcho's value.
- Model catalogue `models.json` beside settings.json (seeded from `config/models.json`) feeds the model dropdowns; one dream model can be added from the page.
- The Scheduler box (dream type, check interval, dreams at the same time) is back, on Overview with the other daily controls.
- Restart Honcho (Apply) works with compose container names: the allow-list entry for each service is matched as the bare name or `<project>-<service>-<n>`. It was refused before because it asked for `api` and `deriver` literally, so the api never restarted and dreams stayed refused after Enable.
- The dashboard has no login; the host firewall limits who reaches it.
- Logs list the newest entry last. A setting changed and changed back is no longer listed as a staged change.
- The two live deployment notes left the repository.
- Dashboard redesign in the suite style: colour and font tokens with light and dark themes (switch in the top bar, kept in the browser), a left page menu that folds into a button on a phone, collapsible boxed sections with counts, switches for every boolean, rich tooltips on labels and headers instead of separate help marks, compact aligned tables that become card rows below 760 px, no horizontal page scroll at 960 or 375 px, local times without fractions, dropdowns for every closed set (modes, kinds, models from a backend's list, effort levels, peers, pairs, skip pairs) and a model dropdown on every backend (its own models list, else the models other backends of that kind use, plus the current value and Other… for any id). Peer and pair rows are one line, with row actions in a ⋯ menu; pages follow the address-bar hash, including back and forward.
- Edits are working values. The floating Stage bar stages or discards them; Review (N) lists the staged changes by file, checks them with the server, allows unstaging one change, one file or all, and Write writes settings.json, the instruction files, the workspace configuration and config.toml in that order, each with its backup. Leaving with pending changes asks first.
- Inherited values show faded; Reset clears a peer or pair row, and in the Honcho config page Reset removes a key so Honcho's default applies.
- Control page: switches for dreams, intake and summaries in the workspace configuration (`dream.enabled`, `reasoning.enabled`, `summary.enabled`, no restart), sent with the whole module block because Honcho merges top-level keys; dialectic shown as not pausable; `[dream] ENABLED` from config.toml shown read-only; the model and transport each task resolves to, with Honcho's built-in default in red.
- Apply (restart of api and deriver) is refused while a config.toml model_config block has no model or no transport, so Honcho cannot fall back to its default model on the OpenAI key unnoticed. The review and the Honcho config page list those blocks.
- Providers: the effort fields say what they control (CLI effort, or the reasoning effort sent to an OpenAI-compatible model). Instructions: separate dream and peer-card boxes that list only the default file and the backends or peers with their own file; database text collapsed. Pairs and skip pairs use two peer dropdowns. Logs are full width, taller and scroll inside.
- Backups go to `backups/` beside settings.json (`settings/`, `instructions/<path>/`, `workspace/`) as `<file>.bak_<YYYYMMDD_HHMMSS>`; config.toml copies keep their folder and take the same name. Rotation keeps the newest 10 per file plus any younger than 7 days (was 20). Every write is read back. Older copies beside the files are moved into the folder on start, never deleted. History lists settings, instruction, workspace and config.toml copies.

## v0.0.16 — 2026-10-30

- One secrets file: every service and the bridge load `${HONCHO_ROOT}/.env` (the Honcho project's `.env`) through `env_file`; `deploy/.env` keeps paths, ports and names only, and `.env.example` lists the secret names expected in the Honcho file instead of carrying them.

## v0.0.15 — 2026-10-30

- Honcho config editor writes `config.toml` in place and keeps its backups in `HONCHO_CONFIG_BAK_DIR` (the Honcho project's `backups/config-history/`, mounted): the dashboard holds the file as a single-file bind mount, where a temp-file rename fails or leaves the mount on the old inode.

## v0.0.14 — 2026-10-30

- The dashboard edits Honcho's `config.toml`. The schema is generated from Honcho's settings classes and leaves out auth, the database, Sentry, and secret fields. A write keeps comments and unknown keys, and keeps at most 20 `config.toml.bak-<timestamp>` copies. Fields that compose sets in the environment are read-only. Apply restarts `api` and `deriver` when that restart is configured. Honcho reads the file only at start.
- The dashboard adds a backend and removes one that nothing references. Removing a backend that `providers.active`, `defaults.backend`, or a peer or pair still names is refused, and the error lists those references. The models list and the enabled flag save through the same review, diff and write path.
- Instruction files are a tree beside `settings.json`: `instructions/default/<kind>.md`, `instructions/backends/<backend>/<kind>.md` and `instructions/peers/<peer>/<kind>.md`. Kinds are dream, peer-card, reasoning, summary and dialectic. For dream and peer-card the deriver hook resolves the observer peer, then the observed peer, then the backend, then the default, then the database. The source tag is `settings.peer.<name>.<kind>`, `settings.backend.<name>.<kind>` or `settings.default.<kind>`. An old flat name still applies for this release and the hook logs one deprecation line. On start the dashboard moves a flat name into the tree when the new file is absent.
- Reasoning, summary and dialectic are written to the workspace configuration through the Honcho API, after a diff. The previous configuration is kept as `instructions/_workspace-config.bak-<timestamp>.json`, at most 20. Those three have no per-peer level. Each peer has an Instructions editor for `dream.md` and `peer-card.md`, with create-from-default and create-from-backend.

## v0.0.13 — 2026-10-30

- Two socket proxies: `docker-proxy` lists (read-only; CONTAINERS=1, POST=0) and `docker-proxy-action` restarts, starts or stops (POST=1, CONTAINERS=0, ALLOW_*=1). The image denies every non-GET unless POST=1, and POST=1 with CONTAINERS=1 would also allow create and exec. The dashboard sends GET to the first and POST to the second (`DOCKER_PROXY_ACTION_URL`). Buttons sit behind basic auth and `restart-allow.json` (container names, read on every request). An empty name list makes `GET /api/containers` show every name the proxy reports, and actions stay off. Each call is written to the restart log.

## v0.0.12 — 2026-10-30

- Dashboard: the Honcho API token is no longer run through the secret-pattern check (a JWT starts with `eyJ`, so every real token was reported as unset).

## v0.0.11 — 2026-10-30

- compose: the control and scheduler services no longer re-declare `HONCHO_API_TOKEN`, `HONCHO_WORKSPACE`, `HONCHO_API_URL`, `CONTROL_USER` and `CONTROL_PASSWORD` under `environment:` (an empty interpolation overrode the `env_file` value and left the dashboard without an API token).

## v0.0.10 — 2026-10-30

- Control service mounts `config/` as a folder at `/config`.

## v0.0.9 — 2026-10-30

- Bridge returns 400 when the chosen backend is disabled, or when `<backend>:<model>` names a model that is not in that backend's `models` list. A call keeps the backend it read at the start. The call log's `final_chars` counts the structured final on a CLI tool turn.
- Scheduler discovery re-reads `settings.json` and compares mtime before it writes, so a newer dashboard save is kept. It does not write when no peer is new. Database host, user and name, and the API URL, come from the environment. The config-changed line logs the skip list.

## v0.0.8 — 2026-10-30

- Consumers mount the control directory (`CONTROL_DIR`, default `${CODE_ROOT}/deploy/control`) at `/control`: the scheduler and the dashboard read-write, the deriver and the bridge read-only. `CONTROL_FILE` is only the in-container path `/control/settings.json`. Instruction files live in `deploy/control/instructions/`. A bind of `settings.json` alone keeps the old inode after a temp-file rename, so hot reload stays on the previous content.
- Obsolete `deploy/instructions/` pointer removed; config, bridge and root READMEs name `CONTROL_DIR`.

## v0.0.7 — 2026-10-30

- Dashboard service `control` edits `settings.json` and the per-backend instruction files. A save is checked against the schema, shown as a diff, then written with a temp file and rename. At most 20 `bak-<timestamp>` copies are kept. Basic auth. One page: mode, providers, peers, pairs, scheduler, instructions, status and history.
- `tests/test_scheduler.py`: the driver-import check runs in a fresh interpreter so the dashboard tests cannot affect it.

## v0.0.6 — 2026-10-30

- In `scheduler` mode the hook runs a dream when `scheduled_by` is `honcho-scheduler`, or when that attribute is absent and `trigger_reason` is `manual` (the value the schedule route writes). Honcho's own reasons, including `document_threshold`, are skipped. The warning about an unmarked payload is gone.
- Brief, effort and rounds resolve field by field: pair, then the observer peer, then the observed peer, then defaults. An empty pair no longer hides the peer blocks. The log source is the block that supplied effort.

## v0.0.5 — 2026-10-30

- Deriver hook re-reads `settings.json` when the mtime changes. Brief, effort and rounds still come from one block (pair, then peer, then defaults). Backend is resolved on its own. The hook gates on `mode` and on `enabled: false`, stamps `backend`, observer and observed onto the dream model `extra_body`, and can replace dream and peer-card instructions from files beside the settings file.
- Scheduler stays up (`restart: unless-stopped`). In `stock` it sleeps. In `scheduler` it queues eligible notebooks and sends `scheduled_by`. New peers from the database are appended as `enabled: false`, with an atomic write and at most 20 `settings.json.bak-<timestamp>` files.
- Bridge is one process (`bridge/bridge_cli.py`): `cli-codex`, `cli-claude`, `openai-compat` and `anthropic-api`. The call log stays one JSON object and gains a `backend` field. `cli-grok` is gone.
- `deploy/compose.yaml` runs the scheduler beside telemetry and backup. The bridge stays in the honcho-bridge project. Mount lines for the deriver and the bridge are in `deploy/honcho-mounts.yaml`.

## v0.0.4 — 2026-10-30

- Add the database backup job. The container uses `pgvector/pgvector:pg15`, the same image as the Honcho database service, and writes `honcho-YYYY-MM-DD.dump` plus `.sha256` at `BACKUP_AT` in the `TZ` zone. Retention keeps the newest 7 dumps and the newest dump of each of the last 4 ISO weeks.
- Add the telemetry receiver: `POST /events` appends one UTC-day JSONL file, refuses a missing token or a low disk, gzips and deletes old files, and forwards each accepted batch to `VIEWER_URL` without blocking the write.
- Review fixes: `/health` reports `low_space` with 200 so a full disk does not restart the receiver; one retention variable (`RETENTION_DAYS`); backup timezone is `TZ` from `.env` (default UTC) instead of a fixed zone; the backup catches up on start when the mark has passed and today's dump is missing; the captured-events test skips when `TELEMETRY_SAMPLE` is unset.

## v0.0.3 — 2026-10-30

- Operations note for the Honcho compose migration: API worker count, inline secrets moved to `.env` names, and guidance for moving config history.
- `tests/measure_pages.py` times 32 conclusion-list pages at 1, 4, 8 and 16 requests in flight against a local fake in pytest, and against the live API when its environment variables are set.
- `pytest.ini`: `python -m pytest` from the repository root runs every suite.

## v0.0.2 — 2026-10-30

- Canonical behaviour file: `config/settings.schema.json` and `config/validate.py`. Extended `config/settings.example.json` (providers, including OpenRouter, GLM and Anthropic; `mode` stays `stock`). Field mapping is in `docs/MIGRATION.md`.
- Schema accepts any backend name under `providers.backends` (the kind decides the fields); `validate.py` checks that every backend reference (`providers.active`, `defaults`, peers, pairs) names a defined backend and, for `<backend>:<model>`, a listed model. Bridge concurrency example set to 10.

## v0.0.1 — 2026-10-30

- GPL-3.0-or-later licence (`LICENSE`), `VERSION` file as the single version source.
- Naming aligned on `settings.json` everywhere (README, config README, `.gitignore`); `control.*` names removed.
- Repository scaffold: component folders, configuration templates (`deploy/.env.example`, `config/settings.example.json`),
  documentation skeleton. No runnable code yet.
