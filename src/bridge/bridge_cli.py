"""OpenAI-compatible bridge. One process, one route table, backends from settings.json.

The file is re-read when its mtime changes. Backend choice for a request is
extra_body.backend (or a top-level backend, which is where Honcho's client merges
extra_body), then defaults.backend, then providers.active. A value may be
<backend> or <backend>:<model>.

Effort on the request wins, then a stage entry's effort, then the backend default. Kinds:
  cli-codex      codex exec, model_reasoning_effort
  cli-claude     claude -p, --effort
  openai-compat  POST {base_url}/chat/completions, Bearer key_env
  anthropic-api  Messages API, output_config.effort

The provider decides the effort field and the levels: a backend's effort_style, else the
base_url host (openrouter.ai, deepseek.com, z.ai, bigmodel.cn, api.openai.com), else a
generic reasoning_effort. OpenRouter takes reasoning.effort, DeepSeek thinking enabled plus
reasoning_effort, the others reasoning_effort. Each provider gets the nearest level it
documents (LEVELS). none switches thinking off the way each provider documents (effort_wire,
to_anthropic). effort and thinking_effort on a backend are only its default level.

Concurrency and timeout come from providers.concurrency and providers.timeout_seconds,
falling back to BRIDGE_CONCURRENCY and BRIDGE_TIMEOUT.
"""
from __future__ import annotations

import datetime
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|AIza|Bearer |eyJ)")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
FORWARD_KEYS = (
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "response_format",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "stop",
    "user",
)

RULES = """You are being called through an API bridge. You cannot run any tool yourself and you have no tools of
your own. The tools listed below are executed by the CALLER: to use them, list the calls in the `tool_calls`
field of your JSON answer. The caller runs them and sends you their results in the next turn.
- To act, return {"tool_calls": [{"name": "<tool>", "arguments": {...}}, ...]} (several calls allowed).
- When you are finished and want no more tool calls, return {"tool_calls": [], "final": "<your final answer>"}.
- Arguments must follow each tool's JSON schema exactly."""


def settings_path() -> Path:
    return Path(os.environ.get("CONTROL_FILE") or "/config/settings.json")


def log_path() -> Path:
    """BRIDGE_LOG, else bridge-calls.jsonl in the stack's LOGS_DIR, mounted at /logs."""
    return Path(os.environ.get("BRIDGE_LOG") or "/logs/bridge-calls.jsonl")


def split_ref(ref: str | None) -> tuple[str | None, str | None]:
    if not isinstance(ref, str) or not ref:
        return None, None
    if ":" in ref:
        name, model = ref.split(":", 1)
        return name, model or None
    return ref, None


def exe_prefix(env_name: str, default: str) -> list[str]:
    """An executable name, or a JSON list used when the executable is a wrapper."""
    raw = os.environ.get(env_name, default).strip()
    if raw.startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed or not all(isinstance(part, str) for part in parsed):
            raise ValueError(env_name)
        return parsed
    return [raw]


def resolve_argv(prefix: list[str], args: list[str]) -> list[str]:
    if len(prefix) == 1:
        found = shutil.which(prefix[0]) or prefix[0]
        if found.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", found, *args]
        return [found, *args]
    return [*prefix, *args]


def require_cli(kind: str) -> None:
    """422 when the executable this kind runs is not on PATH."""
    if kind == "cli-codex":
        env_name, default, install = "BRIDGE_EXE", "codex", "CODEX"
    else:
        env_name, default, install = "BRIDGE_CLAUDE", "claude", "CLAUDE"
    prefix = exe_prefix(env_name, default)
    if shutil.which(prefix[0]):
        return
    raise FinalError(f"{kind} CLI not installed in this image (INSTALL_{install}=1 at build)")


def safe_text(text: str) -> str:
    flat = (text or "").replace("\n", " ").strip()
    if SECRET.search(flat):
        return "(hidden)"
    return flat[:500]


def text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def render(messages, tools, tool_choice, response_format):
    system = "\n\n".join(text_of(message.get("content")) for message in messages if message.get("role") in ("system", "developer"))
    parts = [RULES, "## Tools"]
    for tool in tools:
        function = tool.get("function", tool)
        parts.append(
            f"### {function['name']}\n{function.get('description', '')}\nParameters (JSON schema): "
            f"{json.dumps(function.get('parameters') or function.get('input_schema') or {}, ensure_ascii=False)}"
        )
    if isinstance(tool_choice, dict) and tool_choice.get("function"):
        parts.append(f"You MUST call the tool `{tool_choice['function']['name']}` now.")
    elif tool_choice == "required":
        parts.append("You MUST call at least one tool now.")
    elif tool_choice == "none":
        parts.append("Do NOT call tools now; give your final answer.")
    if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        parts.append(
            "When you give your final answer, `final` must be a JSON document (as a string) matching "
            f"this schema: {json.dumps(schema, ensure_ascii=False)}"
        )
    parts.append("## Conversation so far")
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            continue
        if role == "assistant" and message.get("tool_calls"):
            calls = [
                {"id": call["id"], "name": call["function"]["name"], "arguments": call["function"].get("arguments")}
                for call in message["tool_calls"]
            ]
            parts.append(f"<assistant>\n{text_of(message.get('content'))}\nTOOL CALLS: {json.dumps(calls, ensure_ascii=False)}\n</assistant>")
        elif role == "tool":
            parts.append(f"<tool_result id=\"{message.get('tool_call_id')}\">\n{text_of(message.get('content'))}\n</tool_result>")
        else:
            parts.append(f"<{role}>\n{text_of(message.get('content'))}\n</{role}>")
    parts.append("Now write your next turn as the JSON described above.")
    names = [tool.get("function", tool)["name"] for tool in tools]
    schema = {
        "type": "object",
        "properties": {
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "enum": names}, "arguments": {"type": "object"}},
                    "required": ["name", "arguments"],
                },
            },
            "final": {"type": "string"},
        },
        "required": ["tool_calls"],
    }
    return system or "You are a careful memory-curation agent.", "\n\n".join(parts), schema


def strict_schema(names: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"name": {"type": "string", "enum": names}, "arguments_json": {"type": "string"}},
                    "required": ["name", "arguments_json"],
                },
            },
            "final": {"type": "string"},
        },
        "required": ["tool_calls", "final"],
    }


