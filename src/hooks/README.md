# hooks

Policy hook mounted into Honcho's deriver at `/app/policies`. Set `entrypoint: ["/app/.venv/bin/python", "-m", "src.honcho_rerank.deriver_main", "/app/policies/deriver_main.py"]` on the deriver service (the rerank component's entrypoint installs reranking, then runs this one in the same process; see `deploy/honcho-mounts.yaml`). Without the rerank package the deriver entrypoint is `python /app/policies/deriver_main.py`. The stock entrypoint runs no scheduler hook. `deriver_main.py` calls `policies_runtime.install()`, then runs `src.deriver`.

When `install()` raises, the deriver still starts. `deriver_main.py` logs the exception class, reads `settings.json` (`CONTROL_FILE`) with `json`, and runs `src.deriver`. If that file is readable, `mode` is `stock` or absent, and no entry in `peers` or `pairs` has `enabled` set to false, Honcho's own dream timings stay in force. Otherwise (`mode` is `scheduler`, any peer or pair has `enabled` set to false, or the file is missing, unreadable, or not an object) it sets `DREAM_ENABLED` to `false` before Honcho's settings are imported, so intake runs and dreams do not. It writes the hook status file either way, with `checks_passed` false, `hook_failed` set to the exception class, and `dreams_blocked` true or false. The dashboard then shows `hook failed — dreams blocked` or `hook failed — running stock Honcho`.

## Files and mounts

| What | Path in the deriver | Host folder | Setting |
|---|---|---|---|
| Settings | `/config/settings.json` | `CONFIG_DIR` (read-only) | `CONTROL_FILE` |
| Instruction files | `/data/instructions` | `DATA_DIR/instructions` (read-only) | `INSTRUCTIONS_DIR` |
| Dream log | `/logs/policy-used.log` | `LOGS_DIR` | `POLICY_LOG` |
| Dream run records | `/logs/runs.log` (beside the dream log) | `LOGS_DIR` | follows `POLICY_LOG` |
| Status file | `/logs/hook-status.json` | `LOGS_DIR` | `POLICY_STATUS` |

The hook writes only into `LOGS_DIR`. The paths in the table are the defaults; `deploy/honcho-mounts.yaml` still sets
`POLICY_LOG: /logs/policy-used.log` and `POLICY_STATUS: /logs/hook-status.json` on the deriver, next to the `LOGS_DIR`
mount, so the Honcho compose file shows where the logs go. Mount the config folder, not the file alone: a file-only
mount would keep the old inode after a rename, and the hook would not see the new content.

The hook re-reads `settings.json` when the mtime changes. A file with an error in `mode`, `defaults`, `peers` or `pairs`, including a secret-like string in those values, is not applied. The last good copy stays in memory. An invalid `rerank` block is logged and the last good rerank settings stay; the rest of the file is applied. Reload and failure lines contain `policies reloaded` and no `{`, so a reader that parses the dream lines skips them.

## Lookup

Brief, effort and rounds resolve field by field: `pairs["observer/observed"]`, then `peers[observer]`, then `peers[observed]`, then `defaults`. A null or absent field falls through. An empty pair does not hide the peer blocks. The log source is the block that supplied `effort`.

Backend is separate. A null or absent value falls through: pair, `peers[observer]`, `peers[observed]`, `defaults.backend`, `providers.active`. The instruction file uses the backend key, without a `:model` suffix.

## Gates

`enabled: false` on the observer or the observed skips the dream in either mode (`gate=skipped-disabled`). An unlisted peer is not disabled.

A `settings.json` that exists but has not been accepted since the deriver started (a parse error, a value out of range) skips every dream (`gate=skipped-settings`) until a good file is saved; going on as `stock` would let Honcho's own dreams run in a deployment set to `scheduler`. A missing `settings.json` is stock Honcho. A file rejected after a good one keeps the good one.

A pair's dream limits apply for the whole of Honcho's `process_dream`, including its bookkeeping after `run_dream`, so a per-dream `notes` or `brief` value decides whether that dream keeps and moves the cursor.

`mode: stock` does not skip for a missing mark. `mode: scheduler` runs a dream when `scheduled_by` is `honcho-scheduler`. When that attribute is absent, it runs when `trigger_reason` is `manual`, the value the schedule route writes, and skips Honcho's own reasons (`gate=skipped-mode`). `DreamPayload` has `trigger_reason` and no `scheduled_by`. The hook reads `scheduled_by` first when the attribute is present.

A skipped dream returns without calling the original function. The hook's wrapper drops `scheduled_by` before the original call, because the current signature does not accept it.

## What a dream receives

While the dream runs, the hook overrides `BRIEF_MAX_NEW_CONCLUSIONS` and the two tool-iteration caps from the block. It sets `thinking_effort` and `extra_body.output_config.effort` from `effort` (`none` only on a call that goes to the bridge: Honcho's anthropic transport drops it and its gemini transport would send a `thinking_level` Google does not list, so a direct call keeps the job's own setting), and sets `extra_body.backend`, `extra_body.observer` and `extra_body.observed`.

