"""Honcho config.toml editor. No network and no Honcho import."""
from __future__ import annotations

import base64
import importlib.util
import json
import tomllib
import time
import os
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "src" / "dashboard" / "app" / "app.py"
GEN_PATH = ROOT / "src" / "config" / "gen_honcho_schema.py"
AUTH = ("admin", "test-password")
AUTH_HEADER = {"Authorization": "Basic " + base64.b64encode(b"admin:test-password").decode()}

_app_spec = importlib.util.spec_from_file_location("control_app_honcho", APP_PATH)
dashboard = importlib.util.module_from_spec(_app_spec)
_app_spec.loader.exec_module(dashboard)

_gen_spec = importlib.util.spec_from_file_location("gen_honcho_schema", GEN_PATH)
generator = importlib.util.module_from_spec(_gen_spec)
_gen_spec.loader.exec_module(generator)

FIXTURE = '''
class BaseModel:
    pass

class HonchoSettings:
    pass

Level = Literal["low", "high"]

class Nested(BaseModel):
    """Nested model."""

    model: str
    transport: str = "openai"
    api_key: str | None = None
    api_key_env: str | None = None
    count: Annotated[int, Field(gt=0, le=9)] = 1  # nested count

class LevelSettings(BaseModel):
    """One level."""

    rounds: Annotated[int, Field(ge=0, le=5, validation_alias="rounds")]
    nested: Nested

def _one():
    return Nested(model="m")

def _defaults():
    return {
        "low": LevelSettings(rounds=1, nested=_one()),
        "high": LevelSettings(rounds=2, nested=_one()),
    }

class SampleSettings(HonchoSettings):
    """Sample section."""

    model_config = SettingsConfigDict(env_prefix="SAMPLE_", extra="ignore")

    ENABLED: bool = True
    COUNT: Annotated[int, Field(default=3, gt=0, le=10)] = 3  # how many
    KIND: Level = "low"
    SECRET: str | None = None
    NESTED: Nested = Field(default_factory=_one)
    LEVELS: dict[Level, LevelSettings] = Field(default_factory=_defaults)

class AuthSettings(HonchoSettings):
    model_config = SettingsConfigDict(env_prefix="AUTH_", extra="ignore")

    USE_AUTH: bool = False
    JWT_SECRET: str | None = None
'''

SAMPLE = """# file comment
[deriver]
enabled = true  # keep this
workers = 2
custom = "leave me"

[summary]
messages_per_short_summary = 20
"""


def find_field(sections, path):
    parts = path.split(".")
    section = next(item for item in sections if item["name"] == parts[0])
    fields = section["fields"]
    node = None
    index = 1
    while index < len(parts):
        name = parts[index]
        node = next(item for item in fields if item["name"] == name)
        index += 1
        if index >= len(parts):
            return node
        if node.get("entries"):
            entry = next(item for item in node["entries"] if item["key"] == parts[index])
            fields = entry["fields"]
            index += 1
            continue
        fields = node.get("fields") or []
    return node