def load_settings(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("settings file must be an object")
    return data


class SettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict | None = None
        self.mtime: int | None = None
        self.error: str | None = None
        self._missing_logged = False

    def get(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            self.error = "settings.json unreadable"
            if self.data is None:
                # No file yet: serve with the defaults (no backend) and say so once.
                if not self._missing_logged:
                    print(f"settings.json missing at {self.path}; running with defaults", flush=True)
                    self._missing_logged = True
                return {}
            return self.data
        self._missing_logged = False
        if self.data is not None and mtime == self.mtime:
            return self.data
        try:
            data = load_settings(self.path)
        except (OSError, ValueError):
            self.error = "settings.json cannot be parsed"
            if self.data is None:
                raise RuntimeError(self.error) from None
            return self.data
        self.data = data
        self.mtime = mtime
        self.error = None
        return data


def request_backend_ref(body: dict) -> str | None:
    extra = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
    for source in (body.get("backend"), extra.get("backend")):
        if isinstance(source, str) and source:
            return source
    return None


def select_route(body: dict, settings: dict) -> tuple[str, dict, str, str | None]:
    """Body backend, else defaults.backend, else providers.active.

    Returns name, backend, model, and the stage entry's raw effort. A stage entry
    is a backend string, or an object {"model", "effort"} whose model routes as
    that string does. The effort is None for a string entry and for a call that
    is not a stage.

    A disabled backend is an error. <backend>:<model> must name an id in that
    backend's models list when the backend has one. The returned backend dict is
    the one this call keeps if settings.json changes while the call is in flight.
    """
    ref = request_backend_ref(body)
    providers = settings.get("providers") or {}
    if not isinstance(providers, dict):
        providers = {}
    if not ref:
        defaults = settings.get("defaults") or {}
        candidate = defaults.get("backend") if isinstance(defaults, dict) else None
        if isinstance(candidate, str) and candidate:
            ref = candidate
    if not ref:
        active = providers.get("active")
        if isinstance(active, str) and active:
            ref = active
    stage_effort = None
    if isinstance(ref, str) and ref.startswith("@"):
        stages = providers.get("stages")
        resolved = stages.get(ref[1:]) if isinstance(stages, dict) else None
        if isinstance(resolved, dict):
            model_ref = resolved.get("model")
            if not isinstance(model_ref, str) or not model_ref or model_ref.startswith("@"):
                raise ValueError(f"unknown stage alias {safe_text(ref)}")
            stage_effort = resolved.get("effort")
            ref = model_ref
        elif not isinstance(resolved, str) or not resolved or resolved.startswith("@"):
            raise ValueError(f"unknown stage alias {safe_text(ref)}")
        else:
            ref = resolved
    name, model_override = split_ref(ref)
    backends = providers.get("backends") or {}
    if not name or not isinstance(backends, dict) or name not in backends:
        raise ValueError(f"unknown backend {name or ref or ''}".strip())
    backend = backends[name]
    if not isinstance(backend, dict):
        raise ValueError(f"unknown backend {name}")
    if backend.get("enabled") is False:
        raise ValueError(f"backend {name} is disabled")
    key_env = backend.get("key_env")
    if isinstance(key_env, str) and not key_env.endswith("_KEY"):
        raise ValueError(f"key env {key_env} must end in _KEY")
    listed = backend.get("models")
    if model_override:
        if isinstance(listed, list) and model_override not in listed:
            raise ValueError(f"unknown model {model_override} for backend {name}")
        model = model_override
    else:
        model = backend.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError(f"backend {name} has no model")
    return name, backend, model, stage_effort


def _effort_value(value):
    if not isinstance(value, str) or not value:
        return None
    if value not in EFFORTS:
        raise ValueError("reasoning effort must be none, minimal, low, medium, high, xhigh or max")
    return value


def request_effort(body: dict) -> str | None:
    """The effort the caller asked for, or None when the request leaves it unset."""
    output_config = body.get("output_config") if isinstance(body.get("output_config"), dict) else {}
    extra = body.get("extra_body") if isinstance(body.get("extra_body"), dict) else {}
    extra_output = extra.get("output_config") if isinstance(extra.get("output_config"), dict) else {}
    for value in (
        body.get("reasoning_effort"),
        body.get("thinking_effort"),
        output_config.get("effort"),
        extra.get("reasoning_effort"),
        extra.get("thinking_effort"),
        extra_output.get("effort"),
    ):
        found = _effort_value(value)
        if found:
            return found
    return None


def choose_effort(body: dict, backend: dict, stage_effort=None) -> str | None:
    """Request effort, else the stage entry's effort, else the backend's effort or thinking_effort."""
    asked = request_effort(body)
    if asked:
        return asked
    found = _effort_value(stage_effort)
    if found:
        return found
    for key in ("effort", "thinking_effort"):
        found = _effort_value(backend.get(key))
        if found:
            return found
    return None


EFFORT_STYLES = ("openai", "deepseek", "openrouter", "glm", "anthropic", "none")
# Public API hosts that decide the style when a backend has no effort_style. A host matches itself
# and its subdomains.
STYLE_HOSTS = (
    ("openrouter.ai", "openrouter"),
    ("deepseek.com", "deepseek"),
    ("z.ai", "glm"),
    ("bigmodel.cn", "glm"),
    ("api.openai.com", "openai"),
)
# none switches thinking off. Each provider gets its own off value (effort_wire); a CLI has none, so it gets low.
# Each provider gets the nearest level it documents; a level it does not document is never sent.
# A tie between two documented levels goes to the one the provider itself picks (DeepSeek maps
# medium and xhigh to high), and GLM follows the same rule.
_MINIMAL_TO_LOW = {"none": "none", "minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
_LOW_HIGH_MAX = {"none": "none", "minimal": "low", "low": "low", "medium": "high", "high": "high", "xhigh": "high", "max": "max"}
# claude --effort and Codex model_reasoning_effort have no off level, so none is the lowest one.
_CLI = {**_MINIMAL_TO_LOW, "none": "low"}
_PASS = {level: level for level in EFFORTS}
LEVELS = {
    # Messages API output_config.effort: low, medium, high, xhigh, max. minimal -> low as Honcho's
    # _anthropic_effort does. https://platform.claude.com/docs/en/build-with-claude/effort
    # none sends no effort and thinking {"type": "disabled"}; the model table lists which models accept
    # that (Opus 5.5, Sonnet 5.5, Fable and Mythos reject it, Sonnet 5.5 wants "between_tools").
    # https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting
    "anthropic": _MINIMAL_TO_LOW,
    # claude --effort: low, medium, high, xhigh, max. https://code.claude.com/docs/en/cli-reference
    "claude": _CLI,
    # Codex config model_reasoning_effort: the reference lists low, medium, high, xhigh, max (and
    # ultra) and says the levels depend on the model; minimal is not listed, so it becomes low.
    # https://developers.openai.com/codex/config-reference
    "codex": _CLI,
    # reasoning_effort: the API reference lists none, minimal, low, medium, high, xhigh, max, model
    # dependent. The GPT-6 model pages (gpt-6-luna, gpt-6.1-sol) list low to max without minimal, so
    # minimal becomes low. xhigh and max are not confirmed on every GPT-6 model page. none is sent as
    # it is; the guide says gpt-6-astra and gpt-6.1-sol refuse it (HTTP 400).
    # https://developers.openai.com/api/docs/guides/reasoning
    "openai": _MINIMAL_TO_LOW,
    # reasoning_effort: none, low, high, max; minimal is mapped to low and medium/xhigh to high by
    # DeepSeek. "none disables thinking mode", and with no thinking object the mode follows
    # reasoning_effort, so none sends reasoning_effort alone. https://api-docs.deepseek.com/api/create-chat-completion
    "deepseek": _LOW_HIGH_MAX,
    # reasoning_effort from GLM-5.2; GLM-5.3 supports low, high, max only. none is thinking
    # {"type": "disabled"}, which the same page lists for GLM-4.6 and later except the GLM-5.3 series,
    # whose thinking "can only be enabled". https://docs.z.ai/api-reference/llm/chat-completion
    "glm": _LOW_HIGH_MAX,
    # reasoning.effort documents every level, "none" disables reasoning entirely (a model flagged
    # mandatory refuses it). https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
    "openrouter": _PASS,
    # An unknown host gets the level unchanged, none included.
    "generic": _PASS,
}
GLM_VERSION = re.compile(r"glm-(\d+)(?:\.(\d+))?", re.IGNORECASE)


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def effort_style(backend: dict) -> str:
    """The provider shape for effort: the kind for a CLI, else effort_style, else the base_url host.

    effort and thinking_effort are only the backend's default level; they do not pick the shape.
    """
    kind = backend.get("kind")
    if kind == "cli-codex":
        return "codex"
    if kind == "cli-claude":
        return "claude"
    explicit = backend.get("effort_style")
    if explicit is not None:
        if explicit not in EFFORT_STYLES:
            raise ValueError("effort_style must be openai, deepseek, openrouter, glm, anthropic or none")
        if kind == "anthropic-api" and explicit not in ("anthropic", "none"):
            raise ValueError("effort_style of an anthropic-api backend must be anthropic or none")
        return explicit
    if kind == "anthropic-api":
        return "anthropic"
    host = (urllib.parse.urlparse(backend.get("base_url") or "").hostname or "").lower()
    for domain, style in STYLE_HOSTS:
        if _host_matches(host, domain):
            return style
    return "generic"


def glm_has_effort(model: str | None) -> bool:
    """Z.ai documents reasoning_effort from GLM-5.2. An id without a readable version gets none."""
    match = GLM_VERSION.search(model or "")
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2) or 0)) >= (5, 2)


