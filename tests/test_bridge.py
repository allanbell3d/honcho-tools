"""Bridge tests. Local HTTP stub and a Python CLI stub. No provider traffic."""
from __future__ import annotations

import json
import os
import sys
import socket
import threading
import time
import pytest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "bridge"))

import bridge_cli as bridge  # noqa: E402
from support import sample_settings, write_settings  # noqa: E402


@pytest.mark.parametrize("returned_model", ["provider-version", None, "stage-route", "@deriver"])
def test_routing_evidence_survives_response_and_stream(tmp_path, monkeypatch, returned_model):
    data = sample_settings()
    data["providers"]["stages"] = {"deriver": "openai"}
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", data), tmp_path / "calls.jsonl")
    seen = []

    def respond(body, backend, model, effort, timeout):
        seen.append(model)
        return {"model": returned_model, "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3,
                          "prompt_tokens_details": {"cached_tokens": 1}}}

    monkeypatch.setattr(app, "_openai", respond)
    for stream in (False, True):
        status, response = app.chat({"backend": "@deriver", "model": "stage-route", "stream": stream,
                                     "messages": [{"role": "user", "content": "private prompt"}]})
        assert status == 200
        payload = response["payload"] if stream else response
        assert payload["model"] == ("provider-version" if returned_model == "provider-version" else "gpt-test")
        usage = payload["usage"]
        assert usage["billing_route"] == "api"
        assert usage["backend"] == "openai"
        assert usage["resolved_model"] == "gpt-test"
        assert usage.get("upstream_model") == returned_model
        assert usage["prompt_tokens_details"] == {"cached_tokens": 1}
        chunks = [json.loads(line[6:]) for line in bridge.stream_bytes(payload).decode().splitlines()
                  if line.startswith("data: {")]
        assert chunks[-1]["usage"] == usage
        assert all(chunk["model"] == payload["model"] for chunk in chunks)
    assert seen == ["gpt-test", "gpt-test"]
    records = [json.loads(line) for line in app.log_file.read_text().splitlines()]
    assert all(record["requested_model"] == "stage-route" and record["resolved_model"] == "gpt-test" for record in records)
    health = app.health()
    assert health["routing"]["openai"]["calls"] == 2
    assert health["routing"]["openai"]["last_call"]["upstream_model"] == returned_model
    assert "private prompt" not in json.dumps(health)


def test_route_failure_has_no_fallback_and_log_failure_is_visible(tmp_path, monkeypatch):
    data = sample_settings()
    data["providers"]["stages"] = {"deriver": "openai"}
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", data), tmp_path / "calls.jsonl")
    attempted = []

    def fail(*args):
        attempted.append("openai")
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(app, "_openai", fail)
    monkeypatch.setattr(app, "_tool_free", lambda *args: pytest.fail("must not fall back to CLI"))
    assert app.chat({"backend": "@deriver", "model": "stage-route"})[0] == 502
    assert attempted == ["openai"]
    record = json.loads(app.log_file.read_text())
    assert record["backend"] == "openai" and record["resolved_model"] == "gpt-test"
    assert record["requested_model"] == "stage-route"
    assert app.health()["routing"]["openai"]["errors"] == 1
    app.log_file = tmp_path  # Opening a directory as a log fails on every platform.
    app.write_log({"safe": True})
    health = app.health()
    assert health["log_ok"] is False and health["log_write_errors"] == 1
    assert str(tmp_path) not in json.dumps(health)
    app.log_file = tmp_path / "recovered.jsonl"
    app.write_log({"recovered": True})
    health = app.health()
    assert health["log_ok"] is True and health["log_write_errors"] == 1
    assert json.loads(app.log_file.read_text()) == {"recovered": True}


def test_anthropic_cached_input_is_reported_in_the_openai_field():
    # Honcho's OpenAI usage parser reads prompt_tokens_details.cached_tokens; without it every cache read is billed
    # as fresh input. The streamed tail chunk carries the same usage object.
    usage = {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 4}
    payload = bridge.from_anthropic({"model": "claude-version", "content": [], "usage": usage}, "configured")
    assert payload["usage"]["prompt_tokens_details"] == {"cached_tokens": 10}
    assert payload["usage"]["prompt_tokens"] == 21 and payload["usage"]["cache_read_input_tokens"] == 10
    no_cache = bridge.from_anthropic({"model": "m", "content": [], "usage": {"input_tokens": 7, "output_tokens": 3}}, "configured")
    assert no_cache["usage"]["prompt_tokens_details"] == {"cached_tokens": 0}
    chunks = [json.loads(line[6:]) for line in bridge.stream_bytes(payload).decode("utf-8").split("\n\n") if line.startswith("data: {")]
    assert chunks[-1]["usage"]["prompt_tokens_details"] == {"cached_tokens": 10}


def test_anthropic_model_and_cache_usage_are_preserved():
    payload = bridge.from_anthropic({"model": "claude-version", "content": [],
                                    "usage": {"input_tokens": 1, "output_tokens": 2,
                                              "cache_read_input_tokens": 10, "cache_creation_input_tokens": 4}}, "configured")
    assert payload["model"] == "claude-version"
    assert payload["usage"]["cache_read_input_tokens"] == 10
    assert payload["usage"]["cache_creation_input_tokens"] == 4
    assert payload["usage"]["prompt_tokens"] == 15
    assert payload["usage"]["completion_tokens"] == 2
    assert payload["usage"]["total_tokens"] == 17

STUB = """import json, os, sys
record = os.environ.get("STUB_RECORD")
if record:
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv) + "\\n")
if "empty-model" in sys.argv:
    text = json.dumps({"tool_calls": [], "final": "  "})
elif "tool-model" in sys.argv:
    text = json.dumps({"tool_calls": [{"name": "lookup", "arguments_json": "{}"}], "final": "abcdef"})
else:
    text = json.dumps({"tool_calls": [], "final": "ok"})
if "-p" in sys.argv:
    print(json.dumps({"structured_output": json.loads(text), "usage": {"input_tokens": 1, "output_tokens": 2}}))
else:
    print(json.dumps({"item": {"type": "agent_message", "text": text}, "usage": {"input_tokens": 3, "output_tokens": 4}}))
"""


class _Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        self.server.seen.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("x-api-key"),
            "body": json.loads(raw.decode("utf-8")),
        })
        if self.path.endswith("/messages"):
            payload = {
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
            }
        else:
            payload = {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            }
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, fmt, *args):
        return


def _start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _settings(base: str) -> dict:
    data = sample_settings()
    for name in ("openai", "deepseek", "openrouter"):
        # The stub host says nothing about the provider, so the style is explicit.
        data["providers"]["backends"][name]["base_url"] = base + "/v1"
        data["providers"]["backends"][name]["effort_style"] = name
    return data


