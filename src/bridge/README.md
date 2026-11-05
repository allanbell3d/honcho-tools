# bridge

One process, `bridge_cli.py`: an OpenAI-compatible endpoint that sends each of Honcho's model calls to the backend `settings.json` names. The HTTP server is the standard library; the image installs no Python package.

## Service

The `bridge` service in `deploy/compose.yaml` (container `honcho-tools-bridge-1`) builds `src/bridge/Dockerfile` (`node:22-slim` plus `python3`, `ca-certificates`, `git`; the `codex` and `claude` CLIs from npm at the latest version only when `INSTALL_CODEX` or `INSTALL_CLAUDE` is `1`; command `python3 /app/bridge_cli.py`) and bind-mounts `bridge_cli.py`, so an edit needs no rebuild. It joins the Honcho network, where Honcho reaches it as `http://bridge:8793/v1`. It is not published on the host. It does not depend on any other service.

Tested with Node 22.23.4, Codex 0.166.0 and Claude Code 2.1.311.

| Host | Container | Use |
|---|---|---|
| `${CONFIG_DIR}` | `/config` (read-only) | `settings.json` |
| `${SECRETS_DIR}/codex` | `/home/node/.codex` | Codex CLI login (`auth.json`) |
| `${SECRETS_DIR}/claude` | `/home/node/.claude` | Claude CLI login, written by `claude login` run once inside the container |
| `${LOGS_DIR}` | `/logs` | `bridge-calls.jsonl` |
| `src/bridge/bridge_cli.py` | `/app/bridge_cli.py` (read-only) | the process |

| Variable | Default | Meaning |
|---|---|---|
| `CONTROL_FILE` | `/config/settings.json` | The settings file (compose sets it). |
| `BRIDGE_LOG` | `/logs/bridge-calls.jsonl` | The call log. |
| `BRIDGE_HOST` | `0.0.0.0` in the image | Bind address. |
| `BRIDGE_PORT` | `8793` in compose | Port. |
| `BRIDGE_EXE`, `BRIDGE_CLAUDE` | `codex`, `claude` | The CLI executables. |
| `BRIDGE_CONCURRENCY`, `BRIDGE_TIMEOUT` | `6`, `570` | Fallbacks for `providers.concurrency` and `providers.timeout_seconds`. The timeout is capped at 570 seconds. |
| `BRIDGE_TOKEN` | unset | Shared token. Unset or blank: no check. A secret: it goes in Honcho's `.env`, never in `deploy/.env`. |

An accepted connection has a 60-second read/write timeout (`SOCKET_TIMEOUT_SECONDS`, a constant): a client that stalls after its headers gets the 400 for its unreadable body and the connection is closed. The 570-second call budget is separate.

Secrets come from `${HONCHO_ROOT}/.env` through `env_file`, read by compose on the host. The login folders are never in the repository.

The bridge passes CLI flags (`--effort`, `-c model_reasoning_effort`, `--output-schema`, `--tools`) whose names a CLI release can change. Each CLI installs at the latest version when its build argument is `1` (`INSTALL_CODEX`, `INSTALL_CLAUDE`, default `0`); rebuild and check a call of that kind before relying on that build.

## Authentication

With `BRIDGE_TOKEN` unset the endpoint is open to every container on the Honcho network. With it set, every
route needs `Authorization: Bearer <token>`; anything else gets 401 with `WWW-Authenticate: Bearer` and the connection
is closed without reading the request body. The comparison is constant-time (`hmac.compare_digest` over the bytes).
`GET /health` stays open for the container healthcheck, but without the token it answers only `ok`, `settings_ok` and
`log_ok`; the backends, models, routes and counters need the token. The full answer adds `auth_required` and
`unauthorized` (refused requests since start), so a caller sending the wrong token shows up there.

Honcho sends the token itself. Its OpenAI transport sends the value of the variable named by `api_key_env` as the
Bearer key, so every model block that points at the bridge names the same variable:

```toml
[dream.deduction_model_config.overrides]
base_url = "http://bridge:8793/v1"
api_key_env = "BRIDGE_TOKEN"
```