`policy-used.log` keeps its JSON keys (`max_conclusions`, `reasoning_effort`, `deduction_rounds`, `induction_rounds`) stable, so a log reader can parse the object with `ast.literal_eval`. The line then ends with `mode=<m> backend=<b> gate=<ran|skipped-mode|skipped-disabled|skipped-settings>`. Do not put `true`, `false` or `null` in that JSON.

## Rerank per dream

The rerank component (`src/rerank/honcho_rerank`, mounted so it imports as `src.honcho_rerank`) is optional. When it is missing, `install()` logs `rerank package not installed; dream hook runs without it`, skips the rerank settings proxy and rerank-block validation, and installs the rest of the dream hook. When it is installed, the behaviour below applies. It reads its `settings.RERANK` on every call and never caches it. `install()` imports `src.honcho_rerank` and replaces its `settings` attribute with a proxy. While a dream decision is set, `RERANK` is the component's `RerankSettings` (built from the top-level `rerank` block of `settings.json`) with the dream's `rerank` keys from `settings.json` applied (pair, then `peers[observer]`, then `peers[observed]`, then `defaults`; a key set nowhere keeps the global value). The merged values go through `RerankSettings.model_validate`, so a value the class refuses (a `base_url` httpx cannot parse, for instance) leaves the base object in force and writes `rerank not applied <observer>/<observed> from <block>: ValidationError` once for that dream. Outside a dream, and for a dream without rerank keys, the proxy returns the base object unchanged; every other attribute (`CACHE`, …) passes through. The settings file is re-read on mtime like everything else.

Each dream that runs with rerank keys logs one line, `rerank for <observer>/<observed> from <block>: enabled=True candidates=7 …`, naming the block that supplied the first key the way the dream line's source names the block that supplied effort. A `base_url` is printed as `base_url=set`, never as its value, because a URL can carry a credential. `base_url` must follow Honcho's own rule (http or https, a host, no userinfo, no query, no fragment, no control characters); the hook keeps the last good rerank settings otherwise, and the schema's `rerankBaseUrl` pattern says the same. The line has no `{` and no `<-`, so a reader of the dream lines skips it. Before patching, `install()` checks the fork settings fields and callable signatures. Missing dream-fork support raises `RuntimeError`. A missing `src.honcho_rerank` does not: the hook logs `rerank package not installed; dream hook runs without it` and continues. When only the old fork module `src.rerank` exists the message names it as no longer supported and `install()` raises. A `rerank` block that fails the `RerankSettings` ranges, or holds any key whose name contains `key`, is logged as `rerank settings not applied` and the last good rerank settings stay; the rest of the file is applied.

## Instruction files

The folder is `INSTRUCTIONS_DIR`, by default `/data/instructions`: the `instructions/` folder of the stack's `DATA_DIR`, which the dashboard edits, mounted read-only into the deriver. For each of `dream` and `peer-card`, the hook reads `peers/<observer>/<kind>.md`, else `peers/<observed>/<kind>.md`, else `backends/<backend>/<kind>.md`, else `default/<kind>.md`, else leaves the database value. It sets `custom_instructions_source` to `settings.peer.<name>.<kind>`, `settings.backend.<name>.<kind>` or `settings.default.<kind>`. The old flat names (`<backend>-<kind>-instructions.md`, `default-<kind>-instructions.md`) are still read at their level for one release, with a deprecation line in the log.

The file is stat'd on every dream. The text is reused when `mtime_ns` matches. An empty file sets `custom_instructions` to `""`. A missing folder is not an error and is not logged. A file over `MAX_CUSTOM_INSTRUCTIONS_TOKENS` (default 2000) is still applied, and `policy-used.log` gets a warning with the token count and the cap, not the file text.

Only `orchestrator.get_configuration` is wrapped, so other callers of the helper keep the database value. `orchestrator.process_dream` and `src.dreamer.process_dream` are both wrapped, and so is the binding in `src.deriver.consumer`: the chained entrypoint imports the consumer before `install()` runs, so its copy of `process_dream` is replaced in place.

## Hook status

`POLICY_STATUS` defaults to `/logs/hook-status.json` in both the deriver and the dashboard: both mount `LOGS_DIR` at `/logs`, the deriver writes the file and the dashboard reads it. A custom status path must also be shared and readable by the dashboard. The hook atomically replaces one status file after installation and settings reloads; failed reloads preserve the applied mtime and record only the exception class. Status write failures log at ERROR and leave the deriver running. `HONCHO_FORK_SHA` optionally records the deployed fork SHA. The Overview polls status and marks missing, failed or stale settings red after two scheduler poll intervals.
