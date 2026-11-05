"""The rerank block of settings.json: schema, example and validate.py. No Honcho import."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import sample_settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "src" / "config" / "settings.schema.json").read_text(encoding="utf-8"))
EXAMPLE = json.loads((ROOT / "src" / "config" / "settings.example.json").read_text(encoding="utf-8"))
RERANK_KEYS = {
    "enabled", "provider", "model", "base_url", "candidates", "max_documents", "timeout_seconds",
    "min_score", "daily_request_cap",
}

_spec = importlib.util.spec_from_file_location("settings_validate", ROOT / "src" / "config" / "validate.py")
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)


def valid_base() -> dict:
    """sample_settings with the one field the schema refuses (effort on an anthropic-api backend) removed."""
    data = sample_settings()
    data["providers"]["backends"]["anthropic"].pop("effort", None)
    return data


def errors(data) -> list[str]:
    Draft202012Validator.check_schema(SCHEMA)
    return [e.message for e in Draft202012Validator(SCHEMA).iter_errors(data)]


def test_example_and_sample_validate():
    assert errors(EXAMPLE) == []
    assert errors(valid_base()) == []
    assert set(SCHEMA["$defs"]["rerankOverride"]["properties"]) == RERANK_KEYS
    assert "api_key" not in RERANK_KEYS


def test_rerank_block_accepted_in_defaults_peers_and_pairs():
    data = valid_base()
    full = {
        "enabled": True, "provider": "tei", "model": "bge-reranker", "base_url": "http://reranker.example:8080",
        "candidates": 50, "max_documents": 32, "timeout_seconds": 2.5, "min_score": 0.2,
        "daily_request_cap": 0,
    }
    data["defaults"]["rerank"] = full
    data["peers"]["observer"]["rerank"] = {"enabled": False}
    data["pairs"]["observer/observed"]["rerank"] = {}
    assert errors(data) == []


def test_rerank_block_rejects_key_bad_values_and_null():
    base = valid_base()
    cases = [
        {"api_key": "x"},
        {"enabled": "yes"},
        {"provider": "openai"},
        {"model": ""},
        {"base_url": "ftp://x"},
        {"base_url": "https://user:pw@reranker.example/v1"},
        {"base_url": "https://reranker.example/v1?api_key=x"},
        {"base_url": "https://reranker.example/v1#frag"},
        {"base_url": "https://reranker.example/a b"},
        {"base_url": "https://"},
        {"candidates": 0},
        {"candidates": 1001},
        {"max_documents": 10001},
        {"timeout_seconds": 0},
        {"daily_request_cap": -1},
        {"failures_before_cooldown": 0},
        {"cooldown_seconds": -0.5},
        {"model": "sk-secret"},
        {"enabled": None},
    ]
    for block in cases:
        data = json.loads(json.dumps(base))
        data["defaults"]["rerank"] = block
        assert errors(data), block
    data = json.loads(json.dumps(base))
    data["defaults"]["rerank"] = "on"
    assert errors(data)


def test_stage_object_effort_is_checked_like_a_string():
    # A stage may be "backend:model" or {"model", "effort"}. The object is checked on its model.
    data = json.loads((ROOT / "src" / "config" / "settings.example.json").read_text(encoding="utf-8"))
    assert '"backend:model"' in data["_help"] or "backend:model" in data["_help"]
    backends = data["providers"]["backends"]
    disabled = next(name for name, block in backends.items() if block.get("enabled") is False)
    enabled = next(name for name, block in backends.items() if block.get("enabled") is not False)
    data["providers"]["stages"] = {"summary": f"{enabled}:m"}
    assert errors(data) == []
    data["providers"]["stages"] = {"summary": {"model": f"{enabled}:m", "effort": "high"}}
    assert errors(data) == []
    bad_effort = json.loads(json.dumps(data))
    bad_effort["providers"]["stages"]["summary"] = {"model": f"{enabled}:m", "effort": "ultra"}
    assert errors(bad_effort)
    missing_model = json.loads(json.dumps(data))
    missing_model["providers"]["stages"]["summary"] = {"effort": "high"}
    assert errors(missing_model)
    data["providers"]["stages"] = {
        "summary": {"model": "nope:model", "effort": "high"},
        "deriver": {"model": disabled, "effort": "low"},
    }
    lines = validate.provider_errors(data)
    assert any("providers.stages.summary" in line and "is not a backend" in line for line in lines)
    assert any("providers.stages.deriver" in line and "is disabled" in line for line in lines)
    data["providers"]["stages"] = {"summary": "nope:model", "deriver": disabled}
    lines = validate.provider_errors(data)
    assert any("providers.stages.summary" in line and "is not a backend" in line for line in lines)
    assert any("providers.stages.deriver" in line and "is disabled" in line for line in lines)
    page = (ROOT / "src" / "dashboard" / "app" / "index.html").read_text(encoding="utf-8")
    assert 'effortSelect(stageEffort(task.stage), false, (v) => { writeStageEffort(task.stage, v); render(); }, 150, "provider default")' in page
    assert "effortSelect(backend[eff.key], eff.required, (v) => { if (v) backend[eff.key] = v; else delete backend[eff.key]; render(); })" in page


def test_validate_checks_stage_routes():
    data = json.loads((ROOT / "src" / "config" / "settings.example.json").read_text(encoding="utf-8"))
    backends = data["providers"]["backends"]
    disabled = next(name for name, block in backends.items() if block.get("enabled") is False)
    data["providers"]["stages"] = {"summary": "nope:model", "deriver": disabled}
    lines = validate.provider_errors(data)
    assert any("providers.stages.summary" in line and "is not a backend" in line for line in lines)
    assert any("providers.stages.deriver" in line and "is disabled" in line for line in lines)


def test_validate_names_a_key_and_allows_per_dream_fields():
    data = sample_settings()
    data["pairs"]["observer/observed"]["rerank"] = {"api_key": "sk-abc"}
    lines = validate.rerank_errors(data)
    assert len(lines) == 1
    assert "pairs.observer/observed.rerank.api_key" in lines[0]
    assert "sk-abc" not in lines[0]
    assert "rerank" in validate.ADDED_PEER_FIELDS
    diffs = validate.block_diffs(
        "defaults",
        {"max_conclusions": 4, "reasoning_effort": "medium"},
        {"brief": 4, "effort": "medium", "backend": None, "rerank": {"enabled": True}, "tool_calls": 10},
        {"backend"} | validate.PER_DREAM_FIELDS,
    )
    assert diffs == []


def test_daily_request_cap_is_shared_only_with_redis():
    text = " ".join((ROOT / "src" / "rerank" / "README.md").read_text(encoding="utf-8").split())
    sentence = (
        "The daily request cap is shared across processes only when Honcho's Redis cache is enabled; "
        "without it each process counts separately."
    )
    assert sentence in text


def test_key_env_must_name_a_provider_key():
    # The bridge refuses a key_env that does not end in _KEY; the schema refuses it first, so the dashboard cannot
    # save a backend that would then fail every call.
    data = valid_base()
    name = next(name for name, block in data["providers"]["backends"].items() if block.get("key_env"))
    data["providers"]["backends"][name]["key_env"] = "DEEPSEEK_API_KEY"
    assert errors(data) == []
    data["providers"]["backends"][name]["key_env"] = "POSTGRES_PASSWORD"
    assert any("POSTGRES_PASSWORD" in line for line in errors(data))
    data["providers"]["backends"][name]["key_env"] = "HONCHO_API_TOKEN"
    assert any("HONCHO_API_TOKEN" in line for line in errors(data))
