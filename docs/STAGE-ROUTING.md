# Stage routing through the bridge

`providers.stages` in settings.json maps `deriver`, `summary`, each `dialectic.<level>`, `dream.deduction`, `dream.induction`, and `peer_card` to a backend or backend:model. Known aliases are configured explicitly; an unknown `@stage` is refused. The bridge reloads the file for the next call. CLI backends accept model overrides without requiring a models list.

For a stage still using a Honcho native provider, use Route all jobs through the bridge on Honcho config, review the config.toml change, write, and restart Honcho once. After that, a model choice on Models writes only settings.json. Route all jobs through the bridge copies the bridge URL and key variable from a job already routed through it; if none is, it uses `http://bridge:8793/v1` and `BRIDGE_TOKEN`. Preserve that deployment-specific URL and key environment name; no credentials belong in settings.json.

The initial TOML shape for intake is:

```toml
[deriver.model_config]
model = "stage-route"
transport = "openai"
[deriver.model_config.overrides]
base_url = "<existing bridge URL>"
api_key_env = "<existing bridge key environment name>"
[deriver.model_config.overrides.provider_params.extra_body]
backend = "@deriver"
```

`api_key_env` names the variable whose value Honcho sends to the bridge as `Authorization: Bearer`. When the bridge has `BRIDGE_TOKEN` set (see `src/bridge/README.md`), every bridge model block must name a variable holding that value; the simplest is `api_key_env = "BRIDGE_TOKEN"` itself, since Honcho and the bridge load the same `.env`. Without `BRIDGE_TOKEN` the bridge ignores the key.

Use the same fields under summary.model_config with @summary; dialectic.levels.<level>.model_config with @dialectic.<level>; dream.deduction_model_config with @dream.deduction; dream.induction_model_config with @dream.induction. Merge these fields into existing tables, preserving other provider_params. The dashboard performs that merge. A restart re-reads config.toml; a changed .env needs recreate.

After alias setup/restart, a model or per-job effort change edits only providers.stages. Model changes are hot. Transport, embeddings and other config.toml fields still need restart. Environment overrides lock matching fields (including nested children of a JSON settings variable) without displaying the variable values.

A stage routed through the bridge sends Honcho's OpenAI-style request; the bridge rewrites the provider-specific parts for the chosen backend. The effort the stage asks for reaches the provider as the nearest level that provider documents, in the provider's own field (backend `effort_style`, else the `base_url` host; table in `src/bridge/README.md`). A backend's `effort` or `thinking_effort` is only its default level and never changes that field. On OpenAI, `max_tokens` becomes `max_completion_tokens`. On DeepSeek and GLM, an intake (`deriver`) or other stage that asks for `response_format` `json_schema` is sent as `json_object` with the schema in the system message, since those providers document `json_object` only. A proxy in front of one of these providers needs `effort_style` on its backend.

The peer-card refresh specialist shares the deduction model's transport in the current fork. The hook stamps @peer_card for a card refresh when that stage is configured, so its bridge provider can differ. Configure deduction's bridge transport once; there is no separate peer_card.model_config in this fork.

An explicit pair, peer or defaults.backend still wins for a dream. With no explicit dream override, configured stages use their own aliases. Existing files with no stages map keep providers.active as their fallback. Configure stages before expecting independent providers; older live settings need no immediate migration.

The bridge retains its last good settings after a parse/read error and reports settings_ok=false plus a generic settings_error from /health. It resumes healthy status when a valid changed file loads. This is visible degradation, not silent acceptance of the rejected file.


## Tool-free CLI calls and scheduling

After deploying this bridge version, CLI backends support tool-free intake, summaries, and end-of-loop synthesis. Plain text uses a native final-string envelope; a requested JSON Schema is passed unchanged to the CLI's native output-schema argument and its JSON is returned as completion content. JSON-object mode requests a native object schema. The bridge parses JSON and retries malformed output once within one provider timeout; Honcho retains full target-schema validation. Unsupported native CLI schemas fail with 502 instead of silently downgrading to prompting. CLI subscriptions have no enforced token cap, sampling or prompt caching; max_tokens/max_completion_tokens becomes a best-effort prompt instruction. The subprocess plus its possible repair share providers.timeout_seconds; the existing concurrency gate still controls queueing.

The stored mode value stock is shown as scheduling off (overlays still apply). Honcho can start dreams, and hook settings/instructions still apply. This scheduler does not discover peers or queue dreams in that mode. Scheduler mode enables discovery and queueing.

The current scheduler-mode gate also admits schedule_dream from MCP/SDK: Honcho drops scheduled_by and marks these requests trigger_reason=manual. This is not exclusive scheduling. A future exclusive gate must compare trigger_reason and a marker preserved in dream metadata; that metadata change is intentionally not implemented here.
