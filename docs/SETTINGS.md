# settings.json

The one behaviour file. It lives in `CONFIG_DIR` (`/config/settings.json` inside the containers that read it), is validated
against `src/config/settings.schema.json` (JSON Schema draft 2020-12, `additionalProperties` false everywhere), and
is re-read by the hook, the scheduler, the bridge, the dashboard and the rerank component when its modification time
changes. The dashboard writes it with a backup and a read-back; `python src/config/validate.py <file>` checks a file
by hand, including the cross-references the schema cannot express.

Two rules hold for every key:

- **No secret.** A backend names the environment variable that holds its key (`key_env`, a name ending in `_KEY`),
  never the key. Any string with a word that starts with `sk-`, `AIza`, `Bearer ` or `eyJ` is refused by the schema,
  and the hook and the scheduler refuse a file that carries one in the values they read.
- **A missing or broken file is safe.** A missing file means the defaults: stock Honcho, reranking off, the bridge
  refuses calls until it appears. A file that fails to parse or validate keeps the last good copy in every consumer
  that had one; the dashboard shows the reason (`docs/TROUBLESHOOTING.md`).

`src/config/settings.example.json` is the committed starting file: real catalogue models, the active backend on Honcho's stock model (OpenAI
`gpt-5.4-mini` through the `openai` backend, with Honcho's own `LLM_OPENAI_API_KEY`), `providers.stages` empty (Route
all fills each job's stage with the model `config.toml` gives it, so on a stock Honcho every routed job stays on
`gpt-5.4-mini`), no peers or pairs, and `defaults` on the values Honcho ships with: `brief` 50,
`deduction_rounds` 12, `induction_rounds` 10, no `effort` (the model's own default). Routing a job through the
bridge therefore changes no model and no dream limit. The required top-level keys are
`mode`, `scheduler`, `providers`, `defaults`, `peers` and `pairs`; `rerank`, `logs` and `_help` are optional.

## `mode`

`"stock"` or `"scheduler"`.

- `stock`: Honcho dreams on its own triggers. The scheduler stays up and sleeps. The hook applies the per-dream
  settings and instruction files to every dream but skips none for its trigger.
- `scheduler`: the scheduler queues dreams for the eligible pairs, and the hook runs only a dream that came through
  Honcho's schedule route (`trigger_reason` `manual`, or `scheduled_by` `honcho-scheduler` when that attribute is
  present); Honcho's own triggers are skipped and logged `gate=skipped-mode`.

## `scheduler`

Read by the scheduler at each poll.

| Key | Type | Meaning |
|---|---|---|
| `poll_seconds` | integer 1 to 3600 | Seconds between polls. |
| `max_parallel` | integer 1 to 50 | Dreams the scheduler may have queued at once. |
| `dream_type` | `omni` or `card_refresh` | `omni` runs deduction and induction; `card_refresh` updates the peer card only. |
| `skip` | array of `observer/observed` keys | Pairs the scheduler leaves out. |

The eligible pairs are the keys of `pairs` plus each listed peer with itself, where both peers are listed and not
`enabled: false`, minus `skip`. The dashboard keeps `skip` and the peers' `enabled` in step: a pair switched On turns its
peers On, a peer whose pairs are all Off goes Off, a peer switched Off puts all its pairs in `skip`.

## `providers`

The bridge's backends and routing. The hook reads `active` and `stages` too (it stamps the stage alias on dream
calls); the dashboard shows them on the Models page.

