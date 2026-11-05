# rerank

Reranking for Honcho's conclusion searches, added at run time without editing Honcho's source. A larger vector
candidate pool goes to a reranker (Voyage, Cohere, Jina, or a self-hosted TEI, vLLM or Infinity server), and Honcho
keeps the best `top_k`. Any failure keeps the plain vector order.

It is one of the Honcho Tools components (`src/rerank/` in this repository). It is not a service. The package `honcho_rerank/` is mounted into Honcho's `api` and `deriver` containers and
installed by their entrypoints. It is off by default: with `"enabled": false` in the `rerank` block of `settings.json`,
or with no `settings.json` at all, Honcho's searches are its own, unchanged (`tests/test_passthrough.py` checks both).

| Path | What it is |
|---|---|
| `honcho_rerank/` | The package: settings, hook, client, events, the two entrypoints. |
| `tests/` | Offline tests against Honcho's real modules and fakes. |
| `patches/` | The one source patch this project carries against upstream Honcho (summary output headroom). |
| `requirements.txt` | The packages the package imports directly. Honcho's image already provides them. |

## Why

Honcho ranks conclusions by embedding distance only. A conclusion that means the same thing as the query but is worded
differently can rank low or be missed. A reranker reads the query and each candidate together and orders them by
relevance.

## How it is loaded

1. The package is mounted read-only at `/app/src/honcho_rerank` in both Honcho containers. Nothing else in the image
   changes.
2. The `api` container starts through `python -m src.honcho_rerank.api_main`. It installs the hook, then does what the
   image's `docker/entrypoint.sh` does: it runs `scripts/provision_db.py` and serves `src.main:app` on port 8000 with
   `API_WORKERS` workers.
3. The `deriver` container starts through `python -m src.honcho_rerank.deriver_main /app/policies/deriver_main.py`. It
   installs the hook, then runs the scheduler hook's entrypoint in the same process, which ends by running
   `src.deriver`. Without an argument it runs `src.deriver` directly.
4. Both containers mount the config folder (`CONFIG_DIR`) at `/config`, so the package reads
   `/config/settings.json`.

`deploy/honcho-mounts.yaml` has the exact lines. They are a one-time change to the Honcho compose file.

### Start-up checks

`install()` checks the Honcho names it relies on and raises `RuntimeError` before changing anything when one is off,
so a container never starts half-wrapped. On that failure the api and deriver entrypoints log it and continue into stock Honcho without the wrapper. `install()` checks that `query_documents` has exactly the parameters `db,
workspace_name, query, observer, observed, filters, max_distance, top_k, embedding`, that `src.crud.query_documents` is
that function, that the agent tools' search handler has a `ctx` and `ToolContext` has `parent_category` and `run_id`,
that `DialecticAgent.__init__` sets `_run_id`, that `run_dream` is async and its `DreamResult` has `run_id`, and that
`src.telemetry.events.emit` and `src.dependencies.tracked_db` import.

## How it works

- **One wrapper covers every search.** Every conclusion search in Honcho calls `crud.query_documents`. The hook
  replaces that package attribute with a wrapper. Duplicate detection inside `src/crud/document.py` calls the function
  by its module-local name, so it never reaches the wrapper and is never reranked. A test runs Honcho's real duplicate
  check after `install()` and fails if the wrapper is reached.