def test_schema_from_fixture(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "config.py").write_text(FIXTURE, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n', encoding="utf-8")
    schema = generator.generate(source / "config.py")
    assert schema["honcho_version"] == "9.9.9"
    assert list(schema["sections"]) == ["sample"]
    sample = schema["sections"]["sample"]
    assert sample["class"] == "SampleSettings"
    assert sample["env_prefix"] == "SAMPLE_"
    assert sample["description"] == "Sample section."
    assert "secret" not in sample["fields"]
    count = sample["fields"]["count"]
    assert count["type"] == "integer"
    assert count["default"] == 3
    assert count["constraints"] == {"gt": 0, "le": 10}
    assert count["description"] == "how many"
    assert count["env_name"] == "COUNT"
    assert sample["fields"]["kind"]["enum"] == ["low", "high"]
    assert sample["fields"]["kind"]["default"] == "low"
    assert sample["fields"]["enabled"]["type"] == "boolean"
    nested = sample["fields"]["nested"]
    assert nested["model"] == "Nested"
    assert nested["default"]["model"] == "m"
    assert nested["default"]["transport"] == "openai"
    assert "api_key" not in nested["default"]
    assert "api_key_env" in schema["models"]["Nested"]["fields"]
    assert "api_key" not in schema["models"]["Nested"]["fields"]
    assert schema["models"]["Nested"]["fields"]["count"]["description"] == "nested count"
    levels = sample["fields"]["levels"]
    assert levels["type"] == "map"
    assert levels["model"] == "LevelSettings"
    assert levels["key_enum"] == ["low", "high"]
    assert levels["default"]["low"]["rounds"] == 1
    assert levels["default"]["high"]["rounds"] == 2
    assert levels["default"]["low"]["nested"]["api_key_env"] is None
    assert "auth" not in schema["sections"]


def test_partial_class_emits_only_the_named_fields(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    extra = (
        "\n\nclass DBSettings(HonchoSettings):\n"
        "    model_config = SettingsConfigDict(env_prefix=\"DB_\", extra=\"ignore\")\n"
        "    CONNECTION_URI: str = \"postgresql://x\"\n"
        "    POOL_SIZE: int = 10\n"
        "    HNSW_ITERATIVE_SCAN: Literal[\"off\", \"relaxed_order\", \"strict_order\"] = \"off\"\n"
        "    HNSW_EF_SEARCH: Annotated[int, Field(ge=1, le=1000)] | None = None\n"
    )
    (source / "config.py").write_text(FIXTURE + extra, encoding="utf-8")
    schema = generator.generate(source / "config.py")
    # DBSettings and RerankSettings belong to the honcho_rerank wrapper, not to config.toml.
    assert "db" not in schema["sections"]


def test_committed_schema_shape():
    schema = json.loads((ROOT / "src" / "config" / "honcho-config.schema.json").read_text(encoding="utf-8"))
    assert schema["honcho_version"] == "3.5.0"
    for name in (
        "deriver",
        "dialectic",
        "summary",
        "embedding",
        "llm",
        "dream",
        "peer_card",
        "cache",
        "telemetry",
        "metrics",
        "webhook",
    ):
        assert name in schema["sections"]
    for name in ("auth", "sentry", "rerank", "db"):
        assert name not in schema["sections"]
    # The wrapper owns the rerank and HNSW settings; only [summary] OUTPUT_HEADROOM stays from the fork.
    headroom = schema["sections"]["summary"]["fields"]["output_headroom"]
    assert headroom["type"] == "number" and headroom["default"] == 1.0 and headroom["constraints"] == {"ge": 1.0}
    override = schema["models"]["ModelOverrideSettings"]["fields"]
    assert "api_key_env" in override
    assert "api_key" not in override
    levels = schema["sections"]["dialectic"]["fields"]["levels"]
    assert levels["type"] == "map"
    assert levels["key_enum"] == ["minimal", "low", "medium", "high", "max"]
    assert "secret" not in schema["sections"]["webhook"]["fields"]
    assert "anthropic_api_key" not in schema["sections"]["llm"]["fields"]
    assert schema["sections"]["deriver"]["fields"]["workers"]["env_name"] == "WORKERS"
    for name in (
        "ConfiguredModelSettings",
        "ModelOverrideSettings",
        "FallbackModelSettings",
        "ConfiguredEmbeddingModelSettings",
        "DialecticLevelSettings",
    ):
        assert name in schema["models"]


def _client(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setenv("CONTROL_USER", "admin")
    monkeypatch.setenv("CONTROL_PASSWORD", "test-password")
    monkeypatch.setenv("CONTROL_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("HONCHO_CONFIG_FILE", str(path))
    monkeypatch.setenv("HONCHO_SCHEMA_FILE", str(ROOT / "src" / "config" / "honcho-config.schema.json"))
    monkeypatch.setenv(
        "HONCHO_ENV_OVERRIDES",
        "API_WORKERS,DERIVER_WORKERS,DB_POOL_SIZE,DB_MAX_OVERFLOW,TELEMETRY_ENDPOINT,TELEMETRY_HEADERS",
    )
    monkeypatch.delenv("DOCKER_PROXY_URL", raising=False)
    monkeypatch.delenv("DOCKER_PROXY_ACTION_URL", raising=False)
    return TestClient(dashboard.app, headers=AUTH_HEADER), path


def test_page_has_honcho_section(tmp_path, monkeypatch):
    app, _path = _client(tmp_path, monkeypatch)
    response = app.get("/", auth=AUTH)
    assert response.status_code == 200
    assert "Restart Honcho" in response.text
    assert "/api/honcho-config/apply" in response.text
    assert "Honcho would use its own default" in response.text
    assert "cdn" not in response.text.lower()


def test_read_merges_defaults_and_overrides(tmp_path, monkeypatch):
    app, _path = _client(tmp_path, monkeypatch)
    body = app.get("/api/honcho-config", auth=AUTH)
    assert body.status_code == 200
    payload = body.json()
    assert payload["honcho_version"] == "3.5.0"
    assert payload["missing"] is False
    assert payload["parse_error"] is None
    assert payload["restart_configured"] is False
    assert "DERIVER_WORKERS" in payload["overrides"]
    assert any(item.endswith("custom") for item in payload["unknown"])
    enabled = find_field(payload["sections"], "deriver.enabled")
    assert enabled["value"] is True
    assert enabled["is_default"] is True
    assert enabled["read_only"] is False
    workers = find_field(payload["sections"], "deriver.workers")
    assert workers["value"] == 2
    assert workers["default"] == 1
    assert workers["is_default"] is False
    assert workers["read_only"] is True
    assert workers["note"] == "set in compose"
    endpoint = find_field(payload["sections"], "telemetry.endpoint")
    assert endpoint["read_only"] is True
    assert endpoint["note"] == "set in compose"
    dream = find_field(payload["sections"], "dream.deduction_model_config")
    assert dream["hint"] == "Dream routing is driven by settings.json, not here."
    model = find_field(payload["sections"], "dream.deduction_model_config.model")
    assert model["hint"] == dream["hint"]
    assert model["is_default"] is True
    minimal = find_field(payload["sections"], "dialectic.levels.minimal.max_tool_iterations")
    assert minimal["value"] == 1
    assert minimal["is_default"] is True


def test_review_does_not_write(tmp_path, monkeypatch):
    app, path = _client(tmp_path, monkeypatch)
    before = path.read_text(encoding="utf-8")
    response = app.post(
        "/api/honcho-config/review",
        auth=AUTH,
        json={"values": {"deriver": {"enabled": False}}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["diff"] == [{
        "path": "deriver/enabled",
        "op": "replace",
        "before": True,
        "after": False,
    }]
    assert any("custom" in item for item in body["unknown"])
    assert path.read_text(encoding="utf-8") == before


def test_write_preserves_comment_and_unknown_key(tmp_path, monkeypatch):
    app, path = _client(tmp_path, monkeypatch)
    response = app.post(
        "/api/honcho-config/write",
        auth=AUTH,
        json={"values": {"deriver": {"enabled": False, "workers": 2}}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["bak"] is True
    text = path.read_text(encoding="utf-8")
    assert "# file comment" in text
    assert "# keep this" in text
    assert 'custom = "leave me"' in text
    assert "enabled = false" in text
    assert "workers = 2" in text
    backups = list((tmp_path / "backups" / "config-toml").glob("config.toml.bak_*"))
    assert len(backups) == 1
    assert "enabled = true" in backups[0].read_text(encoding="utf-8")


def test_write_refused_on_validation_error(tmp_path, monkeypatch):
    app, path = _client(tmp_path, monkeypatch)
    before = path.read_text(encoding="utf-8")
    response = app.post(
        "/api/honcho-config/write",
        auth=AUTH,
        json={"values": {"summary": {"messages_per_short_summary": 0}}},
    )
    assert response.status_code == 400
    assert "must be > 0" in response.json()["detail"][0]
    assert path.read_text(encoding="utf-8") == before
    assert list((tmp_path / "backups" / "config-toml").glob("config.toml.bak_*")) == []


def test_env_override_is_read_only(tmp_path, monkeypatch):
    app, path = _client(tmp_path, monkeypatch)
    before = path.read_text(encoding="utf-8")
    response = app.post(
        "/api/honcho-config/write",
        auth=AUTH,
        json={"values": {"deriver": {"workers": 9}}},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == ["deriver.workers: set in compose"]
    assert path.read_text(encoding="utf-8") == before


def test_backup_rotation_keeps_ten_and_recent(tmp_path, monkeypatch):
    app, path = _client(tmp_path, monkeypatch)
    for count in range(12):
        response = app.post(
            "/api/honcho-config/write",
            auth=AUTH,
            json={"values": {"summary": {"messages_per_short_summary": 21 + count}}},
        )
        assert response.status_code == 200
    backups = list((tmp_path / "backups" / "config-toml").glob("config.toml.bak_*"))
    assert len(backups) == 12
    old = time.time() - 8 * 86400
    for item in backups:
        os.utime(item, (old, old))
    response = app.post(
        "/api/honcho-config/write",
        auth=AUTH,
        json={"values": {"summary": {"messages_per_short_summary": 99}}},
    )
    assert response.status_code == 200
    assert len(list((tmp_path / "backups" / "config-toml").glob("config.toml.bak_*"))) == 10


def test_blank_nullable_submodel_is_left_out():
    honcho_config = dashboard.honcho_config
    schema = {
        "models": {
            "Fallback": {"fields": {
                "model": {"type": "string", "required": True, "env_name": "MODEL"},
                "transport": {"type": "string", "required": True, "enum": ["openai"], "env_name": "TRANSPORT"},
                "overrides": {"type": "object", "free": True, "env_name": "OVERRIDES"},
            }},
        },
    }
    fields = {
        "fallback": {"type": "object", "nullable": True, "model": "Fallback", "env_name": "FALLBACK"},
        "count": {"type": "integer", "env_name": "COUNT"},
    }
    blank = {"fallback": {"model": "", "transport": "", "overrides": {"base_url": None}}, "count": 2}
    honcho_config._drop_blank_nullable(schema, fields, blank, {})
    assert blank == {"count": 2}
    edited = {"fallback": {"model": "m", "transport": ""}, "count": 2}
    honcho_config._drop_blank_nullable(schema, fields, edited, {})
    assert "fallback" in edited
    fields["https"] = {"type": "boolean", "nullable": True, "default": None, "env_name": "HTTPS"}
    unticked = {"https": False, "count": 2}
    honcho_config._drop_blank_nullable(schema, fields, unticked, {})
    assert unticked == {"count": 2}
    ticked = {"https": True}
    honcho_config._drop_blank_nullable(schema, fields, ticked, {})
    assert ticked == {"https": True}
    unticked_in_file = {"https": False}
    honcho_config._drop_blank_nullable(schema, fields, unticked_in_file, {"https": True})
    assert unticked_in_file == {"https": False}
    in_file = {"fallback": {"model": "", "transport": ""}}
    honcho_config._drop_blank_nullable(schema, fields, in_file, {"fallback": {"model": "m", "transport": "openai"}})
    assert "fallback" in in_file


COMPLETE_MODELS = """
[deriver.model_config]
transport = "anthropic"
model = "deriver-model"

[summary.model_config]
transport = "anthropic"
model = "summary-model"

[dream.deduction_model_config]
transport = "openai"
model = "bridge-model"

[dream.induction_model_config]
transport = "openai"
model = "bridge-model"

[embedding.model_config]
transport = "openai"
model = "embed-model"
""" + "".join(
    f'\n[dialectic.levels.{level}.model_config]\ntransport = "anthropic"\nmodel = "level-model"\n'
    for level in ("minimal", "low", "medium", "high", "max")
)


def test_structured_output_mode_off_the_openai_transport_refuses_apply(tmp_path, monkeypatch):
    # Honcho 3.5.0 raises a validation error at start-up for structured_output_mode on the anthropic transport.
    app, path = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "run_container_action", lambda *a: pytest.fail("restart sent"))
    text = COMPLETE_MODELS.replace('[dream.deduction_model_config]\ntransport = "openai"\n',
                                   '[dream.deduction_model_config]\ntransport = "anthropic"\nstructured_output_mode = "json_object"\n')
    path.write_text(text, encoding="utf-8")
    gaps = app.get("/api/honcho-config", auth=AUTH).json()["model_gaps"]
    assert len(gaps) == 1 and gaps[0].startswith("dream.deduction_model_config: structured_output_mode")
    assert app.post("/api/honcho-config/apply", json={}, auth=AUTH).status_code == 409
    path.write_text(COMPLETE_MODELS.replace('model = "bridge-model"\n', 'model = "bridge-model"\nstructured_output_mode = "json_object"\n', 1),
                    encoding="utf-8")
    assert app.get("/api/honcho-config", auth=AUTH).json()["model_gaps"] == []


def test_model_gaps_listed_and_apply_refused(tmp_path, monkeypatch):
    app, path = _client(tmp_path, monkeypatch)
    calls = []

    def recorder(request, action, name):
        calls.append((action, name))
        return {"ok": True}

    monkeypatch.setattr(dashboard, "run_container_action", recorder)
    text = COMPLETE_MODELS.replace('[summary.model_config]\ntransport = "anthropic"\n', "[summary.model_config]\n")
    path.write_text(text, encoding="utf-8")
    payload = app.get("/api/honcho-config", auth=AUTH).json()
    assert len(payload["model_gaps"]) == 1
    assert payload["model_gaps"][0].startswith("summary.model_config: no transport")
    rows = {row["task"]: row for row in payload["models"]}
    assert set(rows) >= {"deriver", "summary", "dialectic minimal", "dialectic max", "dream deduction", "dream induction", "embedding"}
    assert rows["summary"]["model"] == "summary-model"
    assert rows["summary"]["transport_source"] == "default"
    assert rows["deriver"]["model_source"] == "file"
    control = app.get("/api/control", auth=AUTH).json()
    assert {row["task"] for row in control["models"]} == set(rows)
    refused = app.post("/api/honcho-config/apply", json={}, auth=AUTH)
    assert refused.status_code == 409
    assert any("summary.model_config" in line for line in refused.json()["detail"])
    assert calls == []
    review = app.post(
        "/api/honcho-config/review",
        auth=AUTH,
        json={"values": {"summary": {"model_config": {"transport": "anthropic"}}}},
    ).json()
    assert review["model_gaps"] == []
    assert path.read_text(encoding="utf-8") == text
    path.write_text(COMPLETE_MODELS, encoding="utf-8")
    assert app.get("/api/honcho-config", auth=AUTH).json()["model_gaps"] == []
    monkeypatch.setattr(dashboard, "RESTART_ALLOWED", ("proj-api-1", "proj-deriver-1", "other-scheduler-1"))
    applied = app.post("/api/honcho-config/apply", json={}, auth=AUTH)
    assert applied.status_code == 200
    assert calls == [("restart", "proj-api-1"), ("restart", "proj-deriver-1")]
    monkeypatch.setattr(dashboard, "RESTART_ALLOWED", ("proj-deriver-1",))
    missing = app.post("/api/honcho-config/apply", json={}, auth=AUTH)
    assert missing.status_code == 409 and "api" in missing.json()["detail"]
    assert len(calls) == 2


def test_writing_the_default_transport_fills_the_gap(tmp_path, monkeypatch):
    # The write skipped a value equal to Honcho's default, so choosing the default left Apply refused.
    app, path = _client(tmp_path, monkeypatch)
    text = COMPLETE_MODELS.replace('[summary.model_config]\ntransport = "anthropic"\n', "[summary.model_config]\n")
    path.write_text(text, encoding="utf-8")
    rows = {row["task"]: row for row in app.get("/api/honcho-config").json()["models"]}
    default = rows["summary"]["transport"]
    assert rows["summary"]["transport_source"] == "default" and default
    mtime = app.get("/api/honcho-config").json()["mtime_ns"]
    written = app.post("/api/honcho-config/write", json={"values": {"summary": {"model_config": {"transport": default}}}, "mtime_ns": mtime})
    assert written.status_code == 200, written.text
    assert app.get("/api/honcho-config").json()["model_gaps"] == []


def test_service_container_matches_compose_names():
    names = ["honcho-api-1", "honcho-deriver-1", "honcho-tools-scheduler-1", "apiary"]
    assert dashboard.service_container(names, "api") == "honcho-api-1"
    assert dashboard.service_container(names, "deriver") == "honcho-deriver-1"
    assert dashboard.service_container(["api"], "api") == "api"
    assert dashboard.service_container(["a-api-1", "b-api-1"], "api") is None
    assert dashboard.service_container([], "deriver") is None


def test_empty_file_falls_back_everywhere(tmp_path, monkeypatch):
    app, path = _client(tmp_path, monkeypatch)
    path.write_text("", encoding="utf-8")
    payload = app.get("/api/honcho-config", auth=AUTH).json()
    assert len(payload["model_gaps"]) == len(payload["models"])
    deriver = next(row for row in payload["models"] if row["task"] == "deriver")
    assert deriver["model_source"] == "default"
    assert deriver["model"]
    assert app.post("/api/honcho-config/apply", json={}, auth=AUTH).status_code == 409


def test_summary_headroom_reviews_and_writes_and_rerank_db_are_gone(tmp_path, monkeypatch):
    app, path = _client(tmp_path, monkeypatch)
    view = app.get("/api/honcho-config", auth=AUTH).json()
    assert find_field(view["sections"], "summary.output_headroom")["default"] == 1.0
    assert not any(section["name"] in ("rerank", "db") for section in view["sections"])
    values = {"summary": {"output_headroom": 2.0}}
    review = app.post("/api/honcho-config/review", json={"values": values, "mtime_ns": view["mtime_ns"]}, auth=AUTH)
    assert review.status_code == 200, review.text
    assert {row["path"] for row in review.json()["diff"]} >= {"summary/output_headroom"}
    written = app.post("/api/honcho-config/write", json={"values": values, "mtime_ns": view["mtime_ns"]}, auth=AUTH)
    assert written.status_code == 200, written.text
    text = path.read_text(encoding="utf-8")
    assert "output_headroom = 2.0" in text
    assert "[rerank]" not in text and "[db]" not in text
    assert "# file comment" in text and 'custom = "leave me"' in text
    for bad in (
        {"rerank": {"enabled": True}},
        {"db": {"hnsw_ef_search": 100}},
        {"summary": {"output_headroom": 0.5}},
    ):
        again = app.get("/api/honcho-config", auth=AUTH).json()
        refused = app.post("/api/honcho-config/write", json={"values": bad, "mtime_ns": again["mtime_ns"]}, auth=AUTH)
        assert refused.status_code in (400, 422), (bad, refused.text)
    assert path.read_text(encoding="utf-8") == text


def test_fallback_table_writes_and_undo_restores_the_deriver(tmp_path, monkeypatch):
    # The install write puts the old primary in fallback and routes the primary. A second write puts it back.
    app, path = _client(tmp_path, monkeypatch)
    path.write_text(
        "\n".join([
            "[deriver.model_config]",
            'transport = "anthropic"',
            'model = "claude-haiku-5-5"',
            'thinking_effort = "medium"',
            "",
            "[deriver.model_config.overrides.provider_params.extra_body.cache_control]",
            'type = "ephemeral"',
            "",
            "[summary.model_config]",
            'transport = "openai"',
            'model = "stage-route"',
            "",
            "[summary.model_config.overrides]",
            'base_url = "http://bridge.example/v1"',
            'api_key_env = "LIVE_BRIDGE_KEY"',
            "",
            "[summary.model_config.overrides.provider_params.extra_body]",
            'backend = "@summary"',
            "",
        ]),
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    view = app.get("/api/honcho-config", auth=AUTH).json()

    def values_of(prefix):
        view_now = app.get("/api/honcho-config", auth=AUTH).json()
        out = {}
        for name in ("model", "transport", "thinking_effort", "structured_output_mode"):
            out[name] = find_field(view_now["sections"], prefix + "." + name)["value"]
        out["base_url"] = find_field(view_now["sections"], prefix + ".overrides.base_url")["value"]
        out["api_key_env"] = find_field(view_now["sections"], prefix + ".overrides.api_key_env")["value"]
        out["provider_params"] = find_field(view_now["sections"], prefix + ".overrides.provider_params")["value"]
        return out

    summary_before = values_of("summary.model_config")
    install = {"deriver": {"model_config": {
        "transport": "openai",
        "model": "stage-route",
        "thinking_effort": None,
        "fallback": {
            "model": "claude-haiku-5-5",
            "transport": "anthropic",
            "thinking_effort": "medium",
            "overrides": {"provider_params": {"extra_body": {"cache_control": {"type": "ephemeral"}}}},
        },
        "overrides": {
            "base_url": "http://bridge.example/v1",
            "api_key_env": "LIVE_BRIDGE_KEY",
            "provider_params": {"extra_body": {"cache_control": {"type": "ephemeral"}, "backend": "@deriver"}},
        },
    }}}
    review = app.post("/api/honcho-config/review", json={"values": install, "mtime_ns": view["mtime_ns"]}, auth=AUTH)
    assert review.status_code == 200, review.text
    assert path.read_text(encoding="utf-8") == before
    written = app.post("/api/honcho-config/write", json={"values": install, "mtime_ns": view["mtime_ns"]}, auth=AUTH)
    assert written.status_code == 200, written.text
    assert written.json()["bak"] is True
    primary = values_of("deriver.model_config")
    assert primary["transport"] == "openai" and primary["model"] == "stage-route"
    assert primary["thinking_effort"] is None
    assert primary["base_url"] == "http://bridge.example/v1"
    assert primary["api_key_env"] == "LIVE_BRIDGE_KEY"
    assert primary["provider_params"]["extra_body"]["backend"] == "@deriver"
    assert primary["provider_params"]["extra_body"]["cache_control"] == {"type": "ephemeral"}
    fallback = values_of("deriver.model_config.fallback")
    assert fallback["model"] == "claude-haiku-5-5"
    assert fallback["transport"] == "anthropic"
    assert fallback["thinking_effort"] == "medium"
    assert fallback["structured_output_mode"] is None
    assert fallback["provider_params"]["extra_body"]["cache_control"] == {"type": "ephemeral"}
    assert values_of("summary.model_config") == summary_before
    again = app.get("/api/honcho-config", auth=AUTH).json()
    undo = {"deriver": {"model_config": {
        "transport": "anthropic",
        "model": "claude-haiku-5-5",
        "thinking_effort": "medium",
        "fallback": None,
        "overrides": {
            "base_url": None,
            "api_key_env": None,
            "provider_params": {"extra_body": {"cache_control": {"type": "ephemeral"}}},
        },
    }}}
    restored = app.post("/api/honcho-config/write", json={"values": undo, "mtime_ns": again["mtime_ns"]}, auth=AUTH)
    assert restored.status_code == 200, restored.text
    assert values_of("deriver.model_config") == {
        "model": "claude-haiku-5-5",
        "transport": "anthropic",
        "thinking_effort": "medium",
        "structured_output_mode": None,
        "base_url": None,
        "api_key_env": None,
        "provider_params": {"extra_body": {"cache_control": {"type": "ephemeral"}}},
    }
    gone = find_field(app.get("/api/honcho-config", auth=AUTH).json()["sections"], "deriver.model_config.fallback.model")
    assert gone["value"] is None and gone["is_default"] is True
    assert values_of("summary.model_config") == summary_before


def test_undo_on_a_block_without_provider_params_restores_it_exactly(tmp_path, monkeypatch):
    # Undo sends the page's edits for a deriver whose original had no overrides: the overrides table is removed
    # whole and no empty provider_params is written, so the file reads as before the install.
    app, path = _client(tmp_path, monkeypatch)
    original = "\n".join([
        "[deriver.model_config]",
        'transport = "anthropic"',
        'model = "claude-haiku-5-5"',
        'thinking_effort = "medium"',
        "",
    ])
    path.write_text(original, encoding="utf-8")
    view = app.get("/api/honcho-config", auth=AUTH).json()
    install = {"deriver": {"model_config": {
        "transport": "openai", "model": "stage-route", "thinking_effort": None,
        "fallback": {"model": "claude-haiku-5-5", "transport": "anthropic", "thinking_effort": "medium"},
        "overrides": {"base_url": "http://bridge.example/v1", "api_key_env": "LIVE_BRIDGE_KEY",
                      "provider_params": {"extra_body": {"backend": "@deriver"}}},
    }}}
    written = app.post("/api/honcho-config/write", json={"values": install, "mtime_ns": view["mtime_ns"]}, auth=AUTH)
    assert written.status_code == 200, written.text
    assert "[deriver.model_config.overrides]" in path.read_text(encoding="utf-8")
    again = app.get("/api/honcho-config", auth=AUTH).json()
    undo = {"deriver": {"model_config": {
        "transport": "anthropic", "model": "claude-haiku-5-5", "thinking_effort": "medium",
        "thinking_budget_tokens": None, "max_output_tokens": None, "structured_output_mode": None,
        "overrides": None, "fallback": None,
    }}}
    restored = app.post("/api/honcho-config/write", json={"values": undo, "mtime_ns": again["mtime_ns"]}, auth=AUTH)
    assert restored.status_code == 200, restored.text
    text = path.read_text(encoding="utf-8")
    assert "overrides" not in text and "provider_params" not in text and "fallback" not in text
    assert text.rstrip("\n") == original.rstrip("\n")


ROUTED_SUMMARY = {"summary": {"model_config": {
    "transport": "openai", "model": "stage-route",
    "fallback": {"model": "claude-haiku-5-5", "transport": "anthropic"},
    "overrides": {"base_url": "http://bridge.example/v1", "api_key_env": "LIVE_BRIDGE_KEY",
                  "provider_params": {"extra_body": {"backend": "@summary"}}},
}}}


def test_undo_routing_reads_the_copy_from_before_route_all(tmp_path, monkeypatch):
    # The Write that carries Route all's edits keeps one copy of config.toml. "Jobs only" puts every job table back
    # from it and keeps a later unrelated edit; "whole file" is the copy byte for byte. Both refuse a stale mtime
    # and refuse without a copy.
    app, path = _client(tmp_path, monkeypatch)
    original = "\n".join([
        "# my file",
        "[deriver.model_config]",
        'transport = "anthropic"',
        'model = "claude-haiku-5-5"',
        "",
        "[deriver.model_config.fallback]  # my own fallback",
        'transport = "openai"',
        'model = "gpt-6-luna"',
        "",
        "[summary.model_config]",
        'transport = "anthropic"',
        'model = "claude-haiku-5-5"',
        "",
    ])
    path.write_text(original, encoding="utf-8")
    copy_path = tmp_path / "bak" / "config.toml.before-route-all"
    monkeypatch.setenv("HONCHO_CONFIG_BAK_DIR", str(tmp_path / "bak"))

    def mtime():
        return app.get("/api/honcho-config", auth=AUTH).json()["mtime_ns"]

    def write(values, **extra):
        response = app.post("/api/honcho-config/write", json={"values": values, "mtime_ns": mtime(), **extra}, auth=AUTH)
        assert response.status_code == 200, response.text

    assert app.get("/api/honcho-config", auth=AUTH).json()["before_route_all_at"] is None
    refused = app.post("/api/honcho-config/undo-routing", json={"scope": "jobs", "mtime_ns": mtime()}, auth=AUTH)
    assert refused.status_code == 409 and refused.json()["detail"] == "No copy from before routing"
    assert app.post("/api/honcho-config/undo-routing", json={"scope": "all", "mtime_ns": mtime()}, auth=AUTH).status_code == 400
    # An ordinary write makes no copy.
    write({"summary": {"output_headroom": 2.0}})
    assert not copy_path.exists()
    before_install = path.read_text(encoding="utf-8")
    # The install write: the deriver's own fallback is kept, the summary gets one.
    install = {"deriver": {"model_config": {
        "transport": "openai", "model": "stage-route",
        "overrides": {"base_url": "http://bridge.example/v1", "api_key_env": "LIVE_BRIDGE_KEY",
                      "provider_params": {"extra_body": {"backend": "@deriver"}}},
    }}, **ROUTED_SUMMARY}
    write(install, before_route_all=True)
    assert copy_path.read_text(encoding="utf-8") == before_install
    assert app.get("/api/honcho-config", auth=AUTH).json()["before_route_all_at"]
    # Later edits: an unrelated key, and the user's own fallback changed on the page. Neither touches the copy.
    write({"summary": {"output_headroom": 3.0}})
    write({"deriver": {"model_config": {"fallback": {"model": "gpt-6-nova"}}}})
    assert copy_path.read_text(encoding="utf-8") == before_install
    stale = app.post("/api/honcho-config/undo-routing", json={"scope": "jobs", "mtime_ns": "1"}, auth=AUTH)
    assert stale.status_code == 409
    done = app.post("/api/honcho-config/undo-routing", json={"scope": "jobs", "mtime_ns": mtime()}, auth=AUTH)
    assert done.status_code == 200, done.text
    assert done.json()["bak"] is True
    after = tomllib.loads(path.read_text(encoding="utf-8"))
    want = tomllib.loads(before_install)
    assert after["deriver"] == want["deriver"]
    assert after["summary"]["model_config"] == want["summary"]["model_config"]
    assert after["summary"]["output_headroom"] == 3.0
    assert "# my own fallback" in path.read_text(encoding="utf-8")
    # The jobs-only undo consumed the copy. Route all again: a new copy, of the file as it is now.
    assert not copy_path.exists()
    assert app.get("/api/honcho-config", auth=AUTH).json()["before_route_all_at"] is None
    before_second = path.read_text(encoding="utf-8")
    write(install, before_route_all=True)
    assert copy_path.read_text(encoding="utf-8") == before_second
    # Whole file: byte for byte the copy, so the later headroom edit is gone too.
    write({"summary": {"output_headroom": 4.0}})
    stale = app.post("/api/honcho-config/undo-routing", json={"scope": "file", "mtime_ns": "1"}, auth=AUTH)
    assert stale.status_code == 409
    expected = copy_path.read_bytes()
    whole = app.post("/api/honcho-config/undo-routing", json={"scope": "file", "mtime_ns": mtime()}, auth=AUTH)
    assert whole.status_code == 200, whole.text
    assert path.read_bytes() == expected
    assert path.read_text(encoding="utf-8") == before_second
    assert not copy_path.exists()
    # A job table absent from the copy is removed by the jobs-only undo (a Route all write that changes nothing
    # takes no copy, so route again first).
    write(install, before_route_all=True)
    write({"dream": {"induction_model_config": {"transport": "anthropic", "model": "claude-haiku-5-5"}}})
    assert "induction_model_config" in path.read_text(encoding="utf-8")
    gone = app.post("/api/honcho-config/undo-routing", json={"scope": "jobs", "mtime_ns": mtime()}, auth=AUTH)
    assert gone.status_code == 200, gone.text
    assert "induction_model_config" not in path.read_text(encoding="utf-8")


def test_the_copy_from_before_route_all_is_made_once_and_consumed_by_an_undo(tmp_path, monkeypatch):
    # A second Route all write keeps the first copy, so an undo always reaches the file from before the first routing;
    # either undo deletes the copy, and the page then reports no copy.
    app, path = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("HONCHO_CONFIG_BAK_DIR", str(tmp_path / "bak"))
    copy_path = tmp_path / "bak" / "config.toml.before-route-all"
    original = path.read_text(encoding="utf-8")

    def mtime():
        return app.get("/api/honcho-config", auth=AUTH).json()["mtime_ns"]

    def write(values):
        response = app.post("/api/honcho-config/write", json={"values": values, "mtime_ns": mtime(), "before_route_all": True}, auth=AUTH)
        assert response.status_code == 200, response.text

    for scope in ("jobs", "file"):
        write({"summary": {"output_headroom": 2.0}})
        assert copy_path.read_text(encoding="utf-8") == original
        write({"summary": {"output_headroom": 3.0}})
        assert copy_path.read_text(encoding="utf-8") == original, "the second Route all write replaced the first copy"
        assert app.get("/api/honcho-config", auth=AUTH).json()["before_route_all_at"]
        done = app.post("/api/honcho-config/undo-routing", json={"scope": scope, "mtime_ns": mtime()}, auth=AUTH)
        assert done.status_code == 200, done.text
        assert not copy_path.exists(), scope
        assert app.get("/api/honcho-config", auth=AUTH).json()["before_route_all_at"] is None, scope
        again = app.post("/api/honcho-config/undo-routing", json={"scope": scope, "mtime_ns": mtime()}, auth=AUTH)
        assert again.status_code == 409 and again.json()["detail"] == "No copy from before routing"
        if scope == "jobs":
            # Jobs only: output_headroom is not a job table, so it stays; put it back for the file round.
            response = app.post("/api/honcho-config/write", json={"values": {"summary": {"output_headroom": None}}, "mtime_ns": mtime()}, auth=AUTH)
            assert response.status_code == 200, response.text
            assert path.read_text(encoding="utf-8").rstrip("\n") == original.rstrip("\n")
        else:
            assert path.read_text(encoding="utf-8") == original


def test_failed_write_puts_the_backup_back(tmp_path, monkeypatch):
    # config.toml is rewritten in place (a single-file bind mount). If the write fails after the file was opened,
    # the backup taken at the start is written back the same way, and the request still fails.
    app, path = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("HONCHO_CONFIG_BAK_DIR", str(tmp_path / "bak"))
    original = path.read_text(encoding="utf-8")
    calls = {"n": 0}
    real_fsync = os.fsync

    def fsync_once_failing(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real_fsync(fd)

    monkeypatch.setattr(dashboard.honcho_config.os, "fsync", fsync_once_failing)
    view = app.get("/api/honcho-config", auth=AUTH).json()
    with pytest.raises(OSError):   # the test client re-raises what the server would answer as 500
        app.post("/api/honcho-config/write", json={"values": {"summary": {"output_headroom": 2.0}}, "mtime_ns": view["mtime_ns"]}, auth=AUTH)
    assert path.read_text(encoding="utf-8") == original
    assert calls["n"] == 2
    copies = list((tmp_path / "bak").glob("config.toml.bak_*"))
    assert len(copies) == 1 and copies[0].read_text(encoding="utf-8") == original


def test_schema_provenance_and_extracted_settings(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "config.py").write_text("from src.extracted import SampleSettings\n", encoding="utf-8")
    (source / "extracted.py").write_text(FIXTURE.replace("SampleSettings(HonchoSettings)", "SampleSettings(BaseSettings)"), encoding="utf-8")
    (tmp_path / "FORK.md").write_text("# Honcho 9.9 fork\n", encoding="utf-8")
    class Result:
        stdout = "a" * 40 + "\n"
    monkeypatch.setattr(generator.subprocess, "run", lambda *args, **kwargs: Result())
    result = generator.generate(source / "config.py")
    assert result["fork_sha"] == "a" * 40
    assert result["fork_version_heading"] == "Honcho 9.9 fork"
    assert "sample" in result["sections"]



def test_actual_environment_lock_is_detected(tmp_path, monkeypatch):
    app, _path = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("SUMMARY_MAX_TOKENS_SHORT", "300")
    view = app.get("/api/honcho-config").json()
    fields = next(section["fields"] for section in view["sections"] if section["name"] == "summary")
    field = next(field for field in fields if field["name"] == "max_tokens_short")
    assert field["read_only"] is True
    response = app.post("/api/honcho-config/review", json={"values": {"summary": {"max_tokens_short": 200}}})
    assert response.status_code == 400


def test_exported_upstream_schema_records_tag_commit(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "config.py").write_text(FIXTURE, encoding="utf-8")
    (tmp_path / "FORK.md").write_text("# Stale fork heading\n", encoding="utf-8")
    commands = []

    def resolve_tag(command, **kwargs):
        commands.append(command)
        return type("Result", (), {"stdout": "0cd37dd" + "a" * 33 + "\n"})()

    monkeypatch.setattr(generator.subprocess, "run", resolve_tag)
    schema = generator.generate(source / "config.py", upstream_repo=tmp_path, upstream_tag="v3.5.0")
    assert schema["upstream_tag"] == "v3.5.0"
    assert schema["upstream_sha"] == "0cd37dd" + "a" * 33
    assert schema["fork_sha"] is None
    assert schema["fork_version_heading"] is None
    assert len(commands) == 1
    assert commands[0][-1] == "refs/tags/v3.5.0^{commit}"


def test_committed_schema_has_upstream_provenance():
    schema = json.loads((ROOT / "src" / "config" / "honcho-config.schema.json").read_text(encoding="utf-8"))
    assert schema["upstream_tag"] == "v3.5.0"
    assert schema["upstream_sha"].startswith("0cd37dd")
    assert schema["fork_sha"] is None and schema["fork_version_heading"] is None
    assert "rerank" not in schema["sections"] and "db" not in schema["sections"]
    assert schema["sections"]["summary"]["fields"]["output_headroom"]["default"] == 1.0