# OpenAI models whose reasoning cannot be switched off: none returns HTTP 400 on them.
# https://developers.openai.com/api/docs/guides/reasoning
OPENAI_NO_NONE = ("gpt-6-astra", "gpt-6.1-sol")


def glm_can_disable(model: str | None) -> bool:
    """Z.ai documents thinking {"type": "disabled"} from GLM-4.6; the GLM-5.3 series "can only be enabled"."""
    match = GLM_VERSION.search(model or "")
    if not match:
        return False
    version = (int(match.group(1)), int(match.group(2) or 0))
    return version >= (4, 6) and version != (5, 3)


def provider_effort(backend: dict, effort: str | None, model: str | None = None) -> str | None:
    """The level this backend's provider receives for one requested level, or None to send nothing."""
    if not effort:
        return None
    style = effort_style(backend)
    if style == "none":
        return None
    if style == "openai" and effort == "none" and (model or backend.get("model")) in OPENAI_NO_NONE:
        # These models refuse reasoning_effort none (HTTP 400): the lowest level they take instead.
        return "low"
    if style == "glm":
        if effort == "none":
            return "none" if glm_can_disable(model or backend.get("model")) else None
        if not glm_has_effort(model or backend.get("model")):
            return None
    return LEVELS[style].get(effort)


def effort_wire(backend: dict, effort: str | None, model: str | None = None) -> dict:
    """Provider field for one requested level. Empty when the kind carries effort on the command line."""
    level = provider_effort(backend, effort, model)
    if not level:
        return {}
    kind = backend.get("kind")
    if kind == "anthropic-api":
        # none is no effort and thinking off, which to_anthropic builds from the level.
        if level == "none":
            off = anthropic_thinking_off(model or backend.get("model"))
            return {"thinking": off} if off else {"output_config": {"effort": "low"}}
        return {"output_config": {"effort": level}}
    if kind != "openai-compat":
        return {}
    style = effort_style(backend)
    if style == "glm" and level == "none":
        return {"thinking": {"type": "disabled"}}
    if style == "openrouter":
        return {"reasoning": {"effort": level}}
    if style == "deepseek":
        # none is reasoning_effort alone: with no thinking object DeepSeek's mode follows it, and enabled would turn thinking on.
        return {"reasoning_effort": level} if level == "none" else {"thinking": {"type": "enabled"}, "reasoning_effort": level}
    return {"reasoning_effort": level}


# Default for bridge-calls.jsonl. settings.json "logs" may set max_mb and keep.
LOG_MAX_BYTES = 10 * 1024 * 1024