Honcho's api and deriver and the bridge all load Honcho's `.env`, so one line there, `BRIDGE_TOKEN=<random value>`
(for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`), serves both sides. A model block that
already names another variable for the bridge can keep it, as long as that variable and `BRIDGE_TOKEN` hold the same
value. Order of a switch-over: put the variable in `.env`, point every bridge model block's `api_key_env` at it,
recreate the bridge and restart Honcho's api and deriver (a changed `.env` needs a recreate, not a restart). Until both
sides have it, calls fail with 401, which Honcho's client does not resend. The dashboard's bridge status reads `/health`
without the token, so with a token set it shows up/down only.

The CLIs never see the token: names ending in `TOKEN` are dropped from their environment.

## Routing

The settings file is `CONTROL_FILE`, or `/config/settings.json`. It is re-read when the mtime changes. Mount the config folder (`CONFIG_DIR`) at `/config`, never the file alone: the dashboard replaces the file on save and a file bind would keep the old inode.

Without `settings.json` the bridge still starts: it logs `settings.json missing at <path>; running with defaults` once, `/health` reports `settings_ok: false`, `/v1/models` is empty and a completion is refused with 400 (`unknown backend`) until the file appears. A file that later fails to parse keeps the last good copy in use and is reported on `/health`.

Backend for a request: top-level `backend`, else `extra_body.backend` (Honcho's client merges `extra_body` onto the body), else `defaults.backend`, else `providers.active`. A value may be `<backend>` or `<backend>:<model>` (split on the first colon). `GET /v1/models` lists each backend and, where a backend has `models`, `<backend>:<model>`.

Request effort wins, then a stage entry's `effort`, then the backend's `effort` or `thinking_effort`; those two are only the backend's default level. Levels are `none` (thinking off), `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. Each provider receives the nearest level it documents (table below), never one it does not document. `none` is each provider's own off switch, listed under the table. The call log records the asked level as `effort` and the level sent as `sent_effort`.

| Kind | Call |
|---|---|
| `cli-codex` | `codex exec`, including `-c model_reasoning_effort="<level>"` |
| `cli-claude` | `claude -p`, including `--effort` |
| `openai-compat` | `POST {base_url}/chat/completions`, `Authorization: Bearer` the value of `key_env` |
| `anthropic-api` | Messages API, `output_config.effort`, `x-api-key` from `key_env` |

The provider decides the effort field and levels. For `openai-compat` it is the backend's `effort_style` (`openai`, `deepseek`, `openrouter`, `glm`, `anthropic` or `none`), else the `base_url` host (`openrouter.ai`, `deepseek.com`, `z.ai` or `bigmodel.cn`, `api.openai.com`, each with its subdomains), else `generic`. `anthropic-api` takes `anthropic` (or `none`); the CLI kinds use their own CLI's levels. A `models` list or a `thinking_effort` key does not change the field. A proxy or self-hosted endpoint in front of one of these providers needs `effort_style`, since its host says nothing.

| Style | Field | minimal | low | medium | high | xhigh | max | Source |
|---|---|---|---|---|---|---|---|---|
| `anthropic` | `output_config.effort` (`reasoning_effort` on `openai-compat`) | low | low | medium | high | xhigh | max | [Effort](https://platform.claude.com/docs/en/build-with-claude/effort); minimal to low as Honcho does |
| `cli-claude` | `--effort` | low | low | medium | high | xhigh | max | [CLI reference](https://code.claude.com/docs/en/cli-reference) |
| `cli-codex` | `-c model_reasoning_effort=` | low | low | medium | high | xhigh | max | [Codex config reference](https://developers.openai.com/codex/config-reference) |
| `openai` | `reasoning_effort` | low | low | medium | high | xhigh | max | [Reasoning](https://developers.openai.com/api/docs/guides/reasoning), GPT-6 model pages |
| `deepseek` | `thinking: {"type": "enabled"}` plus `reasoning_effort` | low | low | high | high | high | max | [Create chat completion](https://api-docs.deepseek.com/api/create-chat-completion) |
| `glm` | `reasoning_effort`, GLM-5.2 and later only | low | low | high | high | high | max | [Chat completion](https://docs.z.ai/api-reference/llm/chat-completion) |
| `openrouter` | `reasoning.effort` | minimal | low | medium | high | xhigh | max | [Reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens) |
| `generic` | `reasoning_effort` | minimal | low | medium | high | xhigh | max | none; passed through |
| `none` | nothing | | | | | | | |

`none` sends, per style: `openai` and `generic` `reasoning_effort: "none"`; `deepseek` `reasoning_effort: "none"` and no `thinking` object (DeepSeek: "none disables thinking mode", and with no `thinking` object the mode follows `reasoning_effort`); `openrouter` `reasoning: {"effort": "none"}`; `glm` `thinking: {"type": "disabled"}` and no `reasoning_effort`, for GLM-4.6 and later except the GLM-5.3 series, whose thinking can only be enabled, so that series gets nothing; `anthropic-api` `thinking: {"type": "disabled"}` and no `output_config.effort` (a `thinking` the job sets itself wins); `cli-claude` and `cli-codex` `low`, because the command line has no off level. Sources: [DeepSeek](https://api-docs.deepseek.com/api/create-chat-completion), [OpenAI](https://developers.openai.com/api/docs/guides/reasoning), [Z.ai](https://docs.z.ai/api-reference/llm/chat-completion), [OpenRouter](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), [Anthropic](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting). Not every model takes it: OpenAI documents `none` per model (gpt-6-astra and gpt-6.1-sol refuse it), Anthropic refuses `disabled` on Fable, Mythos and Opus 5.5 and wants `between_tools` on Sonnet 5.5, and an OpenRouter model flagged mandatory refuses it. The provider answers 400 there.

DeepSeek itself maps medium and xhigh to high and minimal to low; the bridge sends the result. GLM-5.3 supports low, high and max only, and earlier models have no `reasoning_effort`, so a GLM model id without a version of 5.2 or later gets no effort field. Whether every OpenAI and Codex model accepts `xhigh` and `max` depends on the model: the gpt-6-luna and gpt-6.1-sol pages list both, other model pages may not.

On the `openai` style, `max_tokens` is sent as `max_completion_tokens`, which OpenAI's reasoning models require and Honcho itself uses for gpt-5, gpt-6 and the o-series. DeepSeek and GLM document `response_format` `text` and `json_object` only: a `json_schema` request to them is sent as `json_object`, and the schema is added to the first system message (or a new one) in the wording Honcho uses for its own `json_object` mode. An `anthropic-api` backend sends `json_schema` as `output_config.format` (`type` `json_schema` and the schema), in the same object as `effort` when the call has one. Anthropic documents no `json_object` type, so that request uses the same system-message wording. Honcho still validates the answer against its schema. `backend`, `observer`, `observed` and `output_config` are not forwarded on the OpenAI-compatible body. An unknown `effort_style` answers 400.

`providers.concurrency` and `providers.timeout_seconds` win. Otherwise `BRIDGE_CONCURRENCY` (default 6) and `BRIDGE_TIMEOUT` (default 570). Any timeout is capped at 570 seconds, below the 600-second timeout of Honcho's OpenAI client, which would otherwise send a slow call again. A timeout, an empty answer, invalid JSON after the one repair, a missing key variable or a missing CLI executable answers 422, which that client does not resend; other failures answer 502. The CLIs run without the keys, tokens and passwords of the bridge's environment.

A backend whose `key_env` does not end in `_KEY` is refused (and the settings schema refuses it before the dashboard saves it). `BRIDGE_EXE` and `BRIDGE_CLAUDE` default to `codex` and `claude` on `PATH`. Either may be a JSON list of arguments, so a wrapper can be `python` plus a script without a shell. A `.cmd` or `.bat` found on `PATH` is launched with `cmd /c`.

The call log is JSONL, `BRIDGE_LOG` (default `/logs/bridge-calls.jsonl`, in the stack's `LOGS_DIR`). Each line is one object and includes `backend`; it rotates from the `logs` block of `settings.json` (10 MB, one previous copy by default) and its timestamps are UTC with a `Z`. A trailing token would break `json.loads`. Messages and key values are not logged. An Anthropic answer reports its cache reads as `usage.prompt_tokens_details.cached_tokens`, so Honcho's usage parser does not bill them as fresh input.

CLI backends also answer tool-free calls (plain text or a requested JSON schema); see `docs/STAGE-ROUTING.md`. An empty answer is 422. Stage routing (`providers.stages`) is described there too.