- **The caller is read from the calling code, one frame up.** The search tool in `src/utils/agent_tools.py` is `dream`
  or `dialectic` (from the tool context's `parent_category`); `search_memory` called from `src/dialectic/core.py` is
  `prefetch`; `src/routers/conclusions.py` is `conclusions_api`. Anything else, such as representation assembly, gets
  Honcho's own function, untouched.
- **Off means stock.** When reranking is off, or the caller is not listed in `callers`, the search is Honcho's own
  call with the caller's session and `top_k`: no wider pool, no request, no event.
- **No connection is held during the provider call.** A reranked search runs Honcho's own `query_documents` with its
  own short read-only session, fetching `max(top_k, min(candidates, limit))` rows, detaches them and closes the session
  before the request. A caller that holds an open transaction keeps its plain vector order (`skipped_transaction`).
- **Reranking never makes a search fail.** No key, an unknown provider, the daily cap, a cooldown, a timeout, an HTTP
  error, a malformed answer, or anything unexpected: the search returns the plain vector order cut to `top_k`, logs one
  line with the caller, provider and reason, and emits one event. One candidate with no `min_score` sends no request
  (`noop_single`).
- **Back-off per provider.** After `failures_before_cooldown` failures in a row, that provider and base URL pause for
  `cooldown_seconds`. After the pause exactly one probe runs; concurrent searches skip until it resolves.
- **Run ids.** Each `rerank.call.completed` event carries the dream or dialectic run that searched: the tool context's
  run id, or a context variable set by wrapping `run_dream` (deriver) and `DialecticAgent.__init__` (API).
- **Per-dream settings.** The scheduler hook (`src/hooks/policies_runtime.py`) replaces the package attribute
  `src.honcho_rerank.settings` with a proxy. While a dream runs, the `rerank` keys of that dream's pair, peers or
  defaults in `settings.json` replace the global ones; outside a dream the global block applies unchanged.

## Settings

The `rerank` block of `settings.json` in the config folder (`/config/settings.json` in the containers). It is the only
place rerank is configured: there is no `[rerank]` section in Honcho's `config.toml` and no separate settings file.
Keys are matched without regard to case. A `null` value means the default. The file is re-read when its modification
time or size changes, so a change needs no restart. A file that cannot be read, parsed or validated leaves the last
good settings in force and logs the reason (field names only, never values); fix the file and the next search picks it
up. A missing file at start means the defaults, so reranking is off.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turns reranking on for the callers in `callers`. |
| `provider` | `"voyage"` | `voyage`, `cohere`, `jina` or `tei`. Picks the request and response format. |
| `model` | `"rerank-3-lite"` | Model name, sent as-is. The default is a Voyage model, so set it for any other provider. `tei` does not send it. |
| `base_url` | unset | API root, without `/rerank`. Unset uses the provider's own URL. Required for `tei`. Must be `http` or `https` with no credentials, query string or fragment. |
| `candidates` | `100` | 1 to 1000. Pool fetched by vector search: `max(top_k, min(candidates, limit))`. |
| `max_documents` | unset | 1 to 10000. Most documents one request may carry. Unset: 1000 for `voyage`, `cohere` and `jina`, 32 for `tei`. The provider's own ceiling applies on top (1000 for `voyage`, 10000 for `cohere`). |
| `max_query_chars` | `2000` | At least 1. Longer queries are cut to this many characters before the request. |
| `timeout_seconds` | `5.0` | Above 0. Deadline for the request counter and the HTTP call together. |
| `callers` | `["dream", "dialectic", "prefetch"]` | Searches to rerank. Add `"conclusions_api"` for the conclusions query route. Duplicate detection is never reranked. |
| `min_score` | unset | Drops ranked results below this score, so a search can return fewer than `top_k`. Scores are not comparable between providers. |
| `daily_request_cap` | unset | Most provider requests per UTC day. Unset: no cap. `0`: never call the provider. |
| `failures_before_cooldown` | `3` | At least 1. Consecutive failures (timeout, HTTP error, unusable answer, unexpected error, counter failure) before that provider pauses. Only a success resets the count. |
| `cooldown_seconds` | `60.0` | 0 or more. Length of the pause. |

`RERANK_SETTINGS` points the package at another file; the deployment does not set it.

**The key** is `RERANK_API_KEY` in Honcho's `.env`, which both containers load. Surrounding whitespace is removed and
a blank value counts as unset; a value with non-ASCII or control characters is refused (and not echoed). A key field in
`settings.json` is ignored with a warning, and the schema refuses one. The key is required for the hosted `voyage`,
`cohere` and `jina` APIs (no `base_url`); with `base_url` set it is optional and sent only if present.

**The daily cap counts requests, not tokens.** The counter goes up before each request, so a refused, timed-out or
failed request still counts. With `CACHE_ENABLED` in Honcho, the counter is in Redis and the two containers share it;
if Redis is not reachable while a cap is set, reranking is skipped. Without the cache, each process counts on its own.
The daily request cap is shared across processes only when Honcho's Redis cache is enabled; without it each process
counts separately.

### The dashboard switch

The dashboard's **Rerank** page edits this block in its **Global (api and deriver, hot)** box, including the
`enabled` switch (the **Rerank** row). A change goes through **Stage**, **Review** and **Write** like every other `settings.json` edit.
Both containers pick it up at the next search, without a restart. The **Per dream** table on the same page edits the
`rerank` blocks of the defaults, peers and pairs, which replace `enabled`, `provider`, `model`, `base_url`,
`candidates`, `max_documents`, `timeout_seconds`, `min_score` and `daily_request_cap` for one dream.

### Providers

| `provider` | Service | Default `base_url` | Usage reported |
|---|---|---|---|
| `voyage` | Voyage AI `/v1/rerank` | `https://api.voyageai.com/v1` | `usage.total_tokens` → `tokens` |
| `cohere` | Cohere v2 `/v2/rerank` | `https://api.cohere.com/v2` | `meta.billed_units.search_units` → `billed_units` |
| `jina` | Jina AI `/v1/rerank` | `https://api.jina.ai/v1` | `usage.total_tokens` → `tokens` |
| `tei` | Text Embeddings Inference `/rerank` | none: set `base_url` | none |

Self-hosted servers use one of these formats: vLLM as `jina` with `base_url` ending in `/v1`; Infinity as `cohere`;
TEI as `tei` (it refuses more than `--max-client-batch-size` texts, default 32, hence the `max_documents` default).
Results are matched back by `index` and sorted by score here, so the provider's order does not matter. Each answer is
checked against the request before it is used: every `index` an integer in range and not repeated, every score a finite
number, and no more rows than were asked for (`top_n`/`top_k`; for TEI, one per text sent). The body is streamed and
refused past 4 MB (`MAX_RESPONSE_BYTES` in `client.py`), before it is held whole. Any violation is a
`malformed_response` and the search keeps the vector order. A compatible server that ignores `top_n` and returns every
row therefore falls back on each call; the `malformed_response` count on the Rerank page shows it. Prices and
model names change; check the provider before you set a cap. A hosted provider costs money per request: set
`daily_request_cap` if you want a hard ceiling.

### Scan mode

`PGOPTIONS` on both containers sets `hnsw.iterative_scan` and `hnsw.ef_search` on every database connection, for
example `-c hnsw.iterative_scan=strict_order -c hnsw.ef_search=100` (pgvector 0.8.0 or later). `strict_order` keeps
rows in exact distance order, so the plain-order fallback and duplicate detection see the nearest rows first. Set
`hnsw.ef_search` to at least `candidates`; at start the hook reads it once and warns when it is lower (once per pool
size, also after a reload). pgvector also limits how far an iterative scan goes with `hnsw.max_scan_tuples`; this does
not change it. If you connect through a pooler, check that it passes `options`.

## Outcomes

Every search reranking applies to (enabled, caller listed) ends in one outcome:

| Outcome | Meaning |
|---|---|
| `reranked` | The provider's order was used. |
| `skipped_unsupported_provider` | The provider name is unknown (possible only through a settings proxy). |
| `skipped_missing_url` | `tei` with no `base_url`. |
| `skipped_missing_key` | A hosted API with no key. |
| `skipped_cap` | The daily cap is reached or is `0`. |
| `skipped_cooldown` | The provider is paused after repeated failures. |
| `skipped_counter` | A cap is set and the request counter failed or did not answer in time. |
| `skipped_transaction` | The caller holds an open database transaction. |
| `empty_result` | No candidates. |
| `noop_single` | One candidate and no `min_score`: no request, no counter increment. |
| `too_many_candidates` | `top_k` is above the request limit. |
| `timeout` | No answer within `timeout_seconds`. |
| `http_error` | Connection error or error status. |
| `malformed_response` | The answer could not be used (bad JSON, index or score, repeated index, too few or too many rows, body over 4 MB). |
| `cancelled` | The search was cancelled while reranking. Not counted as a failure. |
| `internal_error` | Anything else. |

A search that would certainly skip (no key, no URL, unknown provider, cap reached, paused) does not widen the vector
search either. There are no retries.

## Events

Each outcome produces one `rerank.call.completed` CloudEvent through Honcho's `emit`, when Honcho's telemetry is on. It
is a high-volume event, so `HIGH_VOLUME_SAMPLE_RATE` can drop some. The dashboard's **Rerank** page summarises these
events from the telemetry folder.

| Field | Meaning |
|---|---|
| `caller` | `dream`, `dialectic`, `prefetch` or `conclusions_api`. |
| `workspace_name` | The workspace that searched. |
| `run_id` | The dream or dialectic run (the id in its `dream.run`, `dream.specialist` or `dialectic.completed` event). For a dream search without a tool-context run id, the token logged as `rerank run token=… dream run_id=…`. Null for `conclusions_api`. |
| `provider` | The configured provider, or `unsupported`. |
| `model` | The configured model. |
| `outcome` | See above. |
| `candidates_sent` | Documents in the request; 0 when none was sent. |
| `results_returned` | Rows the search returned. |
| `duration_ms` | Time in this step, counter and HTTP call included. |
| `tokens` | `usage.total_tokens` when the provider reports it, else null. |
| `billed_units` | Cohere search units, else null. |
| `daily_requests` | The counter after this request; null without a cap. |
| `daily_request_cap` | The configured cap; null for none. |

The events and log lines hold no query text, document text, URL or key. There are no Prometheus counters.

## What it changes in Honcho

- **Honcho's code, in memory only.** At start, the entrypoints replace `src.crud.query_documents`,
  `src.dreamer.orchestrator.run_dream`, `DialecticAgent.__init__`, the API's lifespan and the deriver's `main` in the
  running process. No file on disk is written or changed.
- **LLM call telemetry, in memory only.** `routing_telemetry` wraps `src.llm.executor.honcho_llm_call_inner`,
  `backend_for_provider` and `_emit_call_telemetry`, and `TelemetryEmitter.emit`, to add the model, backend and billing
  route that answered (`docs/EXECUTED-ROUTING.md`). An error in that enrichment is logged and the call goes on
  unchanged. When a Honcho upgrade moves one of those names, the containers start without it and log one warning.
- **The container commands**, as above.
- **Database connections**, through `PGOPTIONS`.

It stores nothing on disk. The daily request counter is in Honcho's Redis cache when `CACHE_ENABLED`, else in process
memory; the cooldown state is in process memory and resets on restart.

## The source patch

`patches/summary-output-headroom.patch` is the only change this project carries against upstream Honcho `v3.5.0`. It
adds `[summary] OUTPUT_HEADROOM`, a multiplier (default `1.0`, stock behaviour) on the `max_tokens` sent to the summary
model. It is applied to the Honcho source tree before the image is built, not installed by the hook: see
`patches/README.md`. After a Honcho upgrade, re-apply it and keep the mount and entrypoint lines.

## Tests

The tests need Honcho `v3.5.0` on `PYTHONPATH` and its dependencies installed, so they run with Honcho's Python, not the
system one. `pytest.ini` in this folder sets the async mode. From the repository root:

```bash
export HONCHO_PATH="/path/to/honcho"                  # Honcho v3.5.0 tree root, containing src/
uv sync --project "$HONCHO_PATH"                      # install Honcho's dependencies
export PYTHONPATH="$PWD/src/rerank:$HONCHO_PATH"      # this package and Honcho's source
"$HONCHO_PATH/.venv/bin/python" -m pip install pytest pytest-asyncio  # test tools
"$HONCHO_PATH/.venv/bin/python" -m pytest -c src/rerank/pytest.ini src/rerank/tests -q -p no:cacheprovider
```

On Windows separate the `PYTHONPATH` entries with `;` and use `.venv/Scripts/python.exe`. The tests use made-up data and
a fake HTTP transport; one test starts a throwaway HTTP server on `localhost` to check the key header on a real
connection. `tests/test_rerank_hook_integration.py` in the repository's main suite runs the scheduler hook's proxy against this
package.

## Known limitations

- **Live on one deployment only.** It has run against a real PostgreSQL index, Redis and Voyage since 2026-11-03 on one
  Honcho 3.5.0 deployment; the tests themselves are offline. The other providers are tested against fakes only.
- **Bound to Honcho `v3.5.0`.** Caller detection depends on Honcho's module and function names. The start-up checks
  cover the names listed above, but not every call path: if Honcho changes how a caller reaches `query_documents`,
  that caller stops being reranked without an error. Check the **Rerank** page after an upgrade.
- **One key.** `RERANK_API_KEY` belongs to the global block's provider and host. A per-dream block that names another
  provider or `base_url` gets no key, so it works only with a reranker that needs none.
- **The key travels as given.** With an `http://` `base_url` the key is sent unencrypted; use `https://` for any
  reranker that needs a key.
- **Wider pools read more.** Each reranked search loads `candidates` full rows (embeddings included) instead of
  `top_k`, roughly 20 times the database transfer of a stock dream search at the default 100.

## License

Part of Honcho Tools, released under the repository's [GNU Affero General Public License v3.0](../../LICENSE).

## Credits

Credit to [Grant Jordan (grantjayy/honcho)](https://github.com/grantjayy/honcho) for the shared HTTPS Voyage client
with fail-open behaviour, and to [myles (myleshuzx/honcho)](https://github.com/myleshuzx/honcho) for reranking inside
`query_documents` with bounded candidate retrieval.