def log_time() -> str:
    """UTC timestamp for the call log, with a Z suffix."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def log_limits(settings: dict) -> tuple[int, int]:
    """Byte size that rotates the call log, and how many previous files to keep."""
    logs = settings.get("logs") if isinstance(settings.get("logs"), dict) else {}
    limit, keep = LOG_MAX_BYTES, 1
    if "max_mb" in logs:
        try:
            max_mb = int(logs["max_mb"])
            if max_mb < 1:
                raise ValueError
            limit = max_mb * 1024 * 1024
        except (TypeError, ValueError):
            pass
    if "keep" in logs:
        try:
            parsed = int(logs["keep"])
            if parsed < 1:
                raise ValueError
            keep = parsed
        except (TypeError, ValueError):
            pass
    return limit, keep


def rotate_log(path: Path, limit: int, keep: int = 1) -> None:
    try:
        if path.stat().st_size < limit or keep < 1:
            return
        for index in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{index}")
            if src.exists():
                os.replace(src, path.with_name(f"{path.name}.{index + 1}"))
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


# Below the OpenAI client's 600 s default timeout (openai-python DEFAULT_TIMEOUT), which Honcho keeps.
MAX_CALL_SECONDS = 570
# Read/write timeout on an accepted connection: a client that stalls between bytes is dropped and its thread released.
SOCKET_TIMEOUT_SECONDS = 60


class FinalError(RuntimeError):
    """A failure that the same request would meet again. Answered with 422, which the OpenAI client
    does not resend, instead of 502, which it resends twice."""


def limits(settings: dict) -> tuple[int, int]:
    providers = settings.get("providers") if isinstance(settings.get("providers"), dict) else {}
    try:
        concurrency = int(providers.get("concurrency"))
        if concurrency < 1:
            raise ValueError
    except (TypeError, ValueError):
        concurrency = int(os.environ.get("BRIDGE_CONCURRENCY", "6"))
    try:
        timeout = int(providers.get("timeout_seconds"))
        if timeout < 1:
            raise ValueError
    except (TypeError, ValueError):
        timeout = int(os.environ.get("BRIDGE_TIMEOUT", str(MAX_CALL_SECONDS)))
    # Honcho's OpenAI client gives up after 600 s and sends the call again (twice), and Honcho retries
    # on top: a CLI call longer than that ran up to nine times. The bridge answers first.
    return concurrency, min(timeout, MAX_CALL_SECONDS)


def model_list(settings: dict) -> dict:
    data = []
    providers = settings.get("providers") if isinstance(settings.get("providers"), dict) else {}
    backends = providers.get("backends") if isinstance(providers.get("backends"), dict) else {}
    for name, backend in backends.items():
        if not isinstance(backend, dict):
            continue
        kind = backend.get("kind") or ""
        data.append({"id": name, "object": "model", "owned_by": kind})
        for model in backend.get("models") or []:
            if isinstance(model, str) and model:
                data.append({"id": f"{name}:{model}", "object": "model", "owned_by": kind})
    return {"object": "list", "data": data}


def chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


# Providers whose response_format documents text and json_object only: DeepSeek
# (https://api-docs.deepseek.com/api/create-chat-completion) and GLM
# (https://docs.z.ai/api-reference/llm/chat-completion).
JSON_OBJECT_ONLY = ("deepseek", "glm")


def schema_instruction(schema, tools_present: bool) -> str:
    """The JSON-schema request as text, worded like Honcho's own prompt for json_object mode."""
    lead = "If you are not calling a tool, respond with" if tools_present else "You must respond with"
    if not isinstance(schema, dict):
        return f"{lead} a single JSON object (json). Do not include any text, markdown, or code fences outside it."
    return (
        f"{lead} a single JSON object (json) that conforms exactly to the following JSON schema. Do not include any "
        "text, markdown, or code fences outside the JSON object.\n\nJSON schema:\n" + json.dumps(schema, ensure_ascii=False)
    )


def with_instruction(messages: list, text: str) -> list:
    """A copy of messages with text added to the first system or developer message, or a new system message."""
    copied = [dict(message) if isinstance(message, dict) else message for message in messages]
    for message in copied:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = (content + "\n\n" + text) if content else text
            return copied
        if isinstance(content, list):
            message["content"] = [*content, {"type": "text", "text": text}]
            return copied
        break
    return [{"role": "system", "content": text}, *copied]


def openai_payload(body: dict, model: str, wire: dict, style: str = "generic") -> dict:
    payload = {}
    for key in FORWARD_KEYS:
        if key in body and body[key] is not None:
            payload[key] = body[key]
    payload["model"] = model
    if style == "openai" and "max_tokens" in payload:
        # max_tokens is deprecated in favour of max_completion_tokens and rejected by the reasoning
        # models; Honcho itself switches for gpt-5, gpt-6 and the o-series.
        # https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
        cap = payload.pop("max_tokens")
        payload.setdefault("max_completion_tokens", cap)
    response_format = payload.get("response_format")
    if style in JSON_OBJECT_ONLY and isinstance(response_format, dict) and response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        payload["response_format"] = {"type": "json_object"}
        payload["messages"] = with_instruction(payload.get("messages") or [], schema_instruction(schema, bool(payload.get("tools"))))
    payload.update(wire)
    return payload