def test_routing_precedence():
    data = sample_settings()
    name, _backend, model = bridge.select_route({"backend": "openrouter:or-listed"}, data)[:3]
    assert (name, model) == ("openrouter", "or-listed")
    name, _backend, model = bridge.select_route({"extra_body": {"backend": "claude"}}, data)[:3]
    assert (name, model) == ("claude", "claude-model")
    data["defaults"]["backend"] = "openai"
    name, _backend, model = bridge.select_route({}, data)[:3]
    assert name == "openai"
    data["defaults"]["backend"] = None
    name, _backend, _model = bridge.select_route({}, data)[:3]
    assert name == "codex"
    body = {"backend": "deepseek"}
    extra = {"extra_body": {"backend": "claude"}}
    assert bridge.request_backend_ref({**body, **extra}) == "deepseek"


LEVEL_IN = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Expected level per provider for none, minimal, low, medium, high, xhigh, max (None: nothing sent).
LEVEL_TABLE = {
    "anthropic": ("none", "low", "low", "medium", "high", "xhigh", "max"),
    "claude": ("low", "low", "low", "medium", "high", "xhigh", "max"),  # no off level on the command line
    "codex": ("low", "low", "low", "medium", "high", "xhigh", "max"),
    "openai": ("none", "low", "low", "medium", "high", "xhigh", "max"),
    "deepseek": ("none", "low", "low", "high", "high", "high", "max"),
    "glm": (None, "low", "low", "high", "high", "high", "max"),  # glm-5.3 thinking cannot be disabled
    "glm-old": (None,) * 7,
    "openrouter": LEVEL_IN,
    "generic": LEVEL_IN,
    "none": (None,) * 7,
}
STYLE_BACKENDS = {
    "anthropic": {"kind": "anthropic-api", "model": "m", "key_env": "K"},
    "claude": {"kind": "cli-claude", "model": "m", "effort": "high"},
    "codex": {"kind": "cli-codex", "model": "m", "effort": "high"},
    "openai": {"kind": "openai-compat", "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna"},
    "deepseek": {"kind": "openai-compat", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "glm": {"kind": "openai-compat", "base_url": "https://api.z.ai/api/paas/v4", "model": "glm-5.3"},
    "glm-old": {"kind": "openai-compat", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.5-air"},
    "openrouter": {"kind": "openai-compat", "base_url": "https://openrouter.ai/api/v1", "model": "a/b"},
    "generic": {"kind": "openai-compat", "base_url": "http://proxy.example:4000/v1", "model": "m"},
    "none": {"kind": "openai-compat", "base_url": "https://api.openai.com/v1", "model": "m", "effort_style": "none"},
}


def _wire(style: str, level: str | None) -> dict:
    if level is None or style in ("claude", "codex"):
        return {}
    if style == "anthropic":
        return {"thinking": {"type": "disabled"}} if level == "none" else {"output_config": {"effort": level}}
    if style == "openrouter":
        return {"reasoning": {"effort": level}}
    if style == "deepseek":
        return {"reasoning_effort": "none"} if level == "none" else {"thinking": {"type": "enabled"}, "reasoning_effort": level}
    return {"reasoning_effort": level}


@pytest.mark.parametrize("style", sorted(LEVEL_TABLE))
def test_effort_level_and_shape_per_provider(style):
    backend = STYLE_BACKENDS[style]
    for asked, expected in zip(LEVEL_IN, LEVEL_TABLE[style]):
        assert bridge.provider_effort(backend, asked) == expected, (style, asked)
        assert bridge.effort_wire(backend, asked) == _wire(style, expected), (style, asked)
    assert bridge.effort_wire(backend, None) == {}


def test_backend_default_effort_does_not_change_the_shape():
    for key in ("thinking_effort", "effort"):
        openai = {"kind": "openai-compat", "base_url": "https://api.openai.com/v1", "model": "gpt-6-luna", key: "low", "models": ["x"]}
        assert bridge.effort_style(openai) == "openai"
        assert bridge.effort_wire(openai, bridge.choose_effort({}, openai)) == {"reasoning_effort": "low"}
    body = {"reasoning_effort": "low", "output_config": {"effort": "high"}}
    assert bridge.choose_effort(body, {"effort": "max", "thinking_effort": "medium"}) == "low"
    assert bridge.choose_effort({}, {"effort": "xhigh"}) == "xhigh"
    assert bridge.choose_effort({}, {"thinking_effort": "max"}) == "max"


def test_effort_style_explicit_then_host_then_generic():
    hosts = {
        "https://openrouter.ai/api/v1": "openrouter",
        "https://api.deepseek.com/v1": "deepseek",
        "https://api.z.ai/api/paas/v4": "glm",
        "https://open.bigmodel.cn/api/paas/v4": "glm",
        "https://api.openai.com/v1": "openai",
        "https://notopenrouter.ai/v1": "generic",
        "https://llm.example/v1": "generic",
    }
    for url, style in hosts.items():
        assert bridge.effort_style({"kind": "openai-compat", "base_url": url, "models": ["m"]}) == style, url
    explicit = {"kind": "openai-compat", "base_url": "https://openrouter.ai/api/v1", "effort_style": "deepseek"}
    assert bridge.effort_style(explicit) == "deepseek"
    with pytest.raises(ValueError):
        bridge.effort_style({"kind": "openai-compat", "base_url": "https://x.example", "effort_style": "grok"})
    with pytest.raises(ValueError):
        bridge.effort_style({"kind": "anthropic-api", "effort_style": "openai"})
    assert bridge.effort_style({"kind": "anthropic-api", "effort_style": "none"}) == "none"
    assert bridge.effort_style({"kind": "cli-codex", "effort_style": "none"}) == "codex"
    for model, has in (("glm-5.2", True), ("GLM-5.3-Flash", True), ("glm-6", True), ("glm-5", False),
                       ("glm-4.7", False), ("glm-4.5-air", False), ("custom", False)):
        assert bridge.glm_has_effort(model) is has, model
    glm = {"kind": "openai-compat", "base_url": "https://api.z.ai/api/paas/v4", "model": "glm-4.5-air"}
    assert bridge.effort_wire(glm, "max", "glm-5.3") == {"reasoning_effort": "max"}


def test_effort_none_switches_thinking_off_the_way_each_provider_documents():
    deepseek, glm, openai = (STYLE_BACKENDS[name] for name in ("deepseek", "glm", "openai"))
    # DeepSeek: reasoning_effort none alone; thinking enabled would turn it back on.
    assert bridge.effort_wire(deepseek, "none") == {"reasoning_effort": "none"}
    assert bridge.effort_wire(deepseek, "high") == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    assert bridge.effort_wire(openai, "none") == {"reasoning_effort": "none"}
    assert bridge.effort_wire(STYLE_BACKENDS["generic"], "none") == {"reasoning_effort": "none"}
    assert bridge.effort_wire(STYLE_BACKENDS["openrouter"], "none") == {"reasoning": {"effort": "none"}}
    # GLM: thinking disabled from GLM-4.6, never reasoning_effort; the GLM-5.3 series cannot be switched off.
    for model, off in (("glm-4.6", True), ("glm-4.7", True), ("glm-5", True), ("glm-5.2", True), ("GLM-5.3", False),
                       ("glm-5.3-flash", False), ("glm-5.4", True), ("glm-4.5-air", False), ("custom", False)):
        assert bridge.glm_can_disable(model) is off, model
        assert bridge.effort_wire(glm, "none", model) == ({"thinking": {"type": "disabled"}} if off else {}), model
        assert bridge.provider_effort(glm, "none", model) == ("none" if off else None), model
    # Anthropic over the OpenAI-compatible endpoint ignores reasoning_effort, so the old shape stays.
    compat = {"kind": "openai-compat", "base_url": "https://api.anthropic.com/v1", "model": "m", "effort_style": "anthropic"}
    assert bridge.effort_wire(compat, "none") == {"reasoning_effort": "none"}
    # A CLI has no off level: none is low, on the command line.
    for style in ("claude", "codex"):
        assert bridge.provider_effort(STYLE_BACKENDS[style], "none") == "low"
    assert bridge.choose_effort({"reasoning_effort": "none"}, {"effort": "high"}) == "none"
    assert bridge.choose_effort({}, {"thinking_effort": "none"}) == "none"
    assert bridge.choose_effort({}, {"effort": "high"}, "none") == "none"
    assert bridge.request_effort({"extra_body": {"output_config": {"effort": "none"}}}) == "none"
    with pytest.raises(ValueError, match="none, minimal"):
        bridge.request_effort({"reasoning_effort": "off"})


SCHEMA_FORMAT = {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}}


@pytest.mark.parametrize("style,converted", [("deepseek", True), ("glm", True), ("openai", False), ("openrouter", False), ("generic", False)])
def test_json_schema_becomes_json_object_where_only_that_is_documented(style, converted):
    messages = [{"role": "system", "content": "Base."}, {"role": "user", "content": "hi"}]
    body = {"messages": messages, "response_format": SCHEMA_FORMAT, "max_tokens": 50}
    payload = bridge.openai_payload(body, "m", {}, style)
    if converted:
        assert payload["response_format"] == {"type": "json_object"}
        system = payload["messages"][0]["content"]
        assert system.startswith("Base.\n\n") and "json" in system and '"ok"' in system
        assert messages[0]["content"] == "Base."  # the request itself is not changed
    else:
        assert payload["response_format"] == SCHEMA_FORMAT and payload["messages"] == messages
    if style == "openai":
        assert "max_tokens" not in payload and payload["max_completion_tokens"] == 50
    else:
        assert payload["max_tokens"] == 50 and "max_completion_tokens" not in payload


@pytest.mark.parametrize("kind,style", [
    ("openai-compat", "openai"),
    ("openai-compat", "deepseek"),
    ("openai-compat", "glm"),
    ("openai-compat", "openrouter"),
    ("openai-compat", "anthropic"),
    ("openai-compat", "none"),
    ("anthropic-api", None),
    ("cli-codex", None),
    ("cli-claude", None),
])
def test_json_request_reaches_the_provider(kind, style, tmp_path, monkeypatch):
    # One row per kind (openai-compat once per effort style). json_schema and json_object
    # must arrive in the form that provider documents.
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    formats = {
        "json_schema": {"type": "json_schema", "json_schema": {"name": "r", "schema": schema}},
        "json_object": {"type": "json_object"},
    }
    backend = {"kind": kind, "model": "m"}
    if kind == "openai-compat":
        backend.update(base_url="http://127.0.0.1:9/v1", key_env="TEST_PROVIDER_KEY", effort_style=style)
    elif kind == "anthropic-api":
        backend["key_env"] = "TEST_PROVIDER_KEY"
    data = {"providers": {"active": "b", "backends": {"b": backend}}}
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", data), tmp_path / "calls.jsonl")
    monkeypatch.setenv("TEST_PROVIDER_KEY", "test-key")
    seen = {}

    def post(url, headers, payload, timeout):
        seen["payload"] = payload
        if kind == "anthropic-api":
            return {"content": [{"type": "text", "text": "{}"}], "usage": {"input_tokens": 1, "output_tokens": 1}}
        return {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    def run(argv, stdin_text, timeout, cwd):
        seen["argv"] = list(argv)
        if "--output-schema" in argv:
            seen["schema"] = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8"))
        if "--json-schema" in argv:
            seen["schema"] = json.loads(argv[argv.index("--json-schema") + 1])
        if "-p" in argv:
            return json.dumps({"structured_output": {}, "usage": {"input_tokens": 1, "output_tokens": 1}})
        return json.dumps({"item": {"type": "agent_message", "text": "{}"}})

    monkeypatch.setattr(bridge, "post_json", post)
    monkeypatch.setattr(bridge, "run_process", run)
    # The CLI kinds check for the executable; the fake run_process stands in for it, so the check is stubbed too.
    monkeypatch.setattr(bridge.shutil, "which", lambda name, *args, **kwargs: "/usr/local/bin/" + str(name))
    messages = [{"role": "system", "content": "Base."}, {"role": "user", "content": "hi"}]
    object_schema = {"type": "object", "additionalProperties": True}
    for mode, response_format in formats.items():
        seen.clear()
        status, payload = app.chat({"backend": "b", "messages": messages, "response_format": response_format})
        assert status == 200, (kind, style, mode, payload)
        if kind == "openai-compat":
            body = seen["payload"]
            if style in ("deepseek", "glm") and mode == "json_schema":
                assert body["response_format"] == {"type": "json_object"}
                assert body["messages"][0]["content"].startswith("Base.\n\n") and '"ok"' in body["messages"][0]["content"]
            else:
                assert body["response_format"] == response_format
                assert body["messages"] == messages
        elif kind == "anthropic-api":
            body = seen["payload"]
            system = body["system"][0]["text"]
            assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
            if mode == "json_schema":
                assert body["output_config"]["format"] == {"type": "json_schema", "schema": schema}
                assert '"ok"' not in system
            else:
                assert "format" not in (body.get("output_config") or {})
                assert system.startswith("Base.\n\n") and "single JSON object" in system
        else:
            assert seen["schema"] == (schema if mode == "json_schema" else object_schema)


def test_schema_instruction_without_a_system_message_or_with_parts():
    payload = bridge.openai_payload({"messages": [{"role": "user", "content": "hi"}], "response_format": SCHEMA_FORMAT,
                                     "tools": [{"type": "function", "function": {"name": "t"}}]}, "m", {}, "deepseek")
    assert payload["messages"][0]["role"] == "system" and payload["messages"][0]["content"].startswith("If you are not calling a tool")
    parts = [{"role": "developer", "content": [{"type": "text", "text": "A"}]}]
    payload = bridge.openai_payload({"messages": parts, "response_format": SCHEMA_FORMAT}, "m", {}, "glm")
    assert payload["messages"][0]["content"][0] == {"type": "text", "text": "A"} and len(payload["messages"][0]["content"]) == 2
    payload = bridge.openai_payload({"messages": [], "max_tokens": 9, "max_completion_tokens": 7}, "m", {}, "openai")
    assert payload["max_completion_tokens"] == 7 and "max_tokens" not in payload


def test_model_list_includes_openrouter_models():
    listing = bridge.model_list(sample_settings())
    ids = [item["id"] for item in listing["data"]]
    assert "openrouter" in ids
    assert "openrouter:or-listed" in ids
    assert "grok" not in ids


def test_http_kinds_and_call_log(tmp_path, monkeypatch):
    server = _start_stub()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        path = write_settings(tmp_path / "settings.json", _settings(base))
        monkeypatch.setenv("ANTHROPIC_API_URL", base + "/v1/messages")
        monkeypatch.setenv("TEST_OPENAI_KEY", "test-key")
        monkeypatch.setenv("TEST_DEEPSEEK_KEY", "test-key")
        monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
        monkeypatch.setenv("TEST_ANTHROPIC_KEY", "test-key")
        monkeypatch.setenv("BRIDGE_LOG", str(tmp_path / "calls.jsonl"))
        app = bridge.Bridge(path, tmp_path / "calls.jsonl")
        messages = [{"role": "user", "content": "hello"}]

        status, payload = app.chat({"backend": "openai", "messages": messages, "reasoning_effort": "max", "observer": "a", "observed": "b", "output_config": {"effort": "low"}, "max_tokens": 64})
        assert status == 200
        assert payload["choices"][0]["message"]["content"] == "hi"
        forwarded = server.seen[-1]
        assert forwarded["authorization"] == "Bearer test-key"
        assert forwarded["body"]["reasoning_effort"] == "max"
        assert forwarded["body"]["model"] == "gpt-test"
        assert forwarded["body"]["max_completion_tokens"] == 64 and "max_tokens" not in forwarded["body"]
        assert "backend" not in forwarded["body"]
        assert "observer" not in forwarded["body"]
        assert "observed" not in forwarded["body"]
        assert "output_config" not in forwarded["body"]
        assert "extra_body" not in forwarded["body"]

        status, _payload = app.chat({"backend": "deepseek", "messages": messages, "response_format": SCHEMA_FORMAT})
        assert status == 200
        assert server.seen[-1]["body"]["thinking"] == {"type": "enabled"}
        assert server.seen[-1]["body"]["reasoning_effort"] == "high"  # medium is high on DeepSeek
        assert server.seen[-1]["body"]["response_format"] == {"type": "json_object"}
        assert server.seen[-1]["body"]["messages"][0]["role"] == "system"

        status, _payload = app.chat({"backend": "openrouter:or-listed", "messages": messages, "reasoning_effort": "max"})
        assert status == 200
        assert server.seen[-1]["body"]["model"] == "or-listed"
        assert server.seen[-1]["body"]["reasoning"] == {"effort": "max"}

        status, payload = app.chat({"backend": "anthropic", "messages": messages, "reasoning_effort": "max"})
        assert status == 200
        assert payload["choices"][0]["message"]["content"] == "hi"
        assert server.seen[-1]["x_api_key"] == "test-key"
        assert server.seen[-1]["body"]["output_config"] == {"effort": "max"}
        assert server.seen[-1]["path"].endswith("/messages")

        lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        assert [record["backend"] for record in records] == ["openai", "deepseek", "openrouter", "anthropic"]
        assert [record["sent_effort"] for record in records] == ["max", "high", "max", "max"]
        assert all("test-key" not in line for line in lines)

        # none: DeepSeek gets reasoning_effort alone, Anthropic thinking disabled and no effort.
        status, _payload = app.chat({"backend": "deepseek", "messages": messages, "reasoning_effort": "none"})
        assert status == 200
        sent = server.seen[-1]["body"]
        assert sent["reasoning_effort"] == "none" and "thinking" not in sent
        status, _payload = app.chat({"backend": "anthropic", "messages": messages, "reasoning_effort": "none"})
        assert status == 200
        sent = server.seen[-1]["body"]
        assert sent["thinking"] == {"type": "disabled"} and "output_config" not in sent

        data = _settings(base)
        data["providers"]["backends"]["openai"]["model"] = "gpt-next"
        write_settings(path, data)
        os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2)
        status, _payload = app.chat({"backend": "openai", "messages": messages, "reasoning_effort": "low"})
        assert status == 200
        assert server.seen[-1]["body"]["model"] == "gpt-next"

        status, listing = app.handle("GET", "/v1/models", None)
        assert status == 200
        assert any(item["id"] == "openrouter:or-listed" for item in listing["data"])
        status, health = app.handle("GET", "/health", None)
        assert status == 200 and health["ok"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_cli_stubs_and_bridge_http(tmp_path, monkeypatch):
    stub = tmp_path / "cli_stub.py"
    stub.write_text(STUB, encoding="utf-8")
    record = tmp_path / "argv.jsonl"
    monkeypatch.setenv("BRIDGE_EXE", json.dumps([sys.executable, str(stub)]))
    monkeypatch.setenv("BRIDGE_CLAUDE", json.dumps([sys.executable, str(stub)]))
    monkeypatch.setenv("BRIDGE_CWD", str(tmp_path / "cwd"))
    monkeypatch.setenv("STUB_RECORD", str(record))
    path = write_settings(tmp_path / "settings.json", sample_settings())
    app = bridge.Bridge(path, tmp_path / "calls.jsonl")
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    messages = [{"role": "user", "content": "hello"}]

    status, payload = app.chat({"backend": "codex", "messages": messages, "tools": tools, "reasoning_effort": "max"})
    assert status == 200
    assert payload["choices"][0]["message"]["content"] == "ok"
    status, payload = app.chat({"backend": "claude", "messages": messages, "tools": tools, "reasoning_effort": "max"})
    assert status == 200
    assert payload["choices"][0]["message"]["content"] == "ok"
    argv_lines = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    codex_argv = argv_lines[0]
    claude_argv = argv_lines[1]
    assert "exec" in codex_argv
    assert 'model_reasoning_effort="max"' in codex_argv
    assert "-p" in claude_argv
    assert "--effort" in claude_argv
    assert claude_argv[claude_argv.index("--effort") + 1] == "max"

    status, payload = app.chat({"backend": "codex", "messages": messages, "reasoning_effort": "low"})
    assert status == 200
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["choices"][0]["message"]["content"] == "ok"

    data = sample_settings()
    data["providers"]["backends"]["codex"]["model"] = "empty-model"
    write_settings(path, data)
    os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2)
    status, payload = app.chat({"backend": "codex", "messages": messages, "tools": tools})
    assert status == 422  # the same request would get an empty answer again: not resent
    assert "empty answer" in payload["error"]["message"]

    http = bridge.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        port = http.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            assert response.status == 200
        body = json.dumps({"backend": "claude", "messages": messages, "tools": tools, "reasoning_effort": "high"}).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            answered = json.loads(response.read().decode("utf-8"))
        assert answered["choices"][0]["message"]["content"] == "ok"
        stream_body = json.dumps({
            "backend": "claude",
            "messages": messages,
            "tools": tools,
            "reasoning_effort": "high",
            "stream": True,
        }).encode("utf-8")
        stream_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=stream_body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(stream_request, timeout=10) as response:
            streamed = response.read().decode("utf-8")
            assert response.headers["Content-Type"].startswith("text/event-stream")
        assert "data: [DONE]" in streamed
        assert '"content": "ok"' in streamed
    finally:
        http.shutdown()
        http.server_close()

    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines if line.strip()]
    assert any(record.get("backend") == "codex" and "error" not in record for record in parsed)
    assert any(record.get("backend") == "claude" for record in parsed)