| Key | Type | Meaning |
|---|---|---|
| `active` | backend reference | The backend used when the pair, the peers and `defaults.backend` are null (the Models page's "Dreams fallback" row). Must name an enabled backend. |
| `concurrency` | integer 1 to 100 | In-flight bridge requests. Replaces the bridge's `BRIDGE_CONCURRENCY` fallback. |
| `timeout_seconds` | integer 1 to 86400 | Bridge request timeout, applied as at most 570 seconds (below the 600-second timeout of Honcho's OpenAI client, which would otherwise resend a slow call). |
| `backends` | object | Named backends, below. |
| `stages` | object, optional | One entry per Honcho job routed through the bridge, below. |

A **backend reference** is a key of `backends`, or `<backend>:<model>` to pick one model id; the model must be in that
backend's `models` list when the backend has one (CLI backends accept any id).

### `providers.backends.<name>`

Names match `^[a-z][a-z0-9_-]*$`. The `kind` decides which fields apply.

| Key | Applies to | Meaning |
|---|---|---|
| `kind` | all | `cli-codex` (the Codex CLI), `cli-claude` (the Claude Code CLI), `openai-compat` (an OpenAI-style chat completions API), `anthropic-api` (the Anthropic Messages API). |
| `model` | all | Model id sent to this backend. |
| `models` | all | Model ids offered as `<backend>:<model>`. When present, a reference must use one of them. |
| `enabled` | all | `false` makes `validate.py` refuse the backend as `providers.active` or in `providers.stages`. Picking it as the dreams fallback on the Models page turns it back on. |
| `effort` | CLI kinds | The backend's default effort level, used when neither the request nor the stage entry sets one. |
| `thinking_effort` | HTTP kinds | The same default for `openai-compat` and `anthropic-api`. |
| `base_url` | `openai-compat` | API origin, including the `/v1` prefix when the provider uses one. `http` or `https`. |
| `key_env` | HTTP kinds | Name of the environment variable holding the key; must end in `_KEY`. The bridge reads it from its own environment (Honcho's `.env`). |
| `effort_style` | `openai-compat` | How effort reaches the provider: `openai`, `deepseek`, `openrouter`, `glm`, `anthropic` or `none`. Unset, the `base_url` host decides; a proxy in front of a known provider needs it set. |

Effort levels everywhere are `none` (thinking off), `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; each provider receives the nearest
level it documents, and `none` the way it documents switching thinking off (`src/bridge/README.md` has the table). Precedence for one call: the request's effort (the hook's
per-dream effort), then the stage entry's `effort`, then the backend's `effort` or `thinking_effort`, then none.

### `providers.stages`

Keys: `deriver`, `summary`, `dialectic.minimal`, `dialectic.low`, `dialectic.medium`, `dialectic.high`,
`dialectic.max`, `dream.deduction`, `dream.induction`, `peer_card`. A value is either a backend reference string
(`"deepseek:deepseek-chat"`) or an object `{"model": "<backend>:<model>", "effort": "<level>"}` with a per-job
effort. The bridge resolves the `@<stage>` alias that Honcho's routed `config.toml` block sends to the entry, so a
change here is applied at the next call. Route all on the Honcho config page writes these entries; the Models page
edits them. `peer_card` shares dream deduction's `config.toml` block and is stamped by the hook for a card refresh when
the entry exists.

## `defaults`, `peers`, `pairs`

The per-dream settings. The hook resolves each key for one dream field by field: `pairs["observer/observed"]`, then
`peers[observer]`, then `peers[observed]`, then `defaults`; a null or absent field falls through. An empty pair `{}`
inherits everything. A field set nowhere keeps Honcho's own value from `config.toml` (the dashboard shows "—"); an
effort set nowhere falls to the job's effort, then its backend's, and the dashboard shows "Model default" plus that
level; `defaults` may set any field or none; a peer must set `enabled`; a pair may set any field or none.

| Key | Type | Meaning |
|---|---|---|
| `enabled` | boolean, peers only | `false` skips every dream whose observer or observed is this peer, in either mode, and the scheduler does not queue it. An unlisted peer is not disabled. |
| `brief` | integer 1 to 1000 | Maximum new conclusions in a dream brief (`BRIEF_MAX_NEW_CONCLUSIONS`). |
| `deduction_effort`, `induction_effort` | effort level | The effort of each dream pass, sent to the bridge as the request's effort. A peer card refresh does not use them. |
| `deduction_rounds`, `induction_rounds` | integer 1 to 100 | Tool-iteration cap of each dream pass. |
| `backend` | backend reference or null | A dream model override. When set anywhere in the chain it wins over the stage entries; null everywhere means the stages, else `providers.active`. The instruction files use the backend key without a `:model` suffix. |
| `brief_tokens` | integer 500 to 100000 | Honcho's `dream.brief_max_new_conclusion_tokens` for that dream. |
| `tool_calls` | integer 1 to 50 | Honcho's `dream.max_tool_iterations`. |
| `notes` | `off`, `shadow`, `on` | Honcho's `dream.notes_mode`. |
| `notes_tokens` | integer 100 to 20000 | Honcho's `dream.notes_max_tokens`. |
| `budget` | boolean | Honcho's `dream.budget_enabled`. |
| `write_window` | integer 1 to 50 | Honcho's `dream.write_window_iterations`. |
| `rerank` | object | Per-dream rerank keys, below. |

The six Honcho dream limits apply for the whole of Honcho's `process_dream` for that dream; a limit set nowhere keeps
the value in Honcho's `config.toml` (the dream knobs on the Honcho config page).

Peer names and pair keys come from the database through the scheduler's discovery or the dashboard's Peers page; a
name with a slash or whitespace is refused.

## `rerank` (top level)

The global settings of the rerank component (`src/rerank/`), read by Honcho's api and deriver and re-read on change.
Off unless `enabled` is `true`. Names, types and ranges follow the component's `RerankSettings`; `null` means the
component's default.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Rerank the searches listed in `callers`. |
| `provider` | `voyage` | `voyage`, `cohere`, `jina` or `tei`: the request and response format. |
| `model` | `rerank-3-lite` | Model name sent as-is (`tei` does not send it). |
| `base_url` | unset | API root without `/rerank`; `http` or `https`, a host, no credentials, query or fragment. Required for `tei`. |
| `candidates` | 100 | 1 to 1000. Pool fetched by vector search and sent to the reranker. |
| `max_documents` | unset | 1 to 10000. Most documents one request may carry. |
| `max_query_chars` | 2000 | Longer queries are cut before the request. |
| `timeout_seconds` | 5.0 | Deadline for the counter and the HTTP call together. |
| `callers` | `["dream", "dialectic", "prefetch"]` | Searches to rerank; `conclusions_api` may be added. Duplicate detection is never reranked. |
| `min_score` | unset | Ranked results below this score are dropped. |
| `daily_request_cap` | unset | Most provider requests per UTC day; `0` never calls the provider. |
| `failures_before_cooldown` | 3 | Consecutive failures before the provider pauses. |
| `cooldown_seconds` | 60.0 | Length of the pause. |

The key is `RERANK_API_KEY` in Honcho's `.env`; an `api_key` key here is refused by the schema. The daily cap is shared
between the api and the deriver only when Honcho's Redis cache is enabled; without it each process counts on its own.

## `rerank` (in `defaults`, a peer or a pair)

The keys one dream may replace: `enabled`, `provider`, `model`, `base_url`, `candidates`, `max_documents`,
`timeout_seconds`, `min_score`, `daily_request_cap`. Each resolves pair, then peers, then defaults; a key set nowhere
keeps the global value. A per-dream `enabled` can turn reranking off for that dream, never on while the global switch
is off; a per-dream cap can lower the global cap, not raise it (the hook clamps and logs, the dashboard refuses). A
block naming another provider or `base_url` than the global one runs without the key. `callers`, `max_query_chars`
and the cooldown pair are global only.

## `logs`

Optional rotation for `policy-used.log` and `runs.log` (hook), `dream-nas.log` (scheduler), `restart.log`
(dashboard) and `bridge-calls.jsonl` (bridge).

| Key | Default | Meaning |
|---|---|---|
| `max_mb` | 10 | Rotate a log once it grows past this many megabytes. |
| `keep` | 1 | Rotated files kept, `.1` up to `.keep`; 1 to 10. |

## `_help`

A free-text note the example carries. Ignored by every component.

## Example

A small file: scheduler mode, two backends, intake and dreams routed, one pair with its own limits, reranking on with
a cap.

```json
{
  "mode": "scheduler",
  "scheduler": {"poll_seconds": 15, "max_parallel": 2, "dream_type": "omni", "skip": []},
  "providers": {
    "active": "openrouter",
    "concurrency": 10,
    "timeout_seconds": 570,
    "backends": {
      "openrouter": {
        "kind": "openai-compat",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "model": "example-vendor/example-model",
        "models": ["example-vendor/example-model", "example-vendor/small-model"],
        "enabled": true
      },
      "claude": {"kind": "cli-claude", "model": "example-claude-model", "effort": "high", "enabled": true}
    },
    "stages": {
      "deriver": "openrouter:example-vendor/small-model",
      "dream.deduction": {"model": "claude:example-claude-model", "effort": "high"},
      "dream.induction": "claude:example-claude-model",
      "peer_card": "claude:example-claude-model"
    }
  },
  "rerank": {"enabled": true, "provider": "voyage", "model": "rerank-3-lite", "candidates": 100, "daily_request_cap": 2000},
  "defaults": {"brief": 350, "deduction_effort": "medium", "induction_effort": "medium", "deduction_rounds": 24, "induction_rounds": 20, "backend": null},
  "peers": {"alice": {"enabled": true}, "bob": {"enabled": true}},
  "pairs": {"alice/bob": {"brief": 50, "deduction_effort": "max", "induction_effort": "max", "rerank": {"candidates": 200}}},
  "logs": {"max_mb": 10, "keep": 1}
}
```

The model ids are placeholders: use the ids your providers document. `python src/config/validate.py` accepts the
file above once the models exist in each backend's list.