def _tool_input(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments:
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# Claude 4.6 and later think adaptively; Honcho's own Anthropic client asks for it whenever it sends an effort.
CLAUDE_VERSION = re.compile(r"claude-[a-z]+-(\d+)(?:-(\d{1,2}))?(?![0-9])")


def adaptive_claude(model: str | None) -> bool:
    match = CLAUDE_VERSION.search(model or "")
    return bool(match) and (int(match[1]), int(match[2] or 0)) >= (4, 6)


def anthropic_thinking_off(model: str | None) -> dict | None:
    """How a Claude model switches thinking off, or None when it cannot (thinking is always on).

    Claude Fable, Mythos and Opus 5.5 and later refuse thinking {"type": "disabled"}; Sonnet 5.5 and later refuse it
    too and take {"type": "between_tools"} (no up-front thinking). Every other model takes disabled.
    https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting
    """
    match = re.search(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?![0-9])", model or "")
    if not match:
        return {"type": "disabled"}
    family, version = match[1], (int(match[2]), int(match[3] or 0))
    if family in ("fable", "mythos") or (family == "opus" and version >= (5, 5)):
        return None
    if family == "sonnet" and version >= (5, 5):
        return {"type": "between_tools"}
    return {"type": "disabled"}


def anthropic_tool_choice(choice):
    """OpenAI tool_choice in Anthropic's shape, as Honcho's own Anthropic client converts it."""
    if choice in (None, ""):
        return None
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            return {"type": "tool", "name": (choice.get("function") or {}).get("name")}
        return choice if choice.get("type") in ("auto", "any", "none", "tool") else None
    return {"auto": {"type": "auto"}, "required": {"type": "any"}, "any": {"type": "any"}, "none": {"type": "none"}}.get(choice, {"type": "tool", "name": choice})


def to_anthropic(body: dict, model: str, effort: str | None) -> dict:
    messages = body.get("messages") or []
    system_parts = [text_of(message.get("content")) for message in messages if message.get("role") in ("system", "developer")]
    converted = []
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            continue
        if role == "assistant":
            blocks = []
            text = text_of(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                blocks.append({
                    "type": "tool_use",
                    "id": call.get("id") or "call",
                    "name": function.get("name"),
                    "input": _tool_input(function.get("arguments")),
                })
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": text_of(message.get("content")),
            }
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list) and converted[-1]["content"] and converted[-1]["content"][0].get("type") == "tool_result":
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue
        converted.append({"role": "user" if role != "assistant" else "assistant", "content": text_of(message.get("content"))})
    # Messages API structured output is output_config.format type json_schema. There is no
    # json_object type, so that request uses the same system-message wording as DeepSeek and GLM.
    # https://platform.claude.com/docs/en/build-with-claude/structured-outputs
    response_format = body.get("response_format") if isinstance(body.get("response_format"), dict) else {}
    schema = (response_format.get("json_schema") or {}).get("schema") if response_format.get("type") == "json_schema" else None
    if response_format.get("type") == "json_object" or (response_format.get("type") == "json_schema" and not isinstance(schema, dict)):
        # A json_schema request without a schema is asked for as a JSON object, never sent as a null schema.
        system_parts.append(schema_instruction(None, bool(body.get("tools"))))
    payload = {
        "model": model,
        "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or 4096,
        "messages": converted,
    }
    if system_parts:
        # Cached like Honcho's own Anthropic client does: the system prompt repeats on every call of a job.
        payload["system"] = [{"type": "text", "text": "\n\n".join(part for part in system_parts if part), "cache_control": {"type": "ephemeral"}}]
    for key in ("temperature", "top_p", "top_k"):
        if body.get(key) is not None:
            payload[key] = body[key]
    stop = body.get("stop")
    if stop:
        payload["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    # Anthropic-only fields a job sets in config.toml (provider_params extra_body) reach the bridge as top-level
    # request fields and pass through unchanged: prompt caching, and thinking switched off or set.
    for key in ("cache_control", "thinking"):
        if isinstance(body.get(key), dict):
            payload[key] = body[key]
    if "thinking" not in payload and effort == "none":
        payload["thinking"] = anthropic_thinking_off(model)
        if payload["thinking"] is None:
            # Thinking is always on for this model: none becomes the lowest effort instead.
            del payload["thinking"]
            effort = "low"
    elif "thinking" not in payload and effort and adaptive_claude(model):
        payload["thinking"] = {"type": "adaptive"}
    tools = []
    for tool in body.get("tools") or []:
        function = tool.get("function", tool)
        tools.append({
            "name": function["name"],
            "description": function.get("description") or "",
            "input_schema": function.get("parameters") or function.get("input_schema") or {"type": "object", "properties": {}},
        })
    if tools:
        payload["tools"] = tools
        choice = anthropic_tool_choice(body.get("tool_choice"))
        thinking = (payload.get("thinking") or {}).get("type") in ("adaptive", "enabled")
        if choice and thinking and choice.get("type") in ("any", "tool"):
            # Anthropic refuses a forced tool with thinking on; Honcho's own client relaxes it the same way.
            choice = {"type": "auto"}
        if choice:
            payload["tool_choice"] = choice
    output_config = {}
    if effort and effort != "none":
        output_config["effort"] = effort
    if isinstance(schema, dict):
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if output_config:
        payload["output_config"] = output_config
    return payload


def from_anthropic(payload: dict, model: str) -> dict:
    blocks = payload.get("content") or []
    text = "\n".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
    calls = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"]
    if calls:
        message = {
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {
                    "id": call.get("id") or ("call_" + uuid.uuid4().hex[:24]),
                    "type": "function",
                    "function": {"name": call.get("name"), "arguments": json.dumps(call.get("input") or {})},
                }
                for call in calls
            ],
        }
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish = "stop"
    usage = payload.get("usage") or {}
    usage_in = sum(usage.get(key) or 0 for key in
                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    usage_out = usage.get("output_tokens") or 0
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or model,
        "_upstream_model": payload.get("model"),
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {**(payload.get("usage") or {}), "prompt_tokens": usage_in, "completion_tokens": usage_out, "total_tokens": usage_in + usage_out,
                  "prompt_tokens_details": {"cached_tokens": usage.get("cache_read_input_tokens") or 0}},
    }


def post_json(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = safe_text(exc.read().decode("utf-8", "replace"))
        raise RuntimeError(detail or f"HTTP {exc.code}") from None


def env_key(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise FinalError(f"key env {name} is unset")
    return value


def cli_cwd(kind: str) -> Path:
    root = os.environ.get("BRIDGE_CWD")
    if root:
        path = Path(root) / kind
    elif os.name != "nt" and Path("/app").is_dir():
        path = Path("/app") / f"cwd-{kind}"
    else:
        path = Path(tempfile.gettempdir()) / "honcho-bridge" / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


CLI_ENV_DROP = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|_URI)$", re.IGNORECASE)


def cli_env() -> dict:
    """The bridge's environment without keys, tokens, passwords and connection URIs.

    The bridge loads Honcho's .env, which holds every key of the deployment. A CLI must use its own
    login (SECRETS_DIR), not an API key it happens to find: ANTHROPIC_API_KEY would make the claude
    CLI bill the API while the call is logged as billing_route "cli", and a prompt could ask an
    agent CLI to print its environment.
    """
    return {name: value for name, value in os.environ.items() if not CLI_ENV_DROP.search(name)}


def run_process(argv: list[str], stdin_text: str, timeout: int, cwd: Path) -> str:
    # Own process group, so a timeout stops the CLI and what it started (the npm launcher starts a
    # native binary), not only the launcher.
    posix = os.name != "nt"
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        env=cli_env(),
        start_new_session=posix,
    )
    try:
        stdout, stderr = process.communicate(stdin_text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        if posix:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        process.kill()
        process.communicate()
        raise FinalError(f"timed out after {timeout}s") from None
    out = stdout.decode("utf-8", "replace")
    if process.returncode != 0 and not out:
        raise RuntimeError(f"exit {process.returncode}: {safe_text(stderr.decode('utf-8', 'replace'))}")
    return out


def codex_argv(prefix: list[str], model: str, effort: str | None, schema_path: str) -> list[str]:
    args = ["exec", "-m", model]
    if effort:
        args.extend(["-c", f'model_reasoning_effort="{effort}"'])
    args.extend([
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--output-schema",
        schema_path,
        "--json",
        "-",
    ])
    return resolve_argv(prefix, args)


def claude_argv(prefix: list[str], model: str, effort: str | None, schema: dict, system: str) -> tuple[list[str], str]:
    prompt_system = system
    args = [
        "-p",
        "--model",
        model,
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--tools",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--system-prompt",
        prompt_system if len(prompt_system) <= 20000 else "Follow the instructions in the user message.",
    ]
    if effort:
        args.extend(["--effort", effort])
    return resolve_argv(prefix, args), prompt_system


def parse_codex_raw(out: str):
    text = None
    pin = 0
    pout = 0
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            text = item.get("text")
        usage = event.get("usage") or {}
        pin += usage.get("input_tokens", 0) or 0
        pout += usage.get("output_tokens", 0) or 0
    if text is None:
        raise RuntimeError("codex: no agent_message in output")
    return text, pin, pout


def parse_codex(out: str) -> tuple[dict, int, int]:
    text, pin, pout = parse_codex_raw(out)
    structured = json.loads(text)
    structured["tool_calls"] = [
        {"name": call["name"], "arguments": json.loads(call.get("arguments_json") or "{}")}
        for call in structured.get("tool_calls") or []
    ]
    return structured, pin, pout


def parse_claude_raw(out: str):
    result = json.loads(out)
    if result.get("is_error"):
        raise RuntimeError(safe_text(str(result.get("result") or "claude error")))
    structured = result.get("structured_output")
    text = json.dumps(structured) if structured is not None else result.get("result") or "{}"
    usage = result.get("usage") or {}
    pin = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    pout = usage.get("output_tokens") or 0
    return text, pin, pout


def parse_claude(out: str) -> tuple[dict, int, int]:
    text, pin, pout = parse_claude_raw(out)
    return json.loads(text), pin, pout


def completion_from_structured(structured: dict, model: str, pin: int, pout: int) -> dict:
    calls = [call for call in (structured.get("tool_calls") or []) if call.get("name")]
    if not calls and not (structured.get("final") or "").strip():
        raise FinalError("empty answer")
    if calls:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_" + uuid.uuid4().hex[:24],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments") or {})},
                }
                for call in calls
            ],
        }
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": structured.get("final") or ""}
        finish = "stop"
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": pin, "completion_tokens": pout, "total_tokens": pin + pout},
        "_final_chars": len(structured.get("final") or ""),
        "_upstream_model": None,
    }