def test_disabled_backend_and_unknown_model(tmp_path):
    data = sample_settings()
    data["providers"]["backends"]["openai"]["enabled"] = False
    path = write_settings(tmp_path / "settings.json", data)
    app = bridge.Bridge(path, tmp_path / "calls.jsonl")
    status, payload = app.chat({"backend": "openai", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 400
    assert payload["error"]["message"] == "backend openai is disabled"
    status, payload = app.chat({"backend": "openrouter:not-listed", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 400
    assert payload["error"]["message"] == "unknown model not-listed for backend openrouter"
    name, _backend, model = bridge.select_route({"backend": "deepseek:other-model"}, sample_settings())[:3]
    assert (name, model) == ("deepseek", "other-model")


def test_in_flight_call_keeps_its_backend(tmp_path, monkeypatch):
    server = _start_stub()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        path = write_settings(tmp_path / "settings.json", _settings(base))
        monkeypatch.setenv("TEST_OPENAI_KEY", "test-key")
        app = bridge.Bridge(path, tmp_path / "calls.jsonl")
        seen = []
        real = app._openai

        def wrapped(body, backend, model, effort, timeout):
            changed = json.loads(path.read_text(encoding="utf-8"))
            changed["providers"]["backends"]["openai"]["model"] = "gpt-later"
            write_settings(path, changed)
            os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2)
            app.store.get()
            seen.append(model)
            return real(body, backend, model, effort, timeout)

        app._openai = wrapped
        status, _payload = app.chat({
            "backend": "openai",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "low",
        })
        assert status == 200
        assert seen == ["gpt-test"]
        assert server.seen[-1]["body"]["model"] == "gpt-test"
    finally:
        server.shutdown()
        server.server_close()


def test_cli_log_counts_the_structured_final(tmp_path, monkeypatch):
    stub = tmp_path / "cli_stub.py"
    stub.write_text(STUB, encoding="utf-8")
    monkeypatch.setenv("BRIDGE_EXE", json.dumps([sys.executable, str(stub)]))
    monkeypatch.setenv("BRIDGE_CWD", str(tmp_path / "cwd"))
    data = sample_settings()
    data["providers"]["backends"]["codex"]["model"] = "tool-model"
    path = write_settings(tmp_path / "settings.json", data)
    app = bridge.Bridge(path, tmp_path / "calls.jsonl")
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    status, payload = app.chat({
        "backend": "codex",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": tools,
    })
    assert status == 200
    assert payload["choices"][0]["message"]["content"] is None
    assert "_final_chars" not in payload
    record = json.loads((tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert record["final_chars"] == 6
    assert record["tool_calls"] == ["lookup"]
    assert record["backend"] == "codex"
    assert record["billing_route"] == "cli"
    assert payload["usage"]["billing_route"] == "cli"


def test_missing_cli_executable_answers_422(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_EXE", "codex-not-installed")
    monkeypatch.setenv("BRIDGE_CLAUDE", "claude-not-installed")
    data = sample_settings()
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", data), tmp_path / "calls.jsonl")
    messages = [{"role": "user", "content": "fixture"}]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    status, payload = app.chat({"backend": "codex", "messages": messages, "tools": tools})
    assert status == 422
    assert payload["error"]["message"] == "cli-codex CLI not installed in this image (INSTALL_CODEX=1 at build)"
    status, payload = app.chat({"backend": "claude", "messages": messages})
    assert status == 422
    assert payload["error"]["message"] == "cli-claude CLI not installed in this image (INSTALL_CLAUDE=1 at build)"


def test_stage_alias_reloads_and_keeps_last_good(tmp_path):
    data = sample_settings()
    data["providers"]["stages"] = {"deriver": "codex:stage-one", "summary": "codex:stage-two"}
    path = write_settings(tmp_path / "settings.json", data)
    app = bridge.Bridge(path, tmp_path / "calls.jsonl")
    assert bridge.select_route({"backend": "@deriver"}, app.store.get())[2] == "stage-one"
    assert bridge.select_route({"extra_body": {"backend": "@summary"}}, app.store.get())[2] == "stage-two"
    data["providers"]["stages"]["deriver"] = "codex:changed"
    write_settings(path, data)
    os.utime(path, ns=(path.stat().st_mtime_ns + 2_000_000_000,) * 2)
    assert bridge.select_route({"backend": "@deriver"}, app.store.get())[2] == "changed"
    try:
        bridge.select_route({"backend": "@missing"}, app.store.get())
    except ValueError as exc:
        assert "@missing" in str(exc)
    else:
        assert False, "missing alias accepted"
    path.write_text("{bad", encoding="utf-8")
    os.utime(path, ns=(path.stat().st_mtime_ns + 4_000_000_000,) * 2)
    assert bridge.select_route({"backend": "@deriver"}, app.store.get())[2] == "changed"
    assert app.health()["settings_ok"] is False
    assert app.health()["settings_error"]
    write_settings(path, data)
    os.utime(path, ns=(path.stat().st_mtime_ns + 6_000_000_000,) * 2)
    assert app.health()["settings_ok"] is True



def test_next_stage_call_uses_changed_alias_and_logs_backend(tmp_path, monkeypatch):
    server = _start_stub()
    try:
        data = _settings(f"http://127.0.0.1:{server.server_address[1]}")
        data["providers"]["stages"] = {"deriver": "openai", "summary": "openai"}
        path = write_settings(tmp_path / "settings.json", data)
        monkeypatch.setenv("TEST_OPENAI_KEY", "test-key")
        monkeypatch.setenv("TEST_DEEPSEEK_KEY", "test-key")
        app = bridge.Bridge(path, tmp_path / "calls.jsonl")
        for stage in ("deriver", "summary"):
            assert app.chat({"backend": "@" + stage, "messages": [{"role": "user", "content": "fixture"}]})[0] == 200
        data["providers"]["stages"]["deriver"] = "deepseek"
        write_settings(path, data)
        os.utime(path, ns=(path.stat().st_mtime_ns + 2_000_000_000,) * 2)
        assert app.chat({"backend": "@deriver", "messages": [{"role": "user", "content": "fixture"}]})[0] == 200
        records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert [record["backend"] for record in records] == ["openai", "openai", "deepseek"]
        assert all(record["billing_route"] == "api" for record in records)
    finally:
        server.shutdown()
        server.server_close()


def test_stage_object_effort_beats_backend_default_and_request_beats_both(tmp_path, monkeypatch):
    """Stage effort sits between the request and the backend default. A string stage stays a string."""
    data = sample_settings()
    data["providers"]["backends"]["openai"]["thinking_effort"] = "low"
    data["providers"]["backends"]["openai"]["models"] = ["gpt-test", "gpt-other"]
    data["providers"]["stages"] = {
        "deriver": {"model": "openai:gpt-other", "effort": "high"},
        "summary": "openai:gpt-other",
    }
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", data), tmp_path / "calls.jsonl")
    seen = []

    def respond(body, backend, model, effort, timeout):
        seen.append((model, effort))
        return {"model": model, "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(app, "_openai", respond)
    messages = [{"role": "user", "content": "fixture"}]
    assert app.chat({"backend": "@deriver", "messages": messages})[0] == 200
    assert app.chat({"backend": "@deriver", "messages": messages, "reasoning_effort": "max"})[0] == 200
    assert app.chat({"backend": "@summary", "messages": messages})[0] == 200
    assert app.chat({"backend": "openai", "messages": messages})[0] == 200
    assert app.chat({"backend": "openai", "messages": messages, "reasoning_effort": "medium"})[0] == 200
    assert seen == [
        ("gpt-other", "high"),
        ("gpt-other", "max"),
        ("gpt-other", "low"),
        ("gpt-test", "low"),
        ("gpt-test", "medium"),
    ]


TOOL_FREE_STUB = r"""import json, os, sys, time
from pathlib import Path
sys.stdin.read()
mode = os.environ.get("TOOL_FREE_MODE", "json")
record = Path(os.environ["TOOL_FREE_RECORD"])
attempt = int(record.read_text()) + 1 if record.exists() else 1
record.write_text(str(attempt))
if mode == "timeout":
    time.sleep(3)
if mode == "slowrepair":
    time.sleep(0.6)
if mode == "unsupported":
    sys.stderr.write("unsupported output schema")
    sys.exit(1)
schema = json.loads(Path(sys.argv[sys.argv.index("--output-schema")+1]).read_text()) if "--output-schema" in sys.argv else json.loads(sys.argv[sys.argv.index("--json-schema")+1])
Path(str(record) + ".schema").write_text(json.dumps(schema))
invalid = mode == "bad" or mode in ("repair", "slowrepair") and attempt == 1
value = {"final": "plain answer"} if mode == "plain" else {"ok": True}
text = "{bad" if invalid else json.dumps(value)
if "-p" in sys.argv:
    print(json.dumps({"result": text, "usage": {"input_tokens": 3, "output_tokens": 4}}))
else:
    print(json.dumps({"item": {"type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}}))
"""


def _tool_free_bridge(tmp_path, monkeypatch, kind, mode):
    stub = tmp_path / "fake_tool_free.py"
    stub.write_text(TOOL_FREE_STUB, encoding="utf-8")
    monkeypatch.setenv("BRIDGE_EXE", json.dumps([sys.executable, str(stub)]))
    monkeypatch.setenv("BRIDGE_CLAUDE", json.dumps([sys.executable, str(stub)]))
    monkeypatch.setenv("BRIDGE_CWD", str(tmp_path / "cwd"))
    monkeypatch.setenv("TOOL_FREE_MODE", mode)
    record = tmp_path / "attempts"
    monkeypatch.setenv("TOOL_FREE_RECORD", str(record))
    data = sample_settings()
    data["providers"]["backends"]["codex"]["kind"] = kind
    data["providers"]["timeout_seconds"] = 1 if mode in ("timeout", "slowrepair") else 10
    return bridge.Bridge(write_settings(tmp_path / "settings.json", data), tmp_path / "calls.jsonl"), record


def test_tool_free_codex_and_claude_json_and_plain(tmp_path, monkeypatch):
    for kind in ("cli-codex", "cli-claude"):
        for mode in ("plain", "json", "repair"):
            case = tmp_path / (kind + mode)
            case.mkdir()
            app, record = _tool_free_bridge(case, monkeypatch, kind, mode)
            body = {"backend": "codex", "messages": [{"role": "user", "content": "fixture"}], "max_tokens": 50}
            if mode != "plain":
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": "fixture", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}}
            status, payload = app.chat(body)
            assert status == 200, payload
            assert payload["choices"][0]["finish_reason"] == "stop"
            content = payload["choices"][0]["message"]["content"]
            assert content == "plain answer" if mode == "plain" else json.loads(content) == {"ok": True}
            attempts = 2 if mode == "repair" else 1
            assert record.read_text() == str(attempts)
            native = json.loads(Path(str(record) + ".schema").read_text())
            assert native == (body["response_format"]["json_schema"]["schema"] if mode != "plain" else {"type": "object", "additionalProperties": False, "properties": {"final": {"type": "string"}}, "required": ["final"]})
            assert payload["usage"]["prompt_tokens"] == 3 * attempts
            assert payload["usage"]["completion_tokens"] == 4 * attempts


def test_tool_free_bad_json_retries_once_and_timeout_is_bounded(tmp_path, monkeypatch):
    for mode in ("bad", "timeout"):
        case = tmp_path / mode
        case.mkdir()
        app, record = _tool_free_bridge(case, monkeypatch, "cli-codex", mode)
        status, payload = app.chat({"backend": "codex", "messages": [{"role": "user", "content": "fixture"}], "json_mode": True})
        assert status == 422
        assert ("valid JSON" if mode == "bad" else "timed out") in payload["error"]["message"]
        assert record.read_text() == ("2" if mode == "bad" else "1")



def test_tool_free_json_object_native_schema_and_unsupported_failure(tmp_path, monkeypatch):
    for mode in ("object", "unsupported", "slowrepair"):
        case = tmp_path / mode
        case.mkdir()
        app, record = _tool_free_bridge(case, monkeypatch, "cli-codex", mode)
        status, payload = app.chat({"backend": "codex", "messages": [{"role": "user", "content": "fixture"}], "response_format": {"type": "json_object"}})
        if mode == "object":
            assert status == 200
            assert json.loads(payload["choices"][0]["message"]["content"]) == {"ok": True}
            assert json.loads(Path(str(record) + ".schema").read_text()) == {"type": "object", "additionalProperties": True}
        else:
            # A CLI error may pass on a retry (502); a timeout would only time out again (422).
            assert status == (502 if mode == "unsupported" else 422)
            assert ("unsupported output schema" if mode == "unsupported" else "timed out") in payload["error"]["message"]
            assert record.read_text() == ("1" if mode == "unsupported" else "2")


def test_cli_processes_get_no_keys(monkeypatch, tmp_path):
    # The CLI uses its own login; keys from Honcho's .env must not reach it.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("HONCHO_API_TOKEN", "test-token")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-password")
    monkeypatch.setenv("DB_CONNECTION_URI", "postgresql://u:p@db/x")
    monkeypatch.setenv("TZ", "UTC")
    seen = {}

    class Done:
        returncode = 0
        pid = 0

        def __init__(self, argv, **kwargs):
            seen.update(kwargs["env"])

        def communicate(self, data=None, timeout=None):
            return b"ok", b""

    monkeypatch.setattr(bridge.subprocess, "Popen", Done)
    assert bridge.run_process(["cli"], "hi", 5, tmp_path) == "ok"
    assert seen.get("TZ") == "UTC" and "PATH" in seen
    for name in ("ANTHROPIC_API_KEY", "HONCHO_API_TOKEN", "POSTGRES_PASSWORD", "DB_CONNECTION_URI"):
        assert name not in seen


def test_a_cli_timeout_stops_the_whole_process_group(monkeypatch, tmp_path):
    killed = []

    class Slow:
        returncode = None
        pid = 4242

        def __init__(self, argv, **kwargs):
            self.kwargs = kwargs

        def communicate(self, data=None, timeout=None):
            if timeout is not None:
                raise bridge.subprocess.TimeoutExpired("cli", timeout)
            return b"", b""

        def kill(self):
            killed.append("child")

    monkeypatch.setattr(bridge.subprocess, "Popen", Slow)
    monkeypatch.setattr(bridge.os, "killpg", lambda pid, sig: killed.append(("group", pid)), raising=False)
    with pytest.raises(bridge.FinalError, match="timed out"):
        bridge.run_process(["cli"], "hi", 1, tmp_path)
    assert "child" in killed
    if bridge.os.name != "nt":
        assert ("group", 4242) in killed


def test_call_timeout_stays_below_the_client_timeout(monkeypatch):
    # Honcho's OpenAI client resends after 600 s; the bridge must answer first.
    data = sample_settings()
    data["providers"]["timeout_seconds"] = 900
    assert bridge.limits(data)[1] == bridge.MAX_CALL_SECONDS < 600
    data["providers"]["timeout_seconds"] = 120
    assert bridge.limits(data)[1] == 120
    del data["providers"]["timeout_seconds"]
    monkeypatch.delenv("BRIDGE_TIMEOUT", raising=False)
    assert bridge.limits(data)[1] == bridge.MAX_CALL_SECONDS


def test_a_request_that_cannot_get_a_slot_within_its_budget_is_not_sent(tmp_path, monkeypatch):
    # The budget starts when the request arrives. Time spent waiting for a slot counts, and a
    # request that never gets one answers 422 without calling the provider.
    data = sample_settings()
    data["providers"]["concurrency"] = 1
    data["providers"]["timeout_seconds"] = 1
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", data), tmp_path / "calls.jsonl")
    holding = threading.Event()
    release = threading.Event()
    calls = []

    def hold(body, backend, model, effort, timeout):
        calls.append(timeout)
        holding.set()
        assert release.wait(5)
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(app, "_openai", hold)
    first = threading.Thread(target=lambda: app.chat({"backend": "openai", "messages": []}))
    first.start()
    assert holding.wait(5)
    status, payload = app.chat({"backend": "openai", "messages": []})
    release.set()
    first.join(5)
    assert status == 422 and isinstance(payload.get("error"), dict)
    assert calls and len(calls) == 1


def test_key_env_must_name_a_provider_key(tmp_path, monkeypatch):
    data = sample_settings()
    data["providers"]["backends"]["deepseek"]["key_env"] = "POSTGRES_PASSWORD"
    path = write_settings(tmp_path / "settings.json", data)
    app = bridge.Bridge(path, tmp_path / "calls.jsonl")
    called = []

    def respond(*_args):
        called.append(True)
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(app, "_openai", respond)
    status, _payload = app.chat({"backend": "deepseek", "messages": []})
    assert status == 400 and not called
    data["providers"]["backends"]["deepseek"]["key_env"] = "DEEPSEEK_API_KEY"
    write_settings(path, data)
    os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2)
    status, payload = app.chat({"backend": "deepseek", "messages": []})
    assert status == 200 and payload["choices"][0]["message"]["content"] == "ok" and called == [True]


def test_a_failure_the_same_request_meets_again_is_not_resent(tmp_path, monkeypatch):
    # 422 is not resent by the OpenAI client; 502 is resent twice.
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", sample_settings()), tmp_path / "calls.jsonl")
    monkeypatch.setattr(app, "_openai", lambda *args: (_ for _ in ()).throw(bridge.FinalError("empty answer")))
    assert app.chat({"backend": "openai", "messages": []})[0] == 422
    monkeypatch.setattr(app, "_openai", lambda *args: (_ for _ in ()).throw(RuntimeError("HTTP 503")))
    assert app.chat({"backend": "openai", "messages": []})[0] == 502


def test_request_errors_are_not_echoed_raw(tmp_path):
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", sample_settings()), tmp_path / "calls.jsonl")
    status, payload = app.chat({"backend": "sk-" + "x" * 30, "messages": []})
    assert status == 400 and "sk-" not in json.dumps(payload)


def test_body_length_limits(tmp_path):
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", sample_settings()), tmp_path / "calls.jsonl")
    server = bridge.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import http.client

        for length in ("-1", str(bridge.MAX_BODY_BYTES + 1)):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.putrequest("POST", "/v1/chat/completions")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", length)
            conn.endheaders()
            response = conn.getresponse()
            assert response.status == 400, length
            conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_call_log_rotates_at_the_configured_size_and_timestamps_are_utc(tmp_path, monkeypatch):
    data = sample_settings()
    data["logs"] = {"max_mb": 1, "keep": 1}
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", data), tmp_path / "bridge-calls.jsonl")
    monkeypatch.setenv("TEST_OPENAI_KEY", "test-key")
    monkeypatch.setattr(app, "_openai", lambda *_args: {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    status, _payload = app.chat({"backend": "openai", "messages": []})
    assert status == 200
    record = json.loads(app.log_file.read_text(encoding="utf-8"))
    assert record["ts"].endswith("Z")
    app.log_file.write_text("x" * (1024 * 1024), encoding="utf-8")
    app.write_log({"n": 2})
    assert (tmp_path / "bridge-calls.jsonl.1").read_text(encoding="utf-8").startswith("x")
    assert json.loads(app.log_file.read_text(encoding="utf-8"))["n"] == 2


def test_call_log_rotates_at_its_size_limit(tmp_path, monkeypatch):
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", sample_settings()), tmp_path / "calls.jsonl")
    monkeypatch.setattr(bridge, "LOG_MAX_BYTES", 60)
    for number in range(5):
        app.write_log({"n": number, "pad": "x" * 20})
    previous = (tmp_path / "calls.jsonl.1").read_text(encoding="utf-8").splitlines()
    current = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert previous and current
    assert [json.loads(line)["n"] for line in previous + current][-1] == 4
    assert app.health()["log_ok"] is True


def _call(port, method, path, token=None, body=None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = token
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8") or "{}")
    status, header = response.status, response.getheader("WWW-Authenticate")
    conn.close()
    return status, payload, header


def test_token_compare():
    assert bridge.token_ok(None, "") and bridge.token_ok("Bearer anything", "")
    assert bridge.token_ok("Bearer s3cret", "s3cret")
    assert bridge.token_ok("bearer  s3cret ", "s3cret")
    for header in (None, "", "Bearer", "Bearer s3cre", "Bearer s3cret2", "Basic s3cret", "s3cret", "Bearer S3CRET"):
        assert not bridge.token_ok(header, "s3cret"), header
    # A non-ASCII guess is compared as bytes, not refused with an exception.
    assert not bridge.token_ok("Bearer s3crét", "s3cret")


def test_shared_token_guards_every_route_but_a_reduced_health(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "test-bridge-token")
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", sample_settings()), tmp_path / "calls.jsonl")
    seen = []
    monkeypatch.setattr(app, "chat", lambda body: seen.append(body) or (200, {"ok": True}))
    server = bridge.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    good = "Bearer test-bridge-token"
    body = json.dumps({"backend": "codex", "messages": []})
    try:
        status, health, _ = _call(port, "GET", "/health")
        assert status == 200 and sorted(health) == ["log_ok", "ok", "settings_ok"]
        assert "routing" not in health and "active" not in health
        status, health, _ = _call(port, "GET", "/health", good)
        assert status == 200 and "routing" in health and health["auth_required"] is True

        for token in (None, "Bearer wrong", "test-bridge-token"):
            assert _call(port, "GET", "/v1/models", token)[::2] == (401, "Bearer")
            status, payload, _ = _call(port, "POST", "/v1/chat/completions", token, body)
            assert status == 401 and payload == {"error": {"message": "unauthorized"}}
        assert seen == []
        assert app.health()["unauthorized"] == 6

        assert _call(port, "GET", "/v1/models", good)[0] == 200
        assert _call(port, "POST", "/v1/chat/completions", good, body)[0] == 200
        assert len(seen) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_unset_token_keeps_the_endpoint_open(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "   ")
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", sample_settings()), tmp_path / "calls.jsonl")
    server = bridge.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, health, _ = _call(port, "GET", "/health")
        assert status == 200 and "routing" in health and health["auth_required"] is False
        assert _call(port, "GET", "/v1/models")[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_refused_post_body_is_never_read(tmp_path, monkeypatch):
    """A caller without the token cannot make the bridge buffer a body."""
    import http.client

    monkeypatch.setenv("BRIDGE_TOKEN", "test-bridge-token")
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", sample_settings()), tmp_path / "calls.jsonl")
    server = bridge.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.putrequest("POST", "/v1/chat/completions")
        conn.putheader("Content-Length", str(bridge.MAX_BODY_BYTES))
        conn.endheaders()  # no body follows; the answer must not wait for it
        response = conn.getresponse()
        assert response.status == 401
        assert response.getheader("Connection") == "close"
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_anthropic_json_schema_without_a_schema_is_asked_for_as_an_object():
    body = {"messages": [{"role": "user", "content": "hi"}], "response_format": {"type": "json_schema", "json_schema": {"name": "r"}}}
    payload = bridge.to_anthropic(body, "m", None)
    assert "format" not in (payload.get("output_config") or {})
    assert "JSON" in payload["system"][0]["text"]


def test_anthropic_request_keeps_what_honchos_own_client_sends():
    # Through the bridge an Anthropic job keeps prompt caching, its thinking setting and its tool_choice.
    body = {"messages": [{"role": "system", "content": "Base."}, {"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
            "tool_choice": "required", "cache_control": {"type": "ephemeral"}, "thinking": {"type": "disabled"},
            "temperature": 0.2, "stop": "END"}
    payload = bridge.to_anthropic(body, "claude-haiku-5-5", "medium")
    assert payload["system"] == [{"type": "text", "text": "Base.", "cache_control": {"type": "ephemeral"}}]
    assert payload["cache_control"] == {"type": "ephemeral"} and payload["thinking"] == {"type": "disabled"}
    assert payload["tool_choice"] == {"type": "any"} and payload["output_config"] == {"effort": "medium"}
    assert payload["temperature"] == 0.2 and payload["stop_sequences"] == ["END"]


def test_anthropic_effort_turns_on_adaptive_thinking_on_claude_4_6_and_later():
    body = {"messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "f"}}}
    newer = bridge.to_anthropic(body, "claude-haiku-5-5", "high")
    assert newer["thinking"] == {"type": "adaptive"} and newer["tool_choice"] == {"type": "auto"}
    older = bridge.to_anthropic(body, "claude-haiku-4-5", "high")
    assert "thinking" not in older and older["tool_choice"] == {"type": "tool", "name": "f"}
    assert "thinking" not in bridge.to_anthropic(body, "claude-haiku-5-5", None)


def test_anthropic_effort_none_sends_thinking_disabled_and_no_effort():
    body = {"messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
            "tool_choice": {"type": "function", "function": {"name": "f"}}}
    for model in ("claude-haiku-5-5", "claude-haiku-4-5"):
        payload = bridge.to_anthropic(body, model, "none")
        assert payload["thinking"] == {"type": "disabled"}, model
        assert "output_config" not in payload, model
        assert payload["tool_choice"] == {"type": "tool", "name": "f"}, model  # nothing thinks, so a forced tool stays
    # The structured-output format still goes out; it is not an effort.
    schema = {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}}
    payload = bridge.to_anthropic({**body, "response_format": schema}, "claude-haiku-5-5", "none")
    assert payload["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    # A job's own thinking setting still wins.
    own = bridge.to_anthropic({**body, "thinking": {"type": "adaptive"}}, "claude-haiku-5-5", "none")
    assert own["thinking"] == {"type": "adaptive"} and "output_config" not in own


def test_a_client_that_stalls_after_the_headers_is_dropped(tmp_path, monkeypatch):
    # The accepted socket has a read/write timeout (SOCKET_TIMEOUT_SECONDS, 60 s in the image; shortened here).
    # A client that sends headers and then nothing gets its connection closed and its handler thread released.
    monkeypatch.setattr(bridge, "SOCKET_TIMEOUT_SECONDS", 0.5)
    app = bridge.Bridge(write_settings(tmp_path / "settings.json", sample_settings()), tmp_path / "calls.jsonl")
    server = bridge.serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        idle = threading.active_count()
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n")
        started = time.time()
        received = b""
        while True:                            # the body read times out: the existing 400, then the server closes
            chunk = client.recv(4096)
            if not chunk:
                break
            received += chunk
        assert time.time() - started < 5
        assert received.startswith(b"HTTP/1.1 400")
        client.close()
        for _ in range(50):
            if threading.active_count() <= idle:
                break
            time.sleep(0.1)
        assert threading.active_count() <= idle
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_none_never_sends_a_value_the_model_refuses():
    # Claude Fable, Mythos and Opus 5.5 always think; Sonnet 5.5 takes between_tools; two GPT-6 models refuse none.
    ask = {"messages": [{"role": "user", "content": "hi"}]}
    assert bridge.to_anthropic(ask, "claude-haiku-5-5", "none")["thinking"] == {"type": "disabled"}
    assert bridge.to_anthropic(ask, "claude-sonnet-5-5", "none")["thinking"] == {"type": "between_tools"}
    for model in ("claude-opus-5-5", "claude-fable-5-1"):
        payload = bridge.to_anthropic(ask, model, "none")
        assert "thinking" not in payload and payload["output_config"] == {"effort": "low"}
    openai = {"kind": "openai-compat", "base_url": "https://api.openai.com/v1"}
    assert bridge.effort_wire(openai, "none", "gpt-6.1-sol") == {"reasoning_effort": "low"}
    assert bridge.effort_wire(openai, "none", "gpt-5.4-mini") == {"reasoning_effort": "none"}
    assert bridge.effort_wire({"kind": "anthropic-api"}, "none", "claude-opus-5-5") == {"output_config": {"effort": "low"}}