def render_tool_free(body: dict) -> tuple[str, str, bool]:
    messages = body.get("messages") or []
    system = "\n\n".join(text_of(message.get("content")) for message in messages if message.get("role") in ("system", "developer"))
    parts = []
    for message in messages:
        if message.get("role") in ("system", "developer"):
            continue
        parts.append(f"<{message.get('role')}>\n{text_of(message.get('content'))}\n</{message.get('role')}>")
        if message.get("tool_calls"):
            parts.append("Previous tool calls: " + json.dumps(message["tool_calls"], ensure_ascii=False))
    response_format = body.get("response_format")
    structured = bool(body.get("json_mode")) or isinstance(response_format, dict) and response_format.get("type") in ("json_schema", "json_object")
    if structured:
        schema = (response_format.get("json_schema") or {}).get("schema") if isinstance(response_format, dict) else None
        parts.append("Return only a valid JSON document, without markdown fences." + (" It must match this schema: " + json.dumps(schema, ensure_ascii=False) if schema is not None else ""))
    else:
        parts.append("Return your plain answer in the final string. No tools are available.")
    cap = body.get("max_tokens") or body.get("max_completion_tokens")
    if type(cap) is int and cap > 0:
        parts.append(f"Keep the answer within {cap} output tokens (best effort).")
    return system or "You are a helpful assistant.", "\n\n".join(parts), structured


def usage_of(payload: dict) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    pin = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    pout = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    return int(pin), int(pout)


def tool_names(payload: dict) -> list[str]:
    message = ((payload.get("choices") or [{}])[0].get("message") or {})
    return [call.get("function", {}).get("name") for call in message.get("tool_calls") or [] if call.get("function")]


class Gate:
    def __init__(self):
        self._limit = 6
        self._held = 0
        self._cv = threading.Condition()

    def set_limit(self, limit: int) -> None:
        with self._cv:
            self._limit = max(1, limit)
            self._cv.notify_all()

    def acquire(self, timeout: float) -> bool:
        """Take a slot within timeout seconds, counted by the caller from when the request arrived."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._held >= self._limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
            self._held += 1
            return True

    def release(self) -> None:
        with self._cv:
            self._held -= 1
            self._cv.notify()


class Bridge:
    def __init__(self, path: Path | None = None, log_file: Path | None = None):
        self.store = SettingsStore(path or settings_path())
        self.log_file = Path(log_file) if log_file is not None else log_path()
        self.gate = Gate()
        self.stats = {"calls": 0, "errors": 0, "tool_turns": 0, "final_turns": 0, "in": 0, "out": 0}
        self._log_lock = threading.Lock()
        self.log_write_errors = 0
        self.log_ok = True
        self.routing = {}
        self.unauthorized = 0

    def note_unauthorized(self) -> None:
        with self._log_lock:
            self.unauthorized += 1

    def write_log(self, record: dict) -> None:
        try:
            settings = self.store.get()
        except Exception:
            settings = {}
        limit, keep = log_limits(settings if isinstance(settings, dict) else {})
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with self._log_lock:
                rotate_log(self.log_file, limit, keep)
                with open(self.log_file, "a", encoding="utf-8") as handle:
                    handle.write(line)
                self.log_ok = True
        except OSError:
            with self._log_lock:
                self.log_write_errors += 1
                self.log_ok = False

    def handle(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        route = urllib.parse.urlparse(path).path
        if method == "GET" and route == "/health":
            return 200, self.health()
        if method == "GET" and route == "/v1/models":
            try:
                settings = self.store.get()
            except Exception as exc:
                return 503, {"error": {"message": safe_text(str(exc))}}
            return 200, model_list(settings)
        if method == "POST" and route == "/v1/chat/completions":
            return self.chat(body or {})
        return 404, {"error": {"message": "not found"}}

    def health(self) -> dict:
        active = None
        concurrency = None
        try:
            settings = self.store.get()
            active = (settings.get("providers") or {}).get("active")
            concurrency, timeout = limits(settings)
            self.gate.set_limit(concurrency)
        except Exception:
            timeout = None
        return {"ok": self.store.error is None, "settings_ok": self.store.error is None,
                "settings_error": self.store.error, "active": active, "concurrency": concurrency,
                "timeout_seconds": timeout, "log_ok": self.log_ok,
                "log_write_errors": self.log_write_errors,
                "auth_required": bool(bridge_token()), "unauthorized": self.unauthorized,
                "routing": self.routing_snapshot(), **self.stats}

    def routing_snapshot(self) -> dict:
        with self._log_lock:
            return {name: {**entry, "last_call": dict(entry["last_call"])}
                    for name, entry in self.routing.items()}

    def record_route(self, backend: str, record: dict, error: bool = False) -> None:
        with self._log_lock:
            # Bound history even if backend names change repeatedly on reload.
            if backend not in self.routing and len(self.routing) >= 64:
                del self.routing[next(iter(self.routing))]
            entry = self.routing.setdefault(backend, {"calls": 0, "errors": 0})
            entry["errors" if error else "calls"] += 1
            entry["last_call"] = record

    def chat(self, body: dict) -> tuple[int, dict]:
        # The call budget starts here, before the wait for a concurrency slot. Honcho's client
        # gives up at 600 s and sends the same request again; time spent queued has to come out
        # of the 570 s cap or that second copy meets the first one still running.
        arrived = time.monotonic()
        if not isinstance(body, dict):
            return 400, {"error": {"message": "body must be an object"}}
        backend_name = None
        model = None
        requested_model = safe_text(body["model"]) if isinstance(body.get("model"), str) else None
        try:
            settings = self.store.get()
            concurrency, timeout = limits(settings)
            self.gate.set_limit(concurrency)
            backend_name, backend, model, stage_effort = select_route(body, settings)
            effort = choose_effort(body, backend, stage_effort)
            asked = request_effort(body)
            # The level the provider receives: the nearest one it documents, or None.
            sent = provider_effort(backend, effort, model)
        except ValueError as exc:
            self.write_log({"ts": log_time(), "error": safe_text(str(exc)),
                            "backend": backend_name, "requested_model": requested_model,
                            "resolved_model": model, "upstream_model": None})
            return 400, {"error": {"message": safe_text(str(exc))}}
        except Exception as exc:
            return 503, {"error": {"message": safe_text(str(exc))}}
        kind = backend.get("kind")
        tools = body.get("tools") or []
        started = time.time()
        acquired = False
        try:
            if kind in ("cli-codex", "cli-claude"):
                require_cli(kind)
            remaining = timeout - (time.monotonic() - arrived)
            if remaining <= 0 or not self.gate.acquire(remaining):
                raise FinalError(f"timed out after {timeout}s")
            acquired = True
            left = timeout - (time.monotonic() - arrived)
            if left <= 0:
                raise FinalError(f"timed out after {timeout}s")
            if kind in ("cli-codex", "cli-claude") and not tools:
                payload, pin, pout = self._tool_free(body, model, sent, left, kind)
            elif kind == "cli-codex":
                payload, pin, pout = self._codex(body, model, sent, left)
            elif kind == "cli-claude":
                payload, pin, pout = self._claude(body, model, sent, left)
            elif kind == "openai-compat":
                payload = self._openai(body, backend, model, effort, left)
                pin, pout = usage_of(payload)
            elif kind == "anthropic-api":
                payload = self._anthropic(body, backend, model, sent, left)
                pin, pout = usage_of(payload)
            else:
                return 400, {"error": {"message": f"unknown kind {kind}"}}
        except Exception as exc:
            self.stats["errors"] += 1
            evidence = {
                "ts": log_time(),
                "requested_model": requested_model,
                "resolved_model": safe_text(model),
                "upstream_model": None,
                "billing_route": "cli" if kind in ("cli-codex", "cli-claude") else "api",
                "ok": False,
            }
            self.record_route(backend_name, evidence, error=True)
            self.write_log({
                "ts": log_time(),
                "error": safe_text(str(exc))[:800],
                "backend": backend_name,
                **evidence,
            })
            status = 422 if isinstance(exc, FinalError) else 502
            return status, {"error": {"message": safe_text(str(exc))[:800]}}
        finally:
            if acquired:
                self.gate.release()
        final_chars = payload.pop("_final_chars", None) if isinstance(payload, dict) else None
        names = tool_names(payload)
        self.stats["calls"] += 1
        self.stats["in"] += pin
        self.stats["out"] += pout
        self.stats["tool_turns" if names else "final_turns"] += 1
        message = ((payload.get("choices") or [{}])[0].get("message") or {})
        if final_chars is None:
            final_chars = len(message.get("content") or "")
        billing_route = "cli" if kind in ("cli-codex", "cli-claude") else "api"
        upstream_model = payload.pop("_upstream_model", payload.get("model"))
        if not isinstance(upstream_model, str) or not upstream_model.strip():
            upstream_model = None
        payload["model"] = model if not upstream_model or upstream_model.startswith("@") or upstream_model == "stage-route" else upstream_model
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = payload["usage"] = {}
        usage.update(billing_route=billing_route, backend=backend_name, resolved_model=model)
        if upstream_model:
            usage["upstream_model"] = upstream_model
        evidence = {"ts": log_time(),
                    "requested_model": requested_model, "resolved_model": safe_text(model),
                    "upstream_model": safe_text(upstream_model) if upstream_model else None,
                    "billing_route": billing_route, "ok": True}
        self.record_route(backend_name, evidence)
        self.write_log({
            "ts": log_time(),
            "secs": round(time.time() - started, 1),
            "in": pin,
            "out": pout,
            "effort": effort or "",
            "sent_effort": sent,
            "asked_effort": asked,
            "tool_calls": names,
            "final_chars": final_chars,
            "backend": backend_name,
            "billing_route": billing_route,
            **evidence,
        })
        if body.get("stream"):
            return 200, {"_stream": True, "payload": payload}
        return 200, payload

    def _tool_free(self, body, model, effort, timeout, kind):
        system, prompt, structured_json = render_tool_free(body)
        schema = {"type": "object", "additionalProperties": False, "properties": {"final": {"type": "string"}}, "required": ["final"]}
        if structured_json:
            response_format = body.get("response_format") or {}
            if response_format.get("type") == "json_schema":
                schema = (response_format.get("json_schema") or {}).get("schema")
                if not isinstance(schema, dict):
                    raise FinalError("tool-free JSON schema is missing or invalid")
            else:
                schema = {"type": "object", "additionalProperties": True}
            prompt += "\nReturn the JSON document itself, matching the native output schema; do not wrap it in final."
        deadline = time.monotonic() + timeout
        pin = pout = 0
        cwd = cli_cwd("codex" if kind == "cli-codex" else "claude")
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FinalError("tool-free CLI timed out")
            schema_path = None
            try:
                if kind == "cli-codex":
                    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", dir=cwd, delete=False) as handle:
                        json.dump(schema, handle)
                        schema_path = handle.name
                    argv = codex_argv(exe_prefix("BRIDGE_EXE", "codex"), model, effort, schema_path)
                    out = run_process(argv, f"<instructions>\n{system}\n</instructions>\n\n{prompt}", remaining, cwd)
                    raw, used_in, used_out = parse_codex_raw(out)
                else:
                    actual_system = system if len(system) <= 20000 else "Follow the instructions in the user message."
                    actual_prompt = prompt if actual_system == system else f"<instructions>\n{system}\n</instructions>\n\n{prompt}"
                    argv, _system = claude_argv(exe_prefix("BRIDGE_CLAUDE", "claude"), model, effort, schema, actual_system)
                    out = run_process(argv, actual_prompt, remaining, cwd)
                    raw, used_in, used_out = parse_claude_raw(out)
                pin += used_in
                pout += used_out
                value = json.loads(raw)
                if structured_json:
                    if (body.get("response_format") or {}).get("type") != "json_schema" and not isinstance(value, dict):
                        raise ValueError("JSON object required")
                    final = json.dumps(value, ensure_ascii=False, allow_nan=False)
                else:
                    final = value.get("final")
                    if not isinstance(final, str) or not final.strip():
                        raise ValueError("final string is empty")
                return completion_from_structured({"final": final}, model, pin, pout), pin, pout
            except (ValueError, TypeError, AttributeError) as exc:
                if attempt:
                    raise FinalError("tool-free CLI did not return valid JSON/final content after one repair") from None
                # Quote only the safe error class; parser messages may contain model output.
                prompt += "\nThe previous answer failed validation: " + json.dumps(type(exc).__name__) + (". Return only the valid JSON document matching the native schema." if structured_json else ". Return a nonempty final string.")
            finally:
                if schema_path:
                    try:
                        os.remove(schema_path)
                    except OSError:
                        pass

    def _codex(self, body, model, effort, timeout):
        system, prompt, schema = render(body.get("messages") or [], body.get("tools") or [], body.get("tool_choice"), body.get("response_format"))
        names = schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"]
        cwd = cli_cwd("codex")
        schema_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", dir=cwd, delete=False) as handle:
                json.dump(strict_schema(names), handle)
                schema_path = handle.name
            argv = codex_argv(exe_prefix("BRIDGE_EXE", "codex"), model, effort, schema_path)
            out = run_process(argv, f"<instructions>\n{system}\n</instructions>\n\n{prompt}", timeout, cwd)
        finally:
            if schema_path:
                try:
                    os.remove(schema_path)
                except OSError:
                    pass
        structured, pin, pout = parse_codex(out)
        return completion_from_structured(structured, model, pin, pout), pin, pout

    def _claude(self, body, model, effort, timeout):
        system, prompt, schema = render(body.get("messages") or [], body.get("tools") or [], body.get("tool_choice"), body.get("response_format"))
        if len(system) > 20000:
            prompt = f"<instructions>\n{system}\n</instructions>\n\n{prompt}"
            system = "Follow the instructions in the user message."
        argv, _system = claude_argv(exe_prefix("BRIDGE_CLAUDE", "claude"), model, effort, schema, system)
        out = run_process(argv, prompt, timeout, cli_cwd("claude"))
        structured, pin, pout = parse_claude(out)
        return completion_from_structured(structured, model, pin, pout), pin, pout

    def _openai(self, body, backend, model, effort, timeout):
        key = env_key(backend["key_env"])
        url = chat_url(backend["base_url"])
        payload = openai_payload(body, model, effort_wire(backend, effort, model), effort_style(backend))
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        return post_json(url, headers, payload, timeout)

    def _anthropic(self, body, backend, model, effort, timeout):
        key = env_key(backend["key_env"])
        url = os.environ.get("ANTHROPIC_API_URL", ANTHROPIC_URL)
        payload = to_anthropic(body, model, effort)
        headers = {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        raw = post_json(url, headers, payload, timeout)
        return from_anthropic(raw, model)


def sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def stream_bytes(payload: dict) -> bytes:
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")
    delta = {"role": "assistant"}
    if message.get("tool_calls"):
        delta["tool_calls"] = [dict(call, index=index) for index, call in enumerate(message["tool_calls"])]
    else:
        delta["content"] = message.get("content") or ""
    head = {
        "id": payload.get("id"),
        "object": "chat.completion.chunk",
        "created": payload.get("created"),
        "model": payload.get("model"),
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    tail = {
        "id": payload.get("id"),
        "object": "chat.completion.chunk",
        "created": payload.get("created"),
        "model": payload.get("model"),
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        "usage": payload.get("usage"),
    }
    return sse(head) + sse(tail) + b"data: [DONE]\n\n"


# Honcho's largest prompts (dream specialists with tool results) stay well under this.
MAX_BODY_BYTES = 16 * 1024 * 1024
# What /health tells a caller without the token: up or not, nothing about backends,
# models, routes or counts.
PUBLIC_HEALTH_KEYS = ("ok", "settings_ok", "log_ok")


def bridge_token() -> str:
    """The shared token callers must send; empty means the endpoint is open."""
    return os.environ.get("BRIDGE_TOKEN", "").strip()


def token_ok(header: str | None, token: str | None = None) -> bool:
    """True when no token is configured, or when the header carries it as a Bearer.

    The comparison is constant-time over the bytes, so the time taken does not tell
    how much of a guess matched.
    """
    token = bridge_token() if token is None else token
    if not token:
        return True
    scheme, _, value = (header or "").strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


def public_health(health: dict) -> dict:
    return {key: health.get(key) for key in PUBLIC_HEALTH_KEYS}


def handler_factory(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Applied to the accepted socket before the first read. The 570 s call budget is separate: a provider call
        # in flight holds no socket read, and the answer is one write.
        timeout = SOCKET_TIMEOUT_SECONDS

        def log_message(self, fmt, *args):
            return

        def _authorized(self) -> bool:
            if token_ok(self.headers.get("Authorization")):
                return True
            bridge.note_unauthorized()
            return False

        def _refuse(self) -> None:
            # The body is not read, so this connection cannot carry another request.
            self.close_connection = True
            blob = json.dumps({"error": {"message": "unauthorized"}}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(blob)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            # A negative length made rfile.read wait for the connection to close.
            if length < 0:
                raise ValueError("bad Content-Length")
            if length > MAX_BODY_BYTES:
                raise ValueError("body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _send(self, status: int, payload: dict, stream: bool) -> None:
            if stream:
                blob = stream_bytes(payload)
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            blob = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self):
            if not token_ok(self.headers.get("Authorization")):
                # /health stays open for the container healthcheck, reduced to up/down.
                if urllib.parse.urlparse(self.path).path == "/health":
                    self._send(200, public_health(bridge.health()), False)
                    return
                bridge.note_unauthorized()
                self._refuse()
                return
            status, payload = bridge.handle("GET", self.path, None)
            self._send(status, payload, False)

        def do_POST(self):
            # Checked before the body is read: an unauthorised caller costs no buffer.
            if not self._authorized():
                self._refuse()
                return
            try:
                body = self._read_body()
            except Exception:
                self.close_connection = True
                self._send(400, {"error": {"message": "invalid JSON"}}, False)
                return
            status, payload = bridge.handle("POST", self.path, body)
            stream = bool(payload.pop("_stream", False)) if isinstance(payload, dict) else False
            if stream:
                payload = payload.get("payload") or {}
            self._send(status, payload, stream)

    return Handler


def serve(bridge: Bridge, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    bind_host = host if host is not None else os.environ.get("BRIDGE_HOST", "127.0.0.1")
    bind_port = port if port is not None else int(os.environ.get("BRIDGE_PORT", "8788"))
    server = ThreadingHTTPServer((bind_host, bind_port), handler_factory(bridge))
    return server


def main() -> None:
    bridge = Bridge()
    server = serve(bridge)
    host, port = server.server_address[:2]
    print(f"bridge listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
