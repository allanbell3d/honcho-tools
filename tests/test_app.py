"""Dashboard tests. No network and no Honcho import.

The client talks to the FastAPI app in-process. Settings and instruction files
live in a temporary directory.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import sys
import time
import types
import urllib.error
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "src" / "dashboard" / "app" / "app.py"
AUTH = ("admin", "test-password")
AUTH_HEADER = {"Authorization": "Basic " + base64.b64encode(b"admin:test-password").decode()}

_spec = importlib.util.spec_from_file_location("control_app", APP_PATH)
dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dashboard)


@pytest.mark.parametrize("kind", ["openai-compat", "anthropic-api", "cli-codex", "cli-claude"])
def test_stage_model_override_without_allowlist(kind):
    data = {"providers": {"backends": {"provider": {"kind": kind, "model": "default"}},
                          "stages": {stage: "provider:selected" for stage in
                                     ("dream.deduction", "dream.induction", "peer_card")}}}
    assert dashboard.reference_errors(data) == []


def test_stage_model_override_respects_explicit_allowlist():
    data = {"providers": {"backends": {"provider": {"kind": "openai-compat", "model": "default", "models": ["default"]}},
                          "stages": {"dream.deduction": "provider:selected"}}}
    assert dashboard.reference_errors(data) == ["providers.stages.dream.deduction: selected is not in models"]


def test_stage_object_is_checked_like_the_string_form():
    # A stage entry may be {"model": "backend:model", "effort": ...}; its model gets the same checks and messages.
    backends = {"provider": {"kind": "openai-compat", "model": "default", "models": ["default"]}}
    as_string = {"providers": {"backends": backends, "stages": {"dream.deduction": "provider:selected", "summary": "absent:model"}}}
    as_object = {"providers": {"backends": backends, "stages": {"dream.deduction": {"model": "provider:selected", "effort": "high"},
                                                                   "summary": {"model": "absent:model", "effort": "low"}}}}
    expected = dashboard.reference_errors(as_string)
    assert expected == ["providers.stages.dream.deduction: selected is not in models",
                        "backend absent is not defined; referenced by providers.stages.summary"]
    assert dashboard.reference_errors(as_object) == expected
    fine = {"providers": {"backends": backends, "stages": {"summary": {"model": "provider:default", "effort": "low"}}}}
    assert dashboard.reference_errors(fine) == []


def test_broken_settings_file_returns_the_reason_and_the_page(client):
    # The page draws the pages that do not need settings.json from their own data and shows this reason elsewhere.
    app, path = client
    path.write_text("{not json", encoding="utf-8")
    body = app.get("/api/settings", auth=AUTH).json()
    assert body["settings"] is None and body["errors"] == ["invalid json"]
    assert app.get("/api/status", auth=AUTH).status_code == 200
    assert app.get("/", auth=AUTH).status_code == 200


def valid_settings() -> dict:
    return {
        "mode": "stock",
        "scheduler": {
            "poll_seconds": 15,
            "max_parallel": 2,
            "dream_type": "omni",
            "skip": [],
        },
        "providers": {
            "active": "deepseek",
            "concurrency": 2,
            "timeout_seconds": 30,
            "backends": {
                "codex": {
                    "kind": "cli-codex",
                    "model": "codex-model",
                    "effort": "high",
                    "enabled": True,
                },
                "deepseek": {
                    "kind": "openai-compat",
                    "base_url": "https://api.deepseek.example/v1",
                    "key_env": "DEEPSEEK_API_KEY",
                    "model": "ds-test",
                    "thinking_effort": "medium",
                    "enabled": True,
                },
                "openrouter": {
                    "kind": "openai-compat",
                    "base_url": "https://openrouter.ai/api/v1",
                    "key_env": "OPENROUTER_API_KEY",
                    "model": "or-default",
                    "models": ["or-default"],
                    "enabled": True,
                },
            },
        },
        "defaults": {
            "brief": 4,
            "deduction_effort": "medium",
            "induction_effort": "medium",
            "deduction_rounds": 2,
            "induction_rounds": 3,
            "backend": None,
        },
        "peers": {
            "observer": {"enabled": True, "brief": 8, "backend": None},
        },
        "pairs": {
            "observer/observed": {"brief": 11},
        },
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(valid_settings(), indent=2) + "\n", encoding="utf-8")
    monkeypatch.setenv("CONTROL_USER", "admin")
    monkeypatch.setenv("CONTROL_PASSWORD", "test-password")
    monkeypatch.setenv("CONTROL_FILE", str(path))
    monkeypatch.setenv("INSTRUCTIONS_DIR", str(tmp_path / "instructions"))
    monkeypatch.setenv("POLICY_LOG", str(tmp_path / "policy-used.log"))
    monkeypatch.setenv("SCHEDULER_LOG", str(tmp_path / "dream-nas.log"))
    monkeypatch.setenv("TELEMETRY_DIR", str(tmp_path / "telemetry"))
    monkeypatch.delenv("HONCHO_WORKSPACE", raising=False)
    monkeypatch.delenv("HONCHO_API_TOKEN", raising=False)
    monkeypatch.delenv("INSTRUCTION_TOKEN_CAP", raising=False)
    monkeypatch.delenv("DOCKER_PROXY_URL", raising=False)
    monkeypatch.setenv("RESTART_LOG", str(tmp_path / "restart.log"))
    return TestClient(dashboard.app, headers=AUTH_HEADER), path


def test_password_set_means_login(client):
    app, _path = client
    refused = TestClient(dashboard.app).get("/")
    assert refused.status_code == 401
    assert refused.headers["WWW-Authenticate"].startswith("Basic")
    assert app.get("/api/settings", auth=("admin", "wrong")).status_code == 401
    response = app.get("/")
    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "sk-" not in response.text


def test_blank_or_unset_password_opens_the_page(client, monkeypatch):
    _app, _path = client
    monkeypatch.setenv("CONTROL_PASSWORD", "  ")
    response = TestClient(dashboard.app).get("/")
    assert response.status_code == 200 and "WWW-Authenticate" not in response.headers
    monkeypatch.delenv("CONTROL_PASSWORD")
    assert TestClient(dashboard.app).get("/api/settings").status_code == 200


def test_writes_must_be_json(client):
    # A cross-site form or no-cors fetch can send text/plain or no body; neither reaches a route.
    app, path = client
    before = path.read_text(encoding="utf-8")
    body = json.dumps({"settings": valid_settings(), "apply": True})
    plain = app.post("/api/settings", content=body, headers={"Content-Type": "text/plain"})
    assert plain.status_code == 415
    assert app.post("/api/stop/telemetry").status_code == 415
    assert path.read_text(encoding="utf-8") == before


def test_nan_and_deep_nesting_are_refused_not_written(client):
    app, path = client
    before = path.read_text(encoding="utf-8")
    nan = '{"settings": {"mode": "stock", "x": NaN}, "apply": true}'
    assert app.post("/api/settings", content=nan, headers={"Content-Type": "application/json"}).status_code == 400
    deep = "[" * 100000
    assert app.post("/api/settings", content=deep, headers={"Content-Type": "application/json"}).status_code == 400
    assert path.read_text(encoding="utf-8") == before


def test_manually_added_model_can_be_saved_for_stages_without_allowlist(client, tmp_path, monkeypatch):
    app, path = client
    monkeypatch.setenv("MODELS_FILE", str(tmp_path / "models.json"))
    entry = {"vendor": "DeepSeek", "label": "New custom model", "backend": "deepseek", "model": "custom-model"}
    assert app.post("/api/models/add", json=entry).status_code == 200
    data = app.get("/api/settings").json()
    data["settings"]["providers"]["stages"] = {
        stage: "deepseek:custom-model" for stage in ("dream.deduction", "dream.induction", "peer_card")
    }
    response = app.post("/api/settings", json={**data, "apply": True})
    assert response.status_code == 200, response.text
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["providers"]["stages"] == data["settings"]["providers"]["stages"]
    assert "models" not in saved["providers"]["backends"]["deepseek"]


def test_get_renders(client):
    app, _path = client
    response = app.get("/", auth=AUTH)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    for name in ("Overview", "Models", "Peers", "Pairs", "Instructions", "Logs", "Advanced"):
        assert name in response.text
    assert "client addresses" not in response.text
    assert "Clear selection" in response.text
    assert "scheduling off (overlays still apply)" in response.text
    assert "CLI: no token cap, no sampling, no prompt caching" in response.text
    assert "Start from the All peers text" in response.text
    assert "Honcho's prompt only" in response.text
    assert "Default (Honcho's built-in)" not in response.text
    assert "cdn" not in response.text.lower()
    assert "<script src" not in response.text


def test_docs_are_off(client):
    app, _path = client
    assert app.get("/docs", auth=AUTH).status_code == 404


def test_invalid_save_rejected(client):
    app, path = client
    before = path.read_bytes()
    body = valid_settings()
    body["mode"] = "both"
    response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert response.status_code == 400
    assert path.read_bytes() == before
    assert list(path.parent.glob("settings.json.bak-*")) == []
    assert list(path.parent.glob("settings.json.tmp-*")) == []


def test_secret_rejected(client):
    app, path = client
    before = path.read_bytes()
    secret = "sk-live-secret"
    body = valid_settings()
    body["providers"]["backends"]["codex"]["model"] = secret
    response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert response.status_code == 400
    assert secret not in response.text
    assert "sk-" not in response.text
    assert path.read_bytes() == before
    assert list(path.parent.glob("settings.json.bak-*")) == []


def test_preview_does_not_write(client):
    app, path = client
    body = valid_settings()
    body["mode"] = "scheduler"
    response = app.post("/api/settings", json={"settings": body, "apply": False}, auth=AUTH)
    assert response.status_code == 200
    payload = response.json()
    assert payload["changed"] is True
    assert payload["bak"] is False
    assert any(row["path"] == "/mode" for row in payload["diff"])
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "stock"
    assert list(path.parent.glob("settings.json.bak-*")) == []


def test_valid_save_writes_bak(client):
    app, path = client
    body = valid_settings()
    body["defaults"]["brief"] = 12
    response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert response.status_code == 200
    assert response.json()["bak"] is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["defaults"]["brief"] == 12
    baks = list((path.parent / "backups" / "settings").glob("settings.json.bak_*"))
    assert len(baks) == 1
    assert list(path.parent.glob("settings.json.bak-*")) == []
    assert json.loads(baks[0].read_text(encoding="utf-8"))["defaults"]["brief"] == 4
    assert list(path.parent.glob("settings.json.tmp-*")) == []


def test_bak_rotation_keeps_ten_and_recent(client):
    app, path = client
    folder = path.parent / "backups" / "settings"
    for number in range(1, 14):
        body = valid_settings()
        body["defaults"]["brief"] = number
        response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
        assert response.status_code == 200
    # Every copy is younger than 7 days, so all 13 stay.
    baks = list(folder.glob("settings.json.bak_*"))
    assert len(baks) == 13
    assert all(dashboard.backups.is_bak_name(item.name) for item in baks)
    old = time.time() - 8 * 86400
    for item in baks:
        os.utime(item, (old, old))
    keep = folder / "notes.txt"
    keep.write_text("not a copy\n", encoding="utf-8")
    body = valid_settings()
    body["defaults"]["brief"] = 99
    assert app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH).status_code == 200
    assert len(list(folder.glob("settings.json.bak_*"))) == 10
    assert keep.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["defaults"]["brief"] == 99
    assert list(path.parent.glob("settings.json.tmp-*")) == []


def test_restore_round_trip(client):
    app, path = client
    body = valid_settings()
    body["defaults"]["brief"] = 9
    assert app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH).status_code == 200
    name = next((path.parent / "backups" / "settings").glob("settings.json.bak_*")).name
    preview = app.post("/api/history/restore", json={"name": name, "apply": False}, auth=AUTH)
    assert preview.status_code == 200
    assert preview.json()["changed"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["defaults"]["brief"] == 9
    applied = app.post("/api/history/restore", json={"name": name, "apply": True}, auth=AUTH)
    assert applied.status_code == 200
    assert json.loads(path.read_text(encoding="utf-8"))["defaults"]["brief"] == 4


def test_missing_instruction_folder_is_not_an_error(client):
    app, _path = client
    response = app.get("/api/instructions", auth=AUTH)
    assert response.status_code == 200
    payload = response.json()
    assert payload["folder"] is False
    assert payload["cap"] == 2000
    assert payload["rows"]
    assert all(row["resolution"] == "database" for row in payload["rows"])
    assert all(row["source"] == "database" for row in payload["rows"])
    assert payload["workspace_error"] == "HONCHO_WORKSPACE is unset"
    assert all(payload["database"][kind] is None for kind in ("dream", "peer-card", "reasoning", "summary", "dialectic"))


def _instruction_rows(payload):
    return {(row["scope"], row["name"], row["category"]): row for row in payload["rows"]}


def test_instruction_resolution(client, tmp_path):
    app, _path = client
    directory = tmp_path / "instructions"
    (directory / "default").mkdir(parents=True)
    (directory / "default" / "dream.md").write_text("default dream\n", encoding="utf-8")
    (directory / "backends" / "codex").mkdir(parents=True)
    (directory / "backends" / "codex" / "dream.md").write_text("codex dream\n", encoding="utf-8")
    (directory / "backends" / "codex" / "peer-card.md").write_text("   \n", encoding="utf-8")
    (directory / "openrouter-dream-instructions.md").write_text("flat router\n", encoding="utf-8")
    response = app.get("/api/instructions", auth=AUTH)
    assert response.status_code == 200
    rows = _instruction_rows(response.json())
    assert rows[("backend", "codex", "dream")]["resolution"] == "backend"
    assert rows[("backend", "codex", "dream")]["source"] == "settings.backend.codex.dream"
    assert rows[("backend", "codex", "dream")]["text"] == "codex dream\n"
    assert rows[("backend", "deepseek", "dream")]["resolution"] == "default"
    assert rows[("backend", "deepseek", "dream")]["source"] == "settings.default.dream"
    assert rows[("backend", "codex", "peer-card")]["resolution"] == "backend"
    assert rows[("backend", "codex", "peer-card")]["empty"] is True
    assert rows[("backend", "codex", "peer-card")]["source"] == "settings.backend.codex.peer-card"
    assert rows[("backend", "deepseek", "peer-card")]["resolution"] == "database"
    assert rows[("default", None, "dream")]["resolution"] == "default"
    assert rows[("default", None, "peer-card")]["resolution"] == "database"
    flat = rows[("backend", "openrouter", "dream")]
    assert flat["resolution"] == "backend"
    assert flat["source"] == "settings.backend.openrouter.dream"
    assert flat["text"] == "flat router\n"
    assert flat["file"] == "openrouter-dream-instructions.md"


def test_instruction_save_and_oversize(client, tmp_path, monkeypatch):
    app, _path = client
    monkeypatch.setenv("INSTRUCTION_TOKEN_CAP", "2")
    directory = tmp_path / "instructions"
    target = directory / "backends" / "codex" / "dream.md"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    text = "x" * 12
    body = {"scope": "backend", "name": "codex", "category": "dream", "text": text}
    preview = app.post("/api/instructions", json={**body, "apply": False}, auth=AUTH)
    assert preview.status_code == 200
    assert preview.json()["over"] is True
    assert preview.json()["tokens"] == 3
    assert target.read_text(encoding="utf-8") == "old\n"
    applied = app.post("/api/instructions", json={**body, "apply": True}, auth=AUTH)
    assert applied.status_code == 200
    assert target.read_text(encoding="utf-8") == text
    assert list(target.parent.glob("dream.md.bak_*")) == []
    baks = list((tmp_path / "backups" / "instructions" / "backends" / "codex").glob("dream.md.bak_*"))
    assert len(baks) == 1
    assert baks[0].read_text(encoding="utf-8") == "old\n"
    assert list(directory.glob("*.tmp-*")) == []


def test_create_from_default(client, tmp_path):
    app, settings = client
    before = settings.read_bytes()
    directory = tmp_path / "instructions"
    (directory / "default").mkdir(parents=True)
    (directory / "default" / "dream.md").write_text("from default\n", encoding="utf-8")
    preview = app.post(
        "/api/instructions/from-default",
        json={"scope": "backend", "name": "deepseek", "category": "dream", "apply": False},
        auth=AUTH,
    )
    assert preview.status_code == 200
    assert preview.json()["text"] == "from default\n"
    assert not (directory / "backends" / "deepseek" / "dream.md").exists()
    applied = app.post(
        "/api/instructions/from-default",
        json={"scope": "backend", "name": "deepseek", "category": "dream", "apply": True},
        auth=AUTH,
    )
    assert applied.status_code == 200
    assert (directory / "backends" / "deepseek" / "dream.md").read_text(encoding="utf-8") == "from default\n"
    assert settings.read_bytes() == before
    missing = app.post(
        "/api/instructions/from-default",
        json={"scope": "backend", "name": "deepseek", "category": "peer-card", "apply": True},
        auth=AUTH,
    )
    assert missing.status_code == 404


def test_instruction_secret_rejected(client, tmp_path):
    app, _path = client
    directory = tmp_path / "instructions"
    directory.mkdir()
    secret = "sk-instruction"
    response = app.post(
        "/api/instructions",
        json={"scope": "backend", "name": "codex", "category": "dream", "text": secret, "apply": True},
        auth=AUTH,
    )
    assert response.status_code == 400
    assert secret not in response.text
    assert "sk-" not in response.text
    assert not (directory / "backends" / "codex" / "dream.md").exists()
    assert not (directory / "codex-dream-instructions.md").exists()


def test_add_backend(client):
    app, path = client
    before = json.loads(path.read_text(encoding="utf-8"))
    body = valid_settings()
    body["providers"]["backends"]["glm"] = {
        "kind": "openai-compat",
        "base_url": "https://api.glm.example/v1",
        "key_env": "GLM_API_KEY",
        "model": "glm-test",
        "enabled": True,
    }
    preview = app.post("/api/settings", json={"settings": body, "apply": False}, auth=AUTH)
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["changed"] is True
    assert any(row["path"] == "/providers/backends/glm" and row["op"] == "add" for row in payload["diff"])
    assert json.loads(path.read_text(encoding="utf-8")) == before
    applied = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert applied.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["providers"]["backends"]["glm"]["model"] == "glm-test"


def test_change_between_check_and_replace_is_not_overwritten(client, monkeypatch):
    app, path = client
    intruder = json.dumps({"mode": "scheduler", "marker": "kept"}) + "\n"
    real_fsync = os.fsync

    def plant(fd):
        real_fsync(fd)
        path.write_text(intruder, encoding="utf-8")
        stamp = path.stat().st_mtime_ns + 1_000_000_000
        os.utime(path, ns=(stamp, stamp))

    monkeypatch.setattr(dashboard.os, "fsync", plant)
    body = valid_settings()
    body["mode"] = "scheduler"
    response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert response.status_code == 409
    assert response.json()["detail"] == "the file changed; reload and review the diff again"
    assert path.read_text(encoding="utf-8") == intruder


def test_restore_with_stale_mtime_is_not_overwritten(client, monkeypatch):
    app, path = client
    body = valid_settings()
    body["defaults"]["brief"] = 9
    assert app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH).status_code == 200
    name = next((path.parent / "backups" / "settings").glob("settings.json.bak_*")).name
    loaded = app.get("/api/settings", auth=AUTH).json()["mtime_ns"]
    intruder = json.dumps({"mode": "scheduler", "marker": "kept"}) + "\n"
    real_fsync = os.fsync

    def plant(fd):
        real_fsync(fd)
        path.write_text(intruder, encoding="utf-8")
        stamp = path.stat().st_mtime_ns + 1_000_000_000
        os.utime(path, ns=(stamp, stamp))

    monkeypatch.setattr(dashboard.os, "fsync", plant)
    response = app.post(
        "/api/history/restore",
        json={"name": name, "apply": True, "mtime_ns": loaded},
        auth=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "the file changed; reload and review the diff again"
    assert path.read_text(encoding="utf-8") == intruder


def test_edit_backend_models_and_enabled(client):
    app, path = client
    body = valid_settings()
    body["providers"]["backends"]["openrouter"]["models"] = ["or-default", "or-extra"]
    body["providers"]["backends"]["codex"]["enabled"] = False
    response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert response.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["providers"]["backends"]["openrouter"]["models"] == ["or-default", "or-extra"]
    assert saved["providers"]["backends"]["codex"]["enabled"] is False


def test_remove_backend(client):
    app, path = client
    body = valid_settings()
    del body["providers"]["backends"]["openrouter"]
    preview = app.post("/api/settings", json={"settings": body, "apply": False}, auth=AUTH)
    assert preview.status_code == 200
    assert any(row["path"] == "/providers/backends/openrouter" and row["op"] == "remove" for row in preview.json()["diff"])
    assert "openrouter" in json.loads(path.read_text(encoding="utf-8"))["providers"]["backends"]
    applied = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert applied.status_code == 200
    assert "openrouter" not in json.loads(path.read_text(encoding="utf-8"))["providers"]["backends"]


def test_remove_backend_refused(client):
    app, path = client
    before = path.read_bytes()
    body = valid_settings()
    del body["providers"]["backends"]["deepseek"]
    body["providers"]["active"] = "deepseek"
    body["defaults"]["backend"] = "deepseek"
    body["peers"]["observer"]["backend"] = "deepseek"
    body["pairs"]["observer/observed"]["backend"] = "deepseek"
    response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert response.status_code == 400
    for where in (
        "providers.active",
        "defaults.backend",
        "peers.observer.backend",
        "pairs.observer/observed.backend",
    ):
        assert where in response.text
    assert path.read_bytes() == before


def test_peer_file_beats_backend(client, tmp_path):
    app, path = client
    body = valid_settings()
    body["peers"]["observer"]["backend"] = "codex"
    body["peers"]["observed"] = {"enabled": True}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    directory = tmp_path / "instructions"
    (directory / "backends" / "codex").mkdir(parents=True)
    (directory / "backends" / "codex" / "dream.md").write_text("backend dream\n", encoding="utf-8")
    (directory / "default").mkdir()
    (directory / "default" / "dream.md").write_text("default dream\n", encoding="utf-8")
    (directory / "peers" / "observed").mkdir(parents=True)
    (directory / "peers" / "observed" / "dream.md").write_text("observed dream\n", encoding="utf-8")
    rows = _instruction_rows(app.get("/api/instructions", auth=AUTH).json())
    assert rows[("peer", "observer", "dream")]["resolution"] == "backend"
    assert rows[("peer", "observer", "dream")]["source"] == "settings.backend.codex.dream"
    (directory / "peers" / "observer").mkdir()
    (directory / "peers" / "observer" / "dream.md").write_text("observer dream\n", encoding="utf-8")
    rows = _instruction_rows(app.get("/api/instructions", auth=AUTH).json())
    assert rows[("peer", "observer", "dream")]["resolution"] == "peer"
    assert rows[("peer", "observer", "dream")]["source"] == "settings.peer.observer.dream"
    assert rows[("peer", "observer", "dream")]["text"] == "observer dream\n"
    assert rows[("backend", "codex", "dream")]["resolution"] == "backend"
    assert rows[("backend", "codex", "dream")]["text"] == "backend dream\n"
    assert rows[("peer", "observed", "dream")]["source"] == "settings.peer.observed.dream"


def test_create_from_backend(client, tmp_path):
    app, settings = client
    before = settings.read_bytes()
    directory = tmp_path / "instructions"
    (directory / "backends" / "codex").mkdir(parents=True)
    (directory / "backends" / "codex" / "dream.md").write_text("from backend\n", encoding="utf-8")
    preview = app.post(
        "/api/instructions/from-backend",
        json={"peer": "observer", "backend": "codex", "category": "dream", "apply": False},
        auth=AUTH,
    )
    assert preview.status_code == 200
    assert preview.json()["text"] == "from backend\n"
    dest = directory / "peers" / "observer" / "dream.md"
    assert not dest.exists()
    applied = app.post(
        "/api/instructions/from-backend",
        json={"peer": "observer", "backend": "codex", "category": "dream", "apply": True},
        auth=AUTH,
    )
    assert applied.status_code == 200
    assert dest.read_text(encoding="utf-8") == "from backend\n"
    assert settings.read_bytes() == before
    missing = app.post(
        "/api/instructions/from-backend",
        json={"peer": "observer", "backend": "codex", "category": "peer-card", "apply": True},
        auth=AUTH,
    )
    assert missing.status_code == 404


def test_migrate_instructions(tmp_path):
    directory = tmp_path / "instructions"
    directory.mkdir()
    (directory / "default-dream-instructions.md").write_text("dream\n", encoding="utf-8")
    (directory / "codex-dream-instructions.md").write_text("codex\n", encoding="utf-8")
    (directory / "default").mkdir()
    (directory / "default" / "peer-card.md").write_text("kept\n", encoding="utf-8")
    (directory / "default-peer-card-instructions.md").write_text("old card\n", encoding="utf-8")
    (directory / "default-dream-instructions.md.bak-20260101T000000000000000").write_text("bak\n", encoding="utf-8")
    lines = dashboard.migrate_instructions(directory)
    assert (directory / "default" / "dream.md").read_text(encoding="utf-8") == "dream\n"
    assert not (directory / "default-dream-instructions.md").exists()
    assert (directory / "backends" / "codex" / "dream.md").read_text(encoding="utf-8") == "codex\n"
    assert (directory / "default-peer-card-instructions.md").read_text(encoding="utf-8") == "old card\n"
    assert (directory / "default" / "peer-card.md").read_text(encoding="utf-8") == "kept\n"
    assert (directory / "default-dream-instructions.md.bak-20260101T000000000000000").read_text(encoding="utf-8") == "bak\n"
    assert "migrated default-dream-instructions.md -> default/dream.md" in lines
    assert "migrated codex-dream-instructions.md -> backends/codex/dream.md" in lines
    assert "instruction migration skipped default-peer-card-instructions.md because default/peer-card.md exists" in lines


def test_migration_on_start(tmp_path, monkeypatch):
    directory = tmp_path / "instructions"
    directory.mkdir()
    (directory / "default-dream-instructions.md").write_text("dream\n", encoding="utf-8")
    (directory / "codex-peer-card-instructions.md").write_text("card\n", encoding="utf-8")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(valid_settings()) + "\n", encoding="utf-8")
    monkeypatch.setenv("CONTROL_USER", "admin")
    monkeypatch.setenv("CONTROL_PASSWORD", "test-password")
    monkeypatch.setenv("CONTROL_FILE", str(path))
    monkeypatch.setenv("INSTRUCTIONS_DIR", str(directory))
    with TestClient(dashboard.app) as app:
        response = app.get("/api/instructions", auth=AUTH)
        assert response.status_code == 200
    assert (directory / "default" / "dream.md").read_text(encoding="utf-8") == "dream\n"
    assert not (directory / "default-dream-instructions.md").exists()
    assert (directory / "backends" / "codex" / "peer-card.md").read_text(encoding="utf-8") == "card\n"
    assert not (directory / "codex-peer-card-instructions.md").exists()


def _workspace_store():
    return {
        "configuration": {
            "reasoning": {"custom_instructions": "old reason"},
            "summary": {"enabled": True},
            "custom_instructions": "shared text",
        }
    }


def test_workspace_instructions(client, tmp_path, monkeypatch):
    app, _path = client
    monkeypatch.setenv("HONCHO_WORKSPACE", "ws")
    monkeypatch.setenv("HONCHO_API_TOKEN", "token-value")
    directory = tmp_path / "instructions"
    directory.mkdir()
    store = _workspace_store()
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST" and path == "/workspaces":
            return {"id": "ws", "configuration": json.loads(json.dumps(store["configuration"]))}, None
        if method == "PUT" and path == "/workspaces/ws":
            store["configuration"] = body["configuration"]
            return {"id": "ws", "configuration": store["configuration"]}, None
        return None, "Honcho API unavailable"

    monkeypatch.setattr(dashboard, "api_json", fake)
    preview = app.post(
        "/api/instructions/workspace",
        json={"kind": "reasoning", "text": "new reason", "apply": False},
        auth=AUTH,
    )
    assert preview.status_code == 200
    assert preview.json()["changed"] is True
    assert preview.json()["workspace"] is True
    assert not any(item[0] == "PUT" for item in calls)
    assert list((tmp_path / "backups" / "workspace").glob("workspace-config.json.bak_*")) == []
    applied = app.post(
        "/api/instructions/workspace",
        json={"kind": "reasoning", "text": "new reason", "apply": True},
        auth=AUTH,
    )
    assert applied.status_code == 200
    assert applied.json()["bak"] is True
    assert store["configuration"]["reasoning"]["custom_instructions"] == "new reason"
    assert store["configuration"]["summary"]["enabled"] is True
    put = [item for item in calls if item[0] == "PUT"][-1]
    assert put[2]["configuration"]["summary"]["enabled"] is True
    assert put[2]["configuration"]["reasoning"]["custom_instructions"] == "new reason"
    baks = list((tmp_path / "backups" / "workspace").glob("workspace-config.json.bak_*"))
    assert len(baks) == 1
    previous = json.loads(baks[0].read_text(encoding="utf-8"))
    assert previous["reasoning"]["custom_instructions"] == "old reason"
    assert previous["summary"]["enabled"] is True
    listed = app.get("/api/instructions", auth=AUTH)
    assert listed.json()["database"]["reasoning"] == "new reason"
    for kind in ("dream", "peer-card"):
        refused = app.post(
            "/api/instructions/workspace",
            json={"kind": kind, "text": "nope", "apply": False},
            auth=AUTH,
        )
        assert refused.status_code == 400
    monkeypatch.setenv("INSTRUCTION_TOKEN_CAP", "1")
    over = app.post(
        "/api/instructions/workspace",
        json={"kind": "reasoning", "text": "x" * 8, "apply": False},
        auth=AUTH,
    )
    assert over.status_code == 200
    assert over.json()["over"] is True
    denied = app.post(
        "/api/instructions/workspace",
        json={"kind": "reasoning", "text": "x" * 8, "apply": True},
        auth=AUTH,
    )
    assert denied.status_code == 400
    assert "custom instructions are over the cap" in denied.text
    assert store["configuration"]["reasoning"]["custom_instructions"] == "new reason"
    assert len(list((tmp_path / "backups" / "workspace").glob("workspace-config.json.bak_*"))) == 1


def test_workspace_missing_empty_and_set(client, tmp_path, monkeypatch):
    app, _path = client
    monkeypatch.setenv("HONCHO_WORKSPACE", "ws")
    monkeypatch.setenv("HONCHO_API_TOKEN", "token-value")
    (tmp_path / "instructions").mkdir()
    store = {"configuration": {"reasoning": {"custom_instructions": "kept"}, "summary": {"custom_instructions": ""}, "dream": {"enabled": True}}}
    calls = []

    def fake(method, path, body=None):
        calls.append(method)
        if method == "POST" and path == "/workspaces":
            return {"id": "ws", "configuration": json.loads(json.dumps(store["configuration"]))}, None
        if method == "PUT" and path == "/workspaces/ws":
            # Honcho merges top-level keys; it never drops one.
            store["configuration"] = {**store["configuration"], **body["configuration"]}
            return {"id": "ws", "configuration": store["configuration"]}, None
        return None, "Honcho API unavailable"

    monkeypatch.setattr(dashboard, "api_json", fake)
    listed = app.get("/api/instructions", auth=AUTH).json()
    assert listed["database_state"]["reasoning"] == "set"
    assert listed["database_state"]["summary"] == "empty"
    assert listed["database_state"]["dialectic"] == "missing"

    def post(body):
        response = app.post("/api/instructions/workspace", json=dict(body, apply=True), auth=AUTH)
        assert response.status_code == 200
        return response.json()

    # Saving each kind as it is shown writes nothing.
    assert post({"kind": "reasoning", "text": "kept"})["changed"] is False
    assert post({"kind": "summary", "text": ""})["changed"] is False
    assert post({"kind": "dialectic", "unset": True})["changed"] is False
    assert "PUT" not in calls
    assert "dialectic" not in store["configuration"]

    # Empty on a missing kind is an explicit none; unset removes the key and leaves an empty block.
    assert post({"kind": "dialectic", "text": ""})["changed"] is True
    assert store["configuration"]["dialectic"] == {"custom_instructions": ""}
    assert post({"kind": "dialectic", "unset": True})["changed"] is True
    assert store["configuration"]["dialectic"] == {}
    assert app.get("/api/instructions", auth=AUTH).json()["database_state"]["dialectic"] == "missing"
    assert post({"kind": "dialectic", "unset": True})["changed"] is False
    assert post({"kind": "reasoning", "unset": True})["changed"] is True
    assert store["configuration"]["reasoning"] == {}
    assert store["configuration"]["dream"] == {"enabled": True}


def test_workspace_bak_rotation(client, tmp_path, monkeypatch):
    app, _path = client
    monkeypatch.setenv("HONCHO_WORKSPACE", "ws")
    monkeypatch.setenv("HONCHO_API_TOKEN", "token-value")
    directory = tmp_path / "instructions"
    directory.mkdir()
    store = _workspace_store()

    def fake(method, path, body=None):
        if method == "POST" and path == "/workspaces":
            return {"id": "ws", "configuration": json.loads(json.dumps(store["configuration"]))}, None
        if method == "PUT" and path == "/workspaces/ws":
            store["configuration"] = body["configuration"]
            return {"id": "ws", "configuration": store["configuration"]}, None
        return None, "Honcho API unavailable"

    monkeypatch.setattr(dashboard, "api_json", fake)
    for number in range(21):
        response = app.post(
            "/api/instructions/workspace",
            json={"kind": "reasoning", "text": f"reason {number}", "apply": True},
            auth=AUTH,
        )
        assert response.status_code == 200
    assert len(list((tmp_path / "backups" / "workspace").glob("workspace-config.json.bak_*"))) == 21
    assert list(directory.glob("*.tmp-*")) == []


def test_status_reads_local_logs(client, tmp_path):
    app, _path = client
    (tmp_path / "policy-used.log").write_text("policy line\n", encoding="utf-8")
    (tmp_path / "dream-nas.log").write_text("scheduler line\n", encoding="utf-8")
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stamp = "2026-10-30T00:00:00+00:00"
    line = json.dumps({"received_at": stamp, "event": {"id": "1"}})
    (telemetry / f"events-{day}.jsonl").write_text(line + "\n", encoding="utf-8", newline="\n")
    response = app.get("/api/status", auth=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["queue_error"] == "HONCHO_WORKSPACE is unset"
    assert body["queue"] is None
    assert body["policy_log"] == ["policy line"]
    assert body["scheduler_log"] == ["scheduler line"]
    assert body["telemetry"]["bytes"] == len(line) + 1
    assert body["telemetry"]["last_event"] == stamp


def test_peers_without_workspace(client):
    app, _path = client
    response = app.get("/api/peers", auth=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] == ["observer"]
    assert body["discovered"] == []
    assert body["error"] == "HONCHO_WORKSPACE is unset"


# Documentation range (RFC 5737). Not a host on this network.
ALLOWED_IP = "192.0.2.1"
PROXY_URL = "http://docker-proxy:2375"
STARTED_AT = "2026-10-30T01:02:03.000000000Z"


class _ProxyResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ProxyRecorder:
    def __init__(self, containers, started_at=STARTED_AT):
        self.containers = containers
        self.started_at = started_at
        self.calls = []

    def __call__(self, url, data=None, timeout=None, **kwargs):
        method = url.get_method() if hasattr(url, "get_method") else "GET"
        full = url.full_url if hasattr(url, "full_url") else str(url)
        self.calls.append({"method": method, "url": full, "timeout": timeout})
        if method == "GET" and "/containers/json" in full:
            return _ProxyResponse(200, json.dumps(self.containers).encode("utf-8"))
        if method == "GET" and full.endswith("/json"):
            body = {"State": {"Status": "running", "StartedAt": self.started_at}}
            return _ProxyResponse(200, json.dumps(body).encode("utf-8"))
        if method == "POST":
            return _ProxyResponse(204, b"")
        raise AssertionError(method + " " + full)


def restart_client(host: str) -> TestClient:
    return TestClient(dashboard.app, client=(host, 50000))


def arm_proxy(monkeypatch, tmp_path, names, containers=None):
    """Replace the constant container list for one test, and record proxy calls."""
    monkeypatch.setattr(dashboard, "RESTART_ALLOWED", tuple(names))
    monkeypatch.setenv("DOCKER_PROXY_URL", PROXY_URL)
    recorder = ProxyRecorder([] if containers is None else containers)
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", recorder)
    return names, recorder


SAMPLE_CONTAINERS = [
    {"Names": ["/telemetry"], "State": "running", "Status": "Up 2 hours"},
    {"Names": ["/backup"], "State": "exited", "Status": "Exited (0) 1 hour ago"},
]


def test_restart_allowed_name(client, tmp_path, monkeypatch):
    _app, _settings = client
    _allow, recorder = arm_proxy(monkeypatch, tmp_path, ["telemetry"])
    app = restart_client(ALLOWED_IP)
    for action in ("restart", "stop", "start"):
        response = app.post("/api/" + action + "/telemetry", json={}, auth=AUTH)
        assert response.status_code == 200
        assert response.json()["action"] == action
        assert response.json()["name"] == "telemetry"
    assert [call["url"] for call in recorder.calls] == [
        PROXY_URL + "/containers/telemetry/restart?t=10",
        PROXY_URL + "/containers/telemetry/stop?t=10",
        PROXY_URL + "/containers/telemetry/start?t=10",
    ]
    assert [call["method"] for call in recorder.calls] == ["POST", "POST", "POST"]
    assert [call["timeout"] for call in recorder.calls] == [60, 60, 60]
    log = (tmp_path / "restart.log").read_text(encoding="utf-8")
    assert "ip=" + ALLOWED_IP in log
    assert "name=telemetry" in log
    assert "action=restart" in log
    assert "result=ok" in log


def test_restart_name_outside_allow_list(client, tmp_path, monkeypatch):
    _app, _settings = client
    _allow, recorder = arm_proxy(monkeypatch, tmp_path, ["telemetry"])
    app = restart_client(ALLOWED_IP)
    response = app.post("/api/restart/other", json={}, auth=AUTH)
    assert response.status_code == 403
    assert response.json()["detail"] == "container is not in the allow list"
    assert recorder.calls == []


def test_restart_list_is_a_constant(client, tmp_path, monkeypatch):
    _app, _settings = client
    assert dashboard.RESTART_ALLOWED == (
        "honcho-api-1",
        "honcho-deriver-1",
        "honcho-tools-bridge-1",
        "honcho-tools-scheduler-1",
        "honcho-tools-telemetry-1",
        "honcho-tools-backup-1",
        "honcho-tools-dashboard-1",
    )
    assert dashboard.service_container(dashboard.allow_names(), "api") == "honcho-api-1"
    assert dashboard.service_container(dashboard.allow_names(), "deriver") == "honcho-deriver-1"
    # No file is read: a restart-allow.json beside settings.json changes nothing.
    (tmp_path / "restart-allow.json").write_text(json.dumps({"containers": ["telemetry"]}), encoding="utf-8")
    monkeypatch.setenv("RESTART_ALLOW_FILE", str(tmp_path / "restart-allow.json"))
    monkeypatch.setenv("DOCKER_PROXY_URL", PROXY_URL)
    recorder = ProxyRecorder([])
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", recorder)
    app = restart_client(ALLOWED_IP)
    refused = app.post("/api/restart/telemetry", json={}, auth=AUTH)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "container is not in the allow list"
    allowed = app.post("/api/restart/honcho-tools-bridge-1", json={}, auth=AUTH)
    assert allowed.status_code == 200
    assert [call["url"] for call in recorder.calls] == [PROXY_URL + "/containers/honcho-tools-bridge-1/restart?t=10"]
    assert not hasattr(dashboard, "restart_allow_path")
    assert not hasattr(dashboard, "load_restart_allow")


def test_restart_refused_without_login(client, tmp_path, monkeypatch):
    _app, _settings = client
    _allow, recorder = arm_proxy(monkeypatch, tmp_path, ["telemetry"])
    app = restart_client(ALLOWED_IP)
    response = app.post("/api/restart/telemetry", json={})
    assert response.status_code == 401
    assert recorder.calls == []


def test_containers_from_proxy(client, tmp_path, monkeypatch):
    _app, _settings = client
    _allow, recorder = arm_proxy(
        monkeypatch,
        tmp_path,
        ["telemetry", "not-here"],
        SAMPLE_CONTAINERS,
    )
    app = restart_client(ALLOWED_IP)
    response = app.get("/api/containers", auth=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] == ["telemetry", "not-here"]
    assert body["read_only"] is False
    assert "error" not in body
    rows = {item["name"]: item for item in body["containers"]}
    assert set(rows) == {"telemetry", "not-here"}
    assert rows["telemetry"]["state"] == "running"
    assert rows["telemetry"]["uptime"] == "Up 2 hours"
    assert rows["telemetry"]["started_at"] == STARTED_AT
    assert rows["not-here"]["state"] == "unknown"
    assert rows["not-here"]["started_at"] is None
    assert rows["not-here"]["uptime"] is None
    assert any("/containers/json?all=1" in call["url"] for call in recorder.calls)
    assert not any(call["method"] == "POST" for call in recorder.calls)


def test_containers_proxy_unset(client, tmp_path, monkeypatch):
    _app, _settings = client
    monkeypatch.setattr(dashboard, "RESTART_ALLOWED", ("telemetry",))
    monkeypatch.delenv("DOCKER_PROXY_URL", raising=False)
    recorder = ProxyRecorder(SAMPLE_CONTAINERS)
    monkeypatch.setattr(dashboard.urllib.request, "urlopen", recorder)
    app = restart_client(ALLOWED_IP)
    response = app.get("/api/containers", auth=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["error"] == "DOCKER_PROXY_URL is unset"
    assert recorder.calls == []


def test_containers_proxy_unreachable(client, tmp_path, monkeypatch):
    _app, _settings = client
    monkeypatch.setattr(dashboard, "RESTART_ALLOWED", ("telemetry",))
    monkeypatch.setenv("DOCKER_PROXY_URL", PROXY_URL)

    def explode(url, data=None, timeout=None, **kwargs):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(dashboard.urllib.request, "urlopen", explode)
    app = restart_client(ALLOWED_IP)
    response = app.get("/api/containers", auth=AUTH)
    assert response.status_code == 200
    assert response.json()["error"] == "docker proxy is unreachable"


def test_each_log_rotates_at_the_configured_size(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src" / "hooks"))
    sys.path.insert(0, str(root / "src" / "scheduler"))
    sys.path.insert(0, str(root / "tests"))
    import dream_nas
    import policies_runtime as policies
    from support import sample_settings, write_settings

    data = sample_settings()
    data["mode"] = "stock"
    data["logs"] = {"max_mb": 0.000001, "keep": 2}
    settings = write_settings(tmp_path / "settings.json", data)
    monkeypatch.setenv("CONTROL_FILE", str(settings))
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    log_path = tmp_path / "policy-used.log"
    loaded = policies.Policies(settings, log_path)
    loaded.log("mid")
    loaded.log("new")
    assert "policies reloaded" in (tmp_path / "policy-used.log.2").read_text(encoding="utf-8")
    assert "mid" in (tmp_path / "policy-used.log.1").read_text(encoding="utf-8")
    live = log_path.read_text(encoding="utf-8")
    assert "new" in live and "policies reloaded" not in live

    class Base:
        def __getattr__(self, name):
            return None

    loaded.base_dream = Base()
    decision = policies.Decision(
        policy={}, source="default", mode="stock", backend="none", backend_name=None,
        gate="ran", observer="a", observed="b",
    )
    for run_id in ("run-1", "run-2", "run-3"):
        loaded.log_run(types.SimpleNamespace(run_id=run_id), decision, "2026-01-01T00:00:00Z")
    runs = log_path.with_name("runs.log")
    assert "run-1" in runs.with_name("runs.log.2").read_text(encoding="utf-8")
    assert "run-2" in runs.with_name("runs.log.1").read_text(encoding="utf-8")
    current = runs.read_text(encoding="utf-8")
    assert "run-3" in current and "run-1" not in current

    dream_log = tmp_path / "dream-nas.log"
    monkeypatch.setenv("SCHEDULER_LOG", str(dream_log))
    monkeypatch.delenv("HONCHO_WORKSPACE", raising=False)
    handlers = {}
    monkeypatch.setattr(dream_nas.signal, "signal", lambda *_args: handlers.update(handler=_args[1]))
    monkeypatch.setattr(dream_nas, "_sleep", lambda seconds, halted: handlers["handler"]())
    dream_nas.main()
    assert "=== start" in dream_log.with_name("dream-nas.log.2").read_text(encoding="utf-8")
    assert "sleeping" in dream_log.with_name("dream-nas.log.1").read_text(encoding="utf-8")
    dream_live = dream_log.read_text(encoding="utf-8")
    assert "=== done" in dream_live and "=== start" not in dream_live

    restart_log = tmp_path / "restart.log"
    monkeypatch.setenv("RESTART_LOG", str(restart_log))
    dashboard.write_restart_log("127.0.0.1", "telemetry", "restart", "one")
    dashboard.write_restart_log("127.0.0.1", "telemetry", "stop", "two")
    dashboard.write_restart_log("127.0.0.1", "telemetry", "start", "three")
    assert "result=one" in restart_log.with_name("restart.log.2").read_text(encoding="utf-8")
    assert "result=two" in restart_log.with_name("restart.log.1").read_text(encoding="utf-8")
    restart_live = restart_log.read_text(encoding="utf-8")
    assert "result=three" in restart_live and "result=one" not in restart_live


def test_restart_log_rotates(client, tmp_path, monkeypatch):
    _app, settings = client
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["logs"] = {"max_mb": 0.000001, "keep": 1}
    settings.write_text(json.dumps(data), encoding="utf-8")
    _allow, _recorder = arm_proxy(monkeypatch, tmp_path, ["telemetry"])
    app = restart_client(ALLOWED_IP)
    assert app.post("/api/restart/telemetry", json={}, auth=AUTH).status_code == 200
    assert app.post("/api/stop/telemetry", json={}, auth=AUTH).status_code == 200
    previous = (tmp_path / "restart.log.1").read_text(encoding="utf-8")
    current = (tmp_path / "restart.log").read_text(encoding="utf-8")
    assert "action=restart" in previous
    assert "action=stop" in current
    assert "action=restart" not in current


def test_restart_log_follows_restart_log_setting(client, tmp_path, monkeypatch):
    _app, _settings = client
    _allow, _recorder = arm_proxy(monkeypatch, tmp_path, ["telemetry"])
    elsewhere = tmp_path / "logs" / "restart.log"
    monkeypatch.setenv("RESTART_LOG", str(elsewhere))
    app = restart_client(ALLOWED_IP)
    assert app.post("/api/restart/telemetry", json={}, auth=AUTH).status_code == 200
    assert "action=restart" in elsewhere.read_text(encoding="utf-8")
    assert not (tmp_path / "restart.log").exists()


def _merging_workspace(store, calls):
    def fake(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST" and path == "/workspaces":
            return {"id": "ws", "configuration": json.loads(json.dumps(store["configuration"]))}, None
        if method == "PUT" and path == "/workspaces/ws":
            # Honcho merges top-level keys and never drops one.
            store["configuration"] = {**store["configuration"], **body["configuration"]}
            return {"id": "ws", "configuration": store["configuration"]}, None
        return None, "Honcho API unavailable"

    return fake


def test_backups_move_on_start(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(valid_settings()) + "\n", encoding="utf-8")
    directory = tmp_path / "instructions"
    (directory / "backends" / "codex").mkdir(parents=True)
    (directory / "backends" / "codex" / "dream.md").write_text("live\n", encoding="utf-8")
    old_settings = tmp_path / "settings.json.bak-20260101T000000000000000"
    old_settings.write_text("{}\n", encoding="utf-8")
    old_dream = directory / "backends" / "codex" / "dream.md.bak-20260101T000000000000000"
    old_dream.write_text("old\n", encoding="utf-8")
    old_workspace = directory / "_workspace-config.bak-20260101T000000000000000.json"
    old_workspace.write_text("{}\n", encoding="utf-8")
    taken = tmp_path / "backups" / "settings"
    taken.mkdir(parents=True)
    clash = tmp_path / "settings.json.bak-20260102T000000000000000"
    clash.write_text("beside\n", encoding="utf-8")
    (taken / clash.name).write_text("already\n", encoding="utf-8")
    monkeypatch.setenv("CONTROL_FILE", str(path))
    monkeypatch.setenv("INSTRUCTIONS_DIR", str(directory))
    monkeypatch.delenv("HONCHO_WORKSPACE", raising=False)
    with TestClient(dashboard.app) as app:
        assert app.get("/api/settings").status_code == 200
    backups = tmp_path / "backups"
    # The scheduler writes its copies straight into backups/settings, so nothing beside settings.json is moved.
    assert old_settings.read_text(encoding="utf-8") == "{}\n"
    assert not (backups / "settings" / old_settings.name).exists()
    assert (backups / "instructions" / "backends" / "codex" / old_dream.name).read_text(encoding="utf-8") == "old\n"
    assert (backups / "workspace" / old_workspace.name).read_text(encoding="utf-8") == "{}\n"
    assert not old_dream.exists() and not old_workspace.exists()
    # Never overwritten, never deleted.
    assert clash.read_text(encoding="utf-8") == "beside\n"
    assert (taken / clash.name).read_text(encoding="utf-8") == "already\n"
    assert (directory / "backends" / "codex" / "dream.md").read_text(encoding="utf-8") == "live\n"


def test_history_lists_kinds(client, tmp_path, monkeypatch):
    app, path = client
    monkeypatch.setenv("HONCHO_CONFIG_FILE", str(tmp_path / "config.toml"))
    monkeypatch.setenv("HONCHO_CONFIG_BAK_DIR", str(tmp_path / "config-history"))
    (tmp_path / "config-history").mkdir()
    (tmp_path / "config-history" / "config.toml.bak-20260101T000000000000000").write_text("a = 1\n", encoding="utf-8")
    body = valid_settings()
    body["defaults"]["brief"] = 9
    assert app.post("/api/settings", json={"settings": body, "apply": True}).status_code == 200
    target = tmp_path / "instructions" / "peers" / "observer" / "dream.md"
    target.parent.mkdir(parents=True)
    target.write_text("one\n", encoding="utf-8")
    edit = {"scope": "peer", "name": "observer", "category": "dream", "text": "two\n", "apply": True}
    assert app.post("/api/instructions", json=edit).status_code == 200
    # A copy the scheduler wrote: same folder as the dashboard's, the scheduler's name form.
    scheduler_copy = path.parent / "backups" / "settings" / "settings.json.bak-20250101T000000000000000"
    scheduler_copy.write_text(json.dumps(valid_settings()) + "\n", encoding="utf-8")
    history = app.get("/api/history").json()
    kinds = history["kinds"]
    assert len(kinds["settings"]) == 2
    assert scheduler_copy.exists()
    assert history["backups"] == kinds["settings"]
    assert kinds["settings"][0]["at"].endswith("+00:00")
    [item] = kinds["instructions"]
    assert item["file"] == "peers/observer/dream.md"
    assert (item["scope"], item["owner"], item["category"]) == ("peer", "observer", "dream")
    restored = app.post(
        "/api/instructions/restore",
        json={"scope": "peer", "name": "observer", "category": "dream", "bak": item["name"], "apply": False},
    )
    assert restored.status_code == 200
    assert restored.json()["text"] == "one\n"
    assert kinds["workspace"] == []
    assert [entry["name"] for entry in kinds["honcho-config"]] == ["config.toml.bak-20260101T000000000000000"]
    newest = kinds["settings"][0]["name"]
    preview = app.post("/api/history/restore", json={"name": newest, "apply": False}).json()
    assert preview["settings"]["defaults"]["brief"] == 4


def test_control_without_workspace(client, tmp_path, monkeypatch):
    app, _path = client
    monkeypatch.setenv("HONCHO_CONFIG_FILE", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("[dream]\nENABLED = false\n", encoding="utf-8")
    body = app.get("/api/control").json()
    assert body["enabled"] is None
    assert body["workspace_error"] == "HONCHO_WORKSPACE is unset"
    assert body["dialectic"] == "cannot be paused from configuration"
    assert body["config_toml_dream"]["value"] is False
    assert body["config_toml_dream"]["in_file"] is True
    refused = app.post("/api/workspace", json={"enabled": {"dream": False}, "apply": False})
    assert refused.status_code == 400


def test_control_switches_keep_blocks(client, tmp_path, monkeypatch):
    app, _path = client
    monkeypatch.setenv("HONCHO_WORKSPACE", "ws")
    monkeypatch.setenv("HONCHO_API_TOKEN", "token-value")
    monkeypatch.setenv("HONCHO_CONFIG_FILE", str(tmp_path / "missing.toml"))
    store = {"configuration": {
        "dream": {"enabled": True, "custom_instructions": "dream text"},
        "reasoning": {"custom_instructions": "reason text"},
    }}
    calls = []
    monkeypatch.setattr(dashboard, "api_json", _merging_workspace(store, calls))
    body = app.get("/api/control").json()
    assert body["enabled"] == {"dream": True, "reasoning": None, "summary": None}
    assert body["config_toml_dream"]["value"] is True
    assert body["config_toml_dream"]["in_file"] is False
    preview = app.post("/api/workspace", json={"enabled": {"dream": False, "reasoning": False}, "apply": False})
    assert preview.status_code == 200
    assert preview.json()["changed"] is True
    paths = {row["path"] for row in preview.json()["diff"]}
    assert paths == {"/dream/enabled", "/reasoning/enabled"}
    assert not any(item[0] == "PUT" for item in calls)
    applied = app.post(
        "/api/workspace",
        json={
            "enabled": {"dream": False, "reasoning": False},
            "instructions": {"summary": {"text": "sum"}},
            "apply": True,
        },
    )
    assert applied.status_code == 200
    assert applied.json()["bak"] is True
    assert [item[0] for item in calls].count("PUT") == 1
    config = store["configuration"]
    assert config["dream"] == {"enabled": False, "custom_instructions": "dream text"}
    assert config["reasoning"] == {"custom_instructions": "reason text", "enabled": False}
    assert config["summary"] == {"custom_instructions": "sum"}
    assert len(list((tmp_path / "backups" / "workspace").glob("workspace-config.json.bak_*"))) == 1
    # null drops the key and keeps the rest of the block; a block without the key is unchanged.
    cleared = app.post("/api/workspace", json={"enabled": {"dream": None, "summary": None}, "apply": True})
    assert cleared.status_code == 200
    assert store["configuration"]["dream"] == {"custom_instructions": "dream text"}
    assert store["configuration"]["summary"] == {"custom_instructions": "sum"}
    same = app.post("/api/workspace", json={"enabled": {"dream": None}, "apply": True})
    assert same.json()["changed"] is False
    for bad in ({"enabled": {"dialectic": False}}, {"enabled": {"dream": "off"}}, {"instructions": {"dream": {"text": "x"}}}):
        assert app.post("/api/workspace", json=dict(bad, apply=False)).status_code == 400
    monkeypatch.setenv("INSTRUCTION_TOKEN_CAP", "1")
    over = app.post("/api/workspace", json={"instructions": {"reasoning": {"text": "x" * 8}}, "apply": True})
    assert over.status_code == 400
    assert store["configuration"]["reasoning"]["custom_instructions"] == "reason text"


def test_runtime_reads_only_listed_env_names(client, tmp_path, monkeypatch):
    app, _settings = client
    arm_proxy(monkeypatch, tmp_path, ["deriver"])

    def inspect(url, data=None, timeout=None, **kwargs):
        body = {"Config": {"Env": ["DERIVER_WORKERS=10", "LLM_OPENAI_API_KEY=sk-secret", "OTHER=1"]}}
        return _ProxyResponse(200, json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(dashboard.urllib.request, "urlopen", inspect)
    monkeypatch.setenv("RUNTIME_ENV_NAMES", "DERIVER_WORKERS,LLM_OPENAI_API_KEY")
    body = app.get("/api/runtime").json()
    assert body["values"] == {"DERIVER_WORKERS": {"deriver": "10"}}
    assert "sk-secret" not in json.dumps(body)


def test_models_offer_the_catalogue(client, tmp_path, monkeypatch):
    app, settings_path = client
    data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    data["providers"]["backends"] = {
        "router": {"kind": "openai-compat", "base_url": "https://router.invalid/api/v1", "key_env": "ROUTER_KEY", "model": "a/one", "models": ["a/one"], "enabled": True},
    }
    data["providers"]["active"] = "router"
    Path(settings_path).write_text(json.dumps(data), encoding="utf-8")
    catalogue = tmp_path / "models.json"
    catalogue.write_text(json.dumps({
        "dream": [
            {"vendor": "V", "label": "One", "backend": "router", "model": "a/one"},
            {"vendor": "V", "label": "Gone", "backend": "missing", "model": "x"},
        ],
        "tasks": {"anthropic": [{"label": "C", "model": "c-1"}]},
    }), encoding="utf-8")
    monkeypatch.setenv("MODELS_FILE", str(catalogue))

    def no_network(*args, **kwargs):
        raise AssertionError("no provider is asked for its model list")

    monkeypatch.setattr(dashboard.urllib.request, "urlopen", no_network)
    body = app.get("/api/models").json()
    assert body["dream"] == [{"vendor": "V", "label": "One", "backend": "router", "model": "a/one"}]
    assert body["tasks"] == {"anthropic": [{"label": "C", "model": "c-1"}]}
    assert body["backends"] == {"router": "openai-compat"}


def test_instruction_remove_keeps_a_backup_and_switch_toggles_default(client, tmp_path):
    app, _settings = client
    directory = tmp_path / "instructions"
    (directory / "peers" / "Ann").mkdir(parents=True)
    (directory / "default").mkdir(parents=True)
    (directory / "peers" / "Ann" / "dream.md").write_text("ann text\n", encoding="utf-8")
    (directory / "default" / "dream.md").write_text("all text\n", encoding="utf-8")
    body = {"scope": "peer", "name": "Ann", "category": "dream", "apply": True}
    response = app.post("/api/instructions/remove", json=body)
    assert response.status_code == 200
    assert not (directory / "peers" / "Ann" / "dream.md").exists()
    kept = list(tmp_path.rglob("dream.md.bak_*"))
    assert kept and kept[0].read_text(encoding="utf-8") == "ann text\n"
    refused = app.post("/api/instructions/remove", json={"scope": "default", "category": "dream", "apply": True})
    assert refused.status_code == 400
    off = app.post("/api/instructions/switch", json={"category": "dream", "on": False})
    assert off.json()["on"] is False
    assert (directory / "default" / "dream.md.off").is_file() and not (directory / "default" / "dream.md").exists()
    assert app.get("/api/instructions").json()["switched_off"] == ["dream"]
    on = app.post("/api/instructions/switch", json={"category": "dream", "on": True})
    assert on.json()["on"] is True and (directory / "default" / "dream.md").read_text(encoding="utf-8") == "all text\n"


def test_models_add_appends_to_the_editable_file(client, tmp_path, monkeypatch):
    app, settings_path = client
    data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    data["providers"]["backends"] = {"router": {"kind": "openai-compat", "base_url": "https://router.invalid/api/v1", "key_env": "ROUTER_KEY", "model": "a/one", "models": ["a/one"], "enabled": True}}
    data["providers"]["active"] = "router"
    Path(settings_path).write_text(json.dumps(data), encoding="utf-8")
    catalogue = tmp_path / "models-edit.json"
    catalogue.write_text(json.dumps({"dream": [], "tasks": {}}), encoding="utf-8")
    monkeypatch.setenv("MODELS_FILE", str(catalogue))
    entry = {"vendor": "V", "label": "Two", "backend": "router", "model": "b/two"}
    assert app.post("/api/models/add", json=entry).status_code == 200
    assert json.loads(catalogue.read_text(encoding="utf-8"))["dream"] == [entry]
    assert app.post("/api/models/add", json=entry).status_code == 409
    assert app.post("/api/models/add", json=dict(entry, backend="missing")).status_code == 400


def test_models_add_refuses_a_malformed_catalogue(client, tmp_path, monkeypatch):
    app, settings_path = client
    data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    data["providers"]["backends"] = {"router": {"kind": "openai-compat", "base_url": "https://router.invalid/api/v1", "key_env": "ROUTER_KEY", "model": "a/one", "enabled": True}}
    data["providers"]["active"] = "router"
    Path(settings_path).write_text(json.dumps(data), encoding="utf-8")
    catalogue = tmp_path / "models-bad.json"
    catalogue.write_text(json.dumps({"dream": {}, "tasks": []}), encoding="utf-8")
    monkeypatch.setenv("MODELS_FILE", str(catalogue))
    assert app.get("/api/models").status_code == 200
    entry = {"vendor": "V", "label": "Two", "backend": "router", "model": "b/two"}
    assert app.post("/api/models/add", json=entry).status_code == 409


def test_long_peer_names_are_refused_not_a_server_error():
    assert dashboard.safe_peer_name("p" * 200) is True
    assert dashboard.safe_peer_name("p" * 201) is False
    assert dashboard.safe_peer_name("é" * 101) is False


def test_rerank_warnings_survive_odd_shapes(client, monkeypatch):
    app, path = client
    data = valid_settings()
    data["peers"] = {"a": "not a block", "b": {"rerank": "on"}}
    monkeypatch.setattr(dashboard, "read_settings", lambda: (data, {"missing": False, "mtime_ns": None, "errors": []}))
    assert app.get("/api/rerank/warnings").status_code == 200


def test_rerank_page_and_rerank_block_save(client):
    app, path = client
    page = app.get("/", auth=AUTH).text
    assert "Rerank" in page
    assert "Per dream (settings.json)" in page
    assert "Global (api and deriver, hot)" in page
    assert "Honcho [rerank] (config.toml)" not in page and "hnsw_iterative_scan" not in page
    assert "summary.output_headroom" in page
    assert "RERANK_API_KEY" in page  # named as the place for the key, never as a field
    body = valid_settings()
    body["defaults"]["rerank"] = {"enabled": False, "candidates": 50}
    body["peers"]["observer"]["rerank"] = {"enabled": True, "provider": "tei", "base_url": "http://reranker.example:8080", "model": "bge"}
    body["pairs"]["observer/observed"]["rerank"] = {"daily_request_cap": 0, "timeout_seconds": 2.5}
    response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert response.status_code == 200, response.text
    assert json.loads(path.read_text(encoding="utf-8"))["pairs"]["observer/observed"]["rerank"] == {"daily_request_cap": 0, "timeout_seconds": 2.5}
    body["rerank"] = {"enabled": True, "provider": "voyage", "model": "rerank-3-lite", "candidates": 100, "max_documents": 500, "max_query_chars": 1500,
                      "timeout_seconds": 5.0, "callers": ["dream", "conclusions_api"], "min_score": 0.1, "daily_request_cap": 2000,
                      "failures_before_cooldown": 3, "cooldown_seconds": 60.0}
    response = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert response.status_code == 200, response.text
    assert json.loads(path.read_text(encoding="utf-8"))["rerank"]["callers"] == ["dream", "conclusions_api"]
    for bad in ({"api_key": "test-key"}, {"callers": ["dedupe"]}, {"candidates": 5000}, {"failures_before_cooldown": 0}):
        refused_body = dict(body, rerank=dict(body["rerank"], **bad))
        refused = app.post("/api/settings", json={"settings": refused_body, "apply": True}, auth=AUTH)
        assert refused.status_code == 400, bad
        assert "test-key" not in refused.text
    body["defaults"]["rerank"]["api_key"] = "test-key"
    refused = app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH)
    assert refused.status_code == 400
    assert "api_key" in refused.text and "test-key" not in refused.text
    body["defaults"]["rerank"] = {"candidates": 5000}
    assert app.post("/api/settings", json={"settings": body, "apply": True}, auth=AUTH).status_code == 400


def _rerank_line(day: str, hour: int, **data) -> str:
    base = {"caller": "dream", "provider": "voyage", "model": "rerank-3-lite", "outcome": "reranked", "candidates_sent": 100,
            "results_returned": 10, "duration_ms": 120.0, "tokens": 5000, "billed_units": None, "daily_requests": None, "daily_request_cap": None}
    base.update(data)
    event = {"specversion": "1.0", "id": f"evt_{day}_{hour}_{len(data)}", "source": "/honcho/rerank", "type": "rerank.call.completed",
             "time": f"{day}T{hour:02d}:00:00+00:00", "data": base}
    return json.dumps({"received_at": f"{day}T{hour:02d}:00:01+00:00", "event": event})


def test_rerank_stats_summarise_today_and_seven_days(client, tmp_path):
    import gzip

    app, _path = client
    now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    folder = tmp_path / "telemetry"
    folder.mkdir()
    today = (folder / "events-2026-10-31.jsonl")
    today.write_text("\n".join([
        _rerank_line("2026-10-31", 8, duration_ms=100.0, daily_requests=41, daily_request_cap=500),
        _rerank_line("2026-10-31", 9, outcome="timeout", tokens=None, duration_ms=3000.0, daily_requests=42, daily_request_cap=500),
        _rerank_line("2026-10-31", 10, caller="dialectic", outcome="skipped_cap", duration_ms=0.2, tokens=None, candidates_sent=0, daily_requests=None, daily_request_cap=500),
        _rerank_line("2026-10-31", 11, caller="dialectic", provider="cohere", tokens=None, billed_units=2.0, duration_ms=200.0, daily_requests=43, daily_request_cap=500),
        json.dumps({"received_at": "2026-10-31T11:30:00+00:00", "event": {"type": "dream.run.completed", "time": "2026-10-31T11:30:00+00:00", "data": {"tokens": 999}}}),
        "not json at all",
        json.dumps({"received_at": "2026-10-31T11:40:00+00:00", "event": {"type": "rerank.call.completed", "time": "2026-10-31T11:40:00+00:00", "data": "broken"}}),
    ]) + "\n", encoding="utf-8")
    with gzip.open(folder / "events-2026-10-26.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(_rerank_line("2026-10-26", 5, duration_ms=400.0, tokens=100) + "\n")
        handle.write(_rerank_line("2026-10-26", 6, outcome="http_error", tokens=None, duration_ms=50.0) + "\n")
    (folder / "events-2026-10-20.jsonl").write_text(_rerank_line("2026-10-20", 5) + "\n", encoding="utf-8")  # older than 7 days
    (folder / "events-2026-10-25.jsonl.gz").write_bytes(b"\x1f\x8b not really gzip")  # day 7, unreadable

    stats = dashboard.rerank_stats(folder, now)
    assert stats["days"][0] == "2026-10-31" and stats["days"][-1] == "2026-10-25"
    assert stats["files_read"] == 2
    assert stats["errors"] == ["events-2026-10-25.jsonl.gz"]
    today_view = stats["today"]
    assert today_view["total"] == 4
    assert today_view["outcomes"] == {"reranked": 2, "skipped_cap": 1, "timeout": 1}
    assert today_view["callers"] == {"dialectic": 2, "dream": 2}
    assert today_view["providers"] == {"cohere": 1, "voyage": 3}
    assert today_view["tokens"] == 5000 and today_view["billed_units"] == 2.0
    assert today_view["fallbacks"] == 2 and today_view["fallback_rate"] == 0.5
    assert today_view["sent"] == 3  # the skipped_cap call sent nothing
    assert today_view["duration_ms"] == {"p50": 200.0, "p95": 3000.0, "sampled": 3, "seen": 3}
    assert today_view["latest"] == {"time": "2026-10-31T11:00:00+00:00", "outcome": "reranked", "daily_requests": 43, "daily_request_cap": 500}
    assert today_view["last_counter"] == {"time": "2026-10-31T11:00:00+00:00", "daily_requests": 43, "daily_request_cap": 500}
    assert stats["truncated"] is False and stats["truncated_reasons"] == []
    week = stats["week"]
    assert week["total"] == 6 and week["tokens"] == 5100
    assert week["outcomes"] == {"http_error": 1, "reranked": 3, "skipped_cap": 1, "timeout": 1}
    assert week["duration_ms"]["p50"] == 200.0

    response = app.get("/api/rerank/stats", auth=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["event"] == "rerank.call.completed"
    assert body["today"]["total"] == 0 or body["today"]["total"] == 4  # the fixture day may or may not be today
    assert "sampled" in body["note"]
    page = app.get("/", auth=AUTH).text
    assert "/api/rerank/stats" in page and "Rerank calls (telemetry)" in page


def test_rerank_stats_without_a_telemetry_folder(tmp_path):
    stats = dashboard.rerank_stats(tmp_path / "missing", datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert stats["files_read"] == 0 and stats["errors"] == []
    assert stats["today"] == {"total": 0, "outcomes": {}, "callers": {}, "providers": {}, "fallbacks": 0, "fallback_rate": None, "tokens": 0,
                              "billed_units": 0.0, "sent": 0, "duration_ms": {"p50": None, "p95": None, "sampled": 0, "seen": 0},
                              "skipped_metrics": 0, "latest": None, "last_counter": None}
    assert stats["truncated"] is False
    assert dashboard._percentile([1.0], 0.95) == 1.0
    assert dashboard._percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert dashboard._percentile(list(map(float, range(1, 101))), 0.95) == 95.0


def _stats_folder(tmp_path, lines, day="2026-10-31", name=None):
    folder = tmp_path / "telemetry"
    folder.mkdir(exist_ok=True)
    (folder / (name or f"events-{day}.jsonl")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def test_rerank_stats_skip_non_finite_and_huge_numbers_and_serialise_strictly(client, tmp_path):
    app, _path = client
    lines = [
        _rerank_line("2026-10-31", 8, duration_ms=100.0, tokens=10),
        # 1e309 parses as infinity; the huge integer cannot be a JSON number on the page.
        _rerank_line("2026-10-31", 9, duration_ms=1e309, tokens=10 ** 30, billed_units=1e309, daily_requests=10 ** 30, daily_request_cap=500),
        _rerank_line("2026-10-31", 10, duration_ms=float("nan"), tokens=-5, billed_units=True, candidates_sent=10 ** 30),
        _rerank_line("2026-10-31", 11, duration_ms=50.0, tokens=20, daily_requests=7, daily_request_cap=500),
    ]
    folder = _stats_folder(tmp_path, lines)
    text = (folder / "events-2026-10-31.jsonl").read_text(encoding="utf-8")
    assert "1e+309" in text or "Infinity" in text
    stats = dashboard.rerank_stats(folder, NOW)
    today = stats["today"]
    assert today["total"] == 4 and today["outcomes"] == {"reranked": 4}
    assert today["tokens"] == 30 and today["billed_units"] == 0.0
    assert today["sent"] == 3  # the event with the huge candidates_sent is not counted as sent
    assert today["duration_ms"] == {"p50": 50.0, "p95": 100.0, "sampled": 2, "seen": 2}
    assert today["skipped_metrics"] >= 5
    assert today["last_counter"] == {"time": "2026-10-31T11:00:00+00:00", "daily_requests": 7, "daily_request_cap": 500}
    assert today["latest"]["daily_requests"] == 7
    json.dumps(stats, allow_nan=False)
    assert dashboard._finite_number(float("inf")) is None and dashboard._finite_number(10 ** 30) is None
    assert dashboard._finite_number(True) is None and dashboard._finite_number(3) == 3.0
    assert dashboard._finite_out(float("nan")) is None and dashboard._finite_out(2.5) == 2.5
    response = app.get("/api/rerank/stats", auth=AUTH)
    assert response.status_code == 200
    assert "Infinity" not in response.text and "NaN" not in response.text
    json.loads(response.text, parse_constant=lambda c: pytest.fail("non-finite constant in the response: " + c))


def test_rerank_stats_sent_follows_candidates_not_outcome(tmp_path):
    lines = [
        _rerank_line("2026-10-31", 8, outcome="cancelled", candidates_sent=100, duration_ms=900.0, tokens=None),
        _rerank_line("2026-10-31", 9, outcome="internal_error", candidates_sent=100, duration_ms=300.0, tokens=None),
        _rerank_line("2026-10-31", 10, outcome="skipped_cooldown", candidates_sent=0, duration_ms=0.1, tokens=None),
        _rerank_line("2026-10-31", 11, outcome="reranked", candidates_sent=100, duration_ms=100.0),
    ]
    stats = dashboard.rerank_stats(_stats_folder(tmp_path, lines), NOW)
    today = stats["today"]
    assert today["sent"] == 3
    assert today["duration_ms"]["seen"] == 3 and today["duration_ms"]["p50"] == 300.0 and today["duration_ms"]["p95"] == 900.0


def test_rerank_stats_keep_last_observed_counter_after_a_skip(tmp_path):
    lines = [
        _rerank_line("2026-10-31", 8, daily_requests=12, daily_request_cap=20),
        _rerank_line("2026-10-31", 9, outcome="skipped_cooldown", candidates_sent=0, tokens=None, daily_requests=None, daily_request_cap=20),
    ]
    stats = dashboard.rerank_stats(_stats_folder(tmp_path, lines), NOW)
    today = stats["today"]
    assert today["latest"]["outcome"] == "skipped_cooldown" and today["latest"]["daily_requests"] is None
    assert today["last_counter"] == {"time": "2026-10-31T08:00:00+00:00", "daily_requests": 12, "daily_request_cap": 20}


def test_rerank_stats_drain_an_oversized_line_without_holding_it(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "RERANK_LINE_LIMIT", 2000)
    monkeypatch.setattr(dashboard, "RERANK_CHUNK", 256)
    huge = json.dumps({"received_at": "x", "event": {"type": "rerank.call.completed", "time": "2026-10-31T09:00:00+00:00", "data": {"outcome": "reranked", "pad": "x" * 10_000}}})
    lines = [_rerank_line("2026-10-31", 8), huge, _rerank_line("2026-10-31", 10)]
    folder = _stats_folder(tmp_path, lines)
    seen = []
    for raw in dashboard._event_lines(folder / "events-2026-10-31.jsonl", dashboard._Budget()):
        seen.append(len(raw))
    assert len(seen) == 2 and max(seen) <= 2000
    stats = dashboard.rerank_stats(folder, NOW)
    assert stats["today"]["total"] == 2 and stats["truncated"] is False
    # an unterminated oversized tail is dropped too
    (folder / "events-2026-10-31.jsonl").write_text(_rerank_line("2026-10-31", 8) + "\n" + "y" * 5000, encoding="utf-8")
    assert dashboard.rerank_stats(folder, NOW)["today"]["total"] == 1


def test_rerank_stats_budget_hit_sets_truncated(tmp_path):
    lines = [_rerank_line("2026-10-31", h % 24, tokens=1) for h in range(12)]
    folder = _stats_folder(tmp_path, lines)
    stats = dashboard.rerank_stats(folder, NOW, budget=dashboard._Budget(max_events=5))
    assert stats["today"]["total"] == 5
    assert stats["truncated"] is True and any("event budget" in r for r in stats["truncated_reasons"])
    stats = dashboard.rerank_stats(folder, NOW, budget=dashboard._Budget(max_bytes=700))
    assert stats["truncated"] is True and any("byte budget" in r for r in stats["truncated_reasons"])
    assert 0 < stats["today"]["total"] < 12
    (folder / "events-2026-10-30.jsonl").write_text(_rerank_line("2026-10-30", 1) + "\n", encoding="utf-8")
    stats = dashboard.rerank_stats(folder, NOW, budget=dashboard._Budget(max_events=12))
    assert stats["today"]["total"] == 12 and stats["week"]["total"] == 12
    assert any("not read" in r for r in stats["truncated_reasons"])
    json.dumps(stats, allow_nan=False)


def test_rerank_stats_cache_past_days_by_size_and_mtime(tmp_path, monkeypatch):
    dashboard._day_cache.clear()
    folder = _stats_folder(tmp_path, [_rerank_line("2026-10-31", 8)])
    past = folder / "events-2026-10-30.jsonl"
    past.write_text(_rerank_line("2026-10-30", 8, tokens=1) + "\n", encoding="utf-8")
    calls = []
    real_scan = dashboard._scan_file

    def counting(path, days, budget, period, today=None):
        calls.append(path.name)
        return real_scan(path, days, budget, period, today)

    monkeypatch.setattr(dashboard, "_scan_file", counting)
    first = dashboard.rerank_stats(folder, NOW)
    assert first["week"]["total"] == 2 and sorted(calls) == ["events-2026-10-30.jsonl", "events-2026-10-31.jsonl"]
    calls.clear()
    second = dashboard.rerank_stats(folder, NOW)
    assert calls == ["events-2026-10-31.jsonl"]  # only today's file is re-read
    assert second["week"]["total"] == 2
    past.write_text(past.read_text(encoding="utf-8") + _rerank_line("2026-10-30", 9, tokens=1) + "\n", encoding="utf-8")
    os.utime(past, ns=(past.stat().st_mtime_ns + 5_000_000_000,) * 2)
    calls.clear()
    third = dashboard.rerank_stats(folder, NOW)
    assert "events-2026-10-30.jsonl" in calls and third["week"]["total"] == 3 and third["week"]["tokens"] == 5002
    dashboard._day_cache.clear()


def test_rerank_stats_corrupt_gzip_body_is_listed_not_fatal(tmp_path):
    import gzip

    folder = _stats_folder(tmp_path, [_rerank_line("2026-10-31", 8)])
    good = gzip.compress(b"x" * 4000)
    (folder / "events-2026-10-29.jsonl.gz").write_bytes(good[:12] + b"\x00\x01garbage-not-deflate\xff\xfe" + good[-8:])
    with pytest.raises((zlib.error, EOFError)):
        with gzip.open(folder / "events-2026-10-29.jsonl.gz", "rb") as handle:
            handle.read()
    stats = dashboard.rerank_stats(folder, NOW)
    assert stats["errors"] == ["events-2026-10-29.jsonl.gz"]
    assert stats["today"]["total"] == 1


def test_rerank_stats_duration_sample_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "RERANK_DURATION_SAMPLE", 10)
    lines = [_rerank_line("2026-10-31", h % 24, duration_ms=float(h)) for h in range(50)]
    stats = dashboard.rerank_stats(_stats_folder(tmp_path, lines), NOW)
    duration = stats["today"]["duration_ms"]
    assert duration["sampled"] == 10 and duration["seen"] == 50
    assert 0 <= duration["p50"] <= 49 and 0 <= duration["p95"] <= 49


@pytest.mark.parametrize("section", ["defaults", "peers", "pairs"])
@pytest.mark.parametrize("base_cap,override,allowed", [(1000, 5000, False), (None, 5000, True), (1000, 500, True), (0, 5000, False)])
def test_dashboard_rerank_cap_clamp(client, section, base_cap, override, allowed):
    data = valid_settings()
    if base_cap is not None:
        data["rerank"] = {"daily_request_cap": base_cap}
    block = data["defaults"] if section == "defaults" else next(iter(data[section].values()))
    block["rerank"] = {"daily_request_cap": override}
    errors = dashboard.check_settings(data)
    assert (not errors) == allowed
    if not allowed:
        assert any("daily_request_cap" in line and str(base_cap) in line for line in errors)


@pytest.mark.parametrize("name", ["failures_before_cooldown", "cooldown_seconds"])
def test_dashboard_rejects_per_dream_cooldown(client, name):
    data = valid_settings()
    data["defaults"]["rerank"] = {name: 3}
    assert any(name in line for line in dashboard.check_settings(data))


@pytest.mark.parametrize("state,active", [("missing", False), ("active", True), ("failed", False), ("stale", True), ("pending", True), ("unchecked", False)])
def test_dashboard_hook_status(client, tmp_path, monkeypatch, state, active):
    app, settings = client
    status = tmp_path / "hook-status.json"
    monkeypatch.setenv("POLICY_STATUS", str(status))
    current = settings.stat().st_mtime_ns
    old = time.time_ns() - 120_000_000_000
    if state in ("stale", "pending"):
        current = old if state == "stale" else time.time_ns()
        os.utime(settings, ns=(current, current))
    if state != "missing":
        status.write_text(json.dumps({"installed_at": "2026-11-02T00:00:00Z", "checks_passed": state != "unchecked",
                                      "settings_mtime_applied": current - 1 if state in ("stale", "pending") else current,
                                      "last_reload_error": "ValueError" if state == "failed" else None}), encoding="utf-8")
    response = app.get("/api/status").json()["hook"]
    assert response["active"] is active
    if state == "active":
        assert "hook active, settings applied" in response["message"]
        # A readable time, not the raw nanosecond mtime.
        assert str(current) not in response["message"]
        assert re.search(r"applied \d{4}-\d\d-\d\d \d\d:\d\d:\d\d$", response["message"])
    if state in ("stale", "pending"):
        # The hook reloads when a dream starts: a newer file waits for the next dream, however old it is.
        assert response.get("pending") is True
        assert "apply at the next dream" in response["message"]


@pytest.mark.parametrize(
    "dreams_blocked,message",
    [
        (True, "hook failed — dreams blocked"),
        (False, "hook failed — running stock Honcho"),
    ],
)
def test_dashboard_hook_failed_message(client, tmp_path, monkeypatch, dreams_blocked, message):
    app, _settings = client
    status = tmp_path / "hook-status.json"
    monkeypatch.setenv("POLICY_STATUS", str(status))
    status.write_text(json.dumps({
        "checks_passed": False,
        "hook_failed": "RuntimeError",
        "dreams_blocked": dreams_blocked,
    }), encoding="utf-8")
    response = app.get("/api/status").json()["hook"]
    assert response["active"] is False
    assert response["message"] == message


def test_status_survives_an_unreadable_settings_file(client, tmp_path, monkeypatch):
    # hook_status read settings.get on None and answered 500.
    app, settings = client
    status = tmp_path / "hook-status.json"
    monkeypatch.setenv("POLICY_STATUS", str(status))
    status.write_text(json.dumps({"installed_at": "2026-11-02T00:00:00Z", "checks_passed": True,
                                  "settings_mtime_applied": 1, "last_reload_error": None}), encoding="utf-8")
    settings.write_text("{", encoding="utf-8")
    response = app.get("/api/status")
    assert response.status_code == 200
    assert "hook" in response.json()


def test_rerank_stats_skip_a_deeply_nested_line(client, tmp_path, monkeypatch):
    app, _settings = client
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir(exist_ok=True)
    monkeypatch.setenv("TELEMETRY_DIR", str(telemetry))
    (telemetry / f"events-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl").write_text("[" * 200000 + "\n", encoding="utf-8")
    assert app.get("/api/rerank/stats").status_code == 200


def test_config_provenance_and_running_version(client, tmp_path, monkeypatch):
    app, _settings = client
    schema = json.loads((ROOT / "src/config/honcho-config.schema.json").read_text())
    monkeypatch.setattr(dashboard.honcho_config, "load_schema", lambda: {**schema, "fork_sha": "a" * 40, "fork_version_heading": "Honcho test fork"})
    monkeypatch.setenv("HONCHO_CONFIG_FILE", str(tmp_path / "config.toml"))
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    (telemetry / f"events-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl").write_text(json.dumps({"event": {"data": {"honcho_version": "different-version"}}}) + "\n", encoding="utf-8")
    result = app.get("/api/honcho-config").json()
    assert result["fork_sha"] == "a" * 40
    assert result["fork_version_heading"] == "Honcho test fork"
    assert result["schema_honcho_version"] == schema["honcho_version"]
    assert result["schema_upstream_sha"] == schema["upstream_sha"]
    assert result["running_honcho_version"] == "different-version"
    assert "differs" in result["version_warning"]



def test_rerank_warnings_from_saved_settings(client, monkeypatch):
    app, settings = client
    monkeypatch.setattr(dashboard, "proxy_base", lambda: "")
    monkeypatch.setenv("PGOPTIONS", "-c hnsw.iterative_scan=strict_order -c hnsw.ef_search=100")
    data = valid_settings()
    data["rerank"] = {"enabled": True, "callers": ["dialectic"], "candidates": 150}
    data["defaults"]["rerank"] = {"enabled": True, "candidates": 200}
    settings.write_text(json.dumps(data), encoding="utf-8")
    body = app.get("/api/rerank/warnings").json()
    assert body["ef_search"] == 100 and body["iterative_scan"] == "strict_order"
    assert any("defaults: this block has no effect" in warning for warning in body["warnings"])
    assert any(warning.startswith("global: candidates (150) exceeds ef_search (100)") for warning in body["warnings"])
    assert any(warning.startswith("defaults: candidates (200) exceeds ef_search (100)") for warning in body["warnings"])
    data["rerank"] = {"enabled": True, "callers": ["dream"], "candidates": 100}
    data["defaults"]["rerank"] = {"enabled": True, "candidates": 100}
    settings.write_text(json.dumps(data), encoding="utf-8")
    assert app.get("/api/rerank/warnings").json()["warnings"] == []


def test_rerank_warnings_show_unknown_ef_search_when_pgoptions_unreadable(client, monkeypatch):
    app, settings = client
    monkeypatch.setattr(dashboard, "proxy_base", lambda: "")
    monkeypatch.delenv("PGOPTIONS", raising=False)
    data = valid_settings()
    data["rerank"] = {"enabled": True, "candidates": 900}
    settings.write_text(json.dumps(data), encoding="utf-8")
    body = app.get("/api/rerank/warnings").json()
    assert body["ef_search"] == "unknown" and body["pgoptions_source"] is None
    assert body["warnings"] == []


def test_deployed_pgoptions_read_from_container_environment(client, tmp_path, monkeypatch):
    app, settings = client
    monkeypatch.setattr(dashboard, "RESTART_ALLOWED", ("api", "deriver"))
    monkeypatch.setattr(dashboard, "proxy_base", lambda: "http://proxy")
    options = {"api": "-c hnsw.ef_search=100", "deriver": "-c hnsw.iterative_scan=strict_order -c hnsw.ef_search=64"}

    def fake_open(method, path, timeout):
        name = path.split("/")[2]
        return 200, json.dumps({"Config": {"Env": ["OTHER=1", "PGOPTIONS=" + options[name]]}}).encode()

    monkeypatch.setattr(dashboard, "proxy_open", fake_open)
    result = dashboard.deployed_pgoptions()
    assert result["ef_search"] == 64 and result["source"] == "containers" and result["differs"] is True


def test_runs_join_telemetry_by_run_id(client, tmp_path):
    app, _settings = client
    day = "2026-11-02"
    records = [{"run_id": run, "observer": "a", "observed": "b", "started": day + "T01:00:00Z", "finished": day + "T01:01:00Z", "rerank": {"candidates": 100}} for run in ("one", "two")]
    (tmp_path / "runs.log").write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    events = [
        {"type": "rerank.call.completed", "data": {"run_id": "one", "outcome": "noop_single", "tokens": 0, "candidates_sent": 0}},
        {"type": "rerank.call.completed", "data": {"run_id": "one", "outcome": "future_outcome", "tokens": 20, "candidates_sent": 5, "duration_ms": 42}},
        {"type": "rerank.call.completed", "data": {"run_id": "other", "outcome": "reranked", "tokens": 999}},
        {"type": "dream.run", "data": {"run_id": "one", "total_duration_ms": 100, "conclusions_created": 3}},
        {"id": "actual", "type": "llm.call.completed", "data": {"run_id": "one", "call_purpose": "dream_deduction", "model": "deepseek-chat", "backend": "deepseek", "billing_route": "api", "routing_evidence": "bridge_response"}},
        {"id": "actual", "type": "llm.call.completed", "data": {"run_id": "one", "model": "duplicate"}},
        {"type": "llm.call.completed", "data": {"run_id": "two", "model": "stage-route"}},
    ]
    (telemetry / f"events-{day}.jsonl").write_text("\n".join(json.dumps({"event": {**event, "time": day + "T01:00:00Z"}}) for event in events), encoding="utf-8")
    result = app.get("/api/runs", params={"date": day, "pair": "a/b"}).json()
    rows = {row["run_id"]: row for row in result["runs"]}
    assert rows["one"]["rerank_stats"]["outcomes"] == {"future_outcome": 1, "noop_single": 1}
    assert rows["one"]["rerank_stats"]["tokens"] == 20
    assert rows["one"]["rerank_stats"]["duration_ms"]["p50"] == 42
    assert rows["one"]["dream"]["duration_ms"] == 100
    assert rows["one"]["dream"]["conclusions_created"] == 3
    assert rows["two"]["dream"]["duration_ms"] is None
    assert rows["one"]["observed_models"] == [{"stage": "dream_deduction", "model": "deepseek-chat", "backend": "deepseek", "billing_route": "api", "evidence": "bridge_response", "calls": 1}]
    assert rows["two"]["observed_models"][0]["backend"] == "unknown"
    assert rows["two"]["observed_models"][0]["model"] == "stage-route"
    assert app.get("/api/runs", params={"date": day, "pair": "other/pair"}).json()["runs"] == []
    assert app.get("/api/runs", params={"date": "bad"}).status_code == 400



def test_cost_estimates_keep_unknown_and_subscription_separate(client, tmp_path, monkeypatch):
    app, _settings = client
    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps({"llm": {"m": {"input_per_million": 1, "output_per_million": 2, "cache_read_per_million": 0.1, "cache_creation_per_million": 1.25}}, "rerank": {"voyage/r": {"per_million_tokens": 0.02}}, "cli": {"c": "subscription"}}), encoding="utf-8")
    monkeypatch.setenv("RATES_FILE", str(rates))
    llm = {"model": "m", "call_purpose": "dream", "transport": "openai", "provider_input_tokens": 100, "provider_output_tokens": 50, "cache_read_tokens": 20, "cache_creation_tokens": 10, "run_id": "one"}
    events = [
        ("llm.call.completed", {**llm, "billing_route": "api"}),
        ("llm.call.completed", llm),
        ("llm.call.completed", {**llm, "model": "c"}),
        ("llm.call.completed", {**llm, "model": "missing", "billing_route": "api"}),
        ("rerank.call.completed", {"provider": "voyage", "model": "r", "tokens": 1000000, "caller": "dream", "run_id": "one"}),
    ]
    folder = tmp_path / "telemetry"
    folder.mkdir()
    day = "2026-11-02"
    (folder / f"events-{day}.jsonl").write_text("\n".join(json.dumps({"event": {"id": str(index), "time": day + "T01:00:00Z", "type": kind, "data": data}}) for index, (kind, data) in enumerate(events)), encoding="utf-8")
    result = app.get("/api/cost", params={"date": day}).json()
    assert result["estimated_usd"] == pytest.approx(0.0201845)
    assert result["potential_api_usd"] == pytest.approx(0.0001845)
    assert result["no_rate_calls"] == 1 and result["subscription_calls"] == 1
    assert result["unknown_route_calls"] == 1 and result["priced_calls"] == 2
    assert app.get("/api/cost", params={"date": day, "run_id": "absent"}).json()["estimated_usd"] is None


@pytest.mark.parametrize("transport,pin,read,expected", [
    ("anthropic", 90000, 10000, 0.0096),
    ("anthropic", 90001, 10000, 0.0480005),
    ("openai", 100000, 10000, 0.0096),
    ("openai", 100001, 10000, 0.0480005),
])
def test_public_haiku_context_tier_counts_cached_prompt_tokens(transport, pin, read, expected):
    rates = json.loads((ROOT / "src/config/rates.example.json").read_text(encoding="utf-8"))
    data = {"model": "claude-haiku-5-5", "billing_route": "api", "transport": transport,
            "provider_input_tokens": pin, "provider_output_tokens": 1000, "cache_read_tokens": read}
    assert dashboard.estimate_call("llm.call.completed", data, rates)["estimated_usd"] == pytest.approx(expected)


def test_deepseek_off_peak_is_half_outside_the_peak_windows():
    rates = json.loads((ROOT / "src/config/rates.example.json").read_text(encoding="utf-8"))
    spec = rates["llm"]["deepseek-flash"]
    assert spec["peak_utc"] == ["01:00-04:00", "06:00-10:00"]
    assert spec["peak_days"] == "Mon-Fri"
    assert spec["off_peak_factor"] == 0.5
    assert "holiday" in spec["estimate_note"].lower()
    data = {"model": "deepseek-flash", "billing_route": "api", "transport": "openai",
            "provider_input_tokens": 1_000_000, "provider_output_tokens": 0}
    peak = dashboard.estimate_call("llm.call.completed", data, rates, "2026-11-03T02:00:00Z")
    midday = dashboard.estimate_call("llm.call.completed", data, rates, "2026-11-03T12:00:00Z")
    saturday = dashboard.estimate_call("llm.call.completed", data, rates, "2026-11-07T02:00:00Z")
    assert peak["estimated_usd"] == pytest.approx(0.3)
    assert midday["estimated_usd"] == pytest.approx(0.15)
    assert saturday["estimated_usd"] == pytest.approx(0.15)
    plain = {"llm": {"m": {"input_per_million": 0.3, "output_per_million": 1.2}}}
    same = {**data, "model": "m"}
    assert dashboard.estimate_call("llm.call.completed", same, plain, "2026-11-03T12:00:00Z")["estimated_usd"] == pytest.approx(0.3)


def test_cost_missing_cache_write_price_is_unknown():
    rates = {"llm": {"m": {"input_per_million": 1, "output_per_million": 2, "cache_read_per_million": 0.1}}, "cli": {"m": "subscription"}}
    data = {"model": "m", "billing_route": "api", "transport": "openai", "provider_input_tokens": 100, "provider_output_tokens": 10, "cache_read_tokens": 20, "cache_creation_tokens": 10}
    estimate = dashboard.estimate_call("llm.call.completed", data, rates)
    assert estimate["estimated_usd"] is None
    assert estimate["billing_route"] == "api" and estimate["route_conflict"]
    assert "cache_creation" in estimate["note"]


def test_cost_direct_transports_and_incomplete_usage():
    for transport in ("anthropic", "gemini"):
        assert dashboard.estimate_call("llm.call.completed", {"model": "m", "transport": transport}, {})["billing_route"] == "api"
    assert dashboard.estimate_call("llm.call.completed", {"model": "stage-route", "transport": "openai"}, {})["billing_route"] == "unknown"
    result = dashboard.estimate_call("llm.call.completed", {"usage_complete": False, "billing_route": "api"}, {})
    assert result["estimated_usd"] is None and result["note"] == "token usage incomplete"


def test_cost_price_alias_requires_observed_bridge_resolution():
    data = {"model": "deepseek-flash-v1", "resolved_model": "deepseek-flash", "billing_route": "api",
            "routing_evidence": "bridge_response", "transport": "openai", "provider_input_tokens": 1000000, "provider_output_tokens": 0}
    rates = {"llm": {"deepseek-flash": {"input_per_million": 2}}}
    result = dashboard.estimate_call("llm.call.completed", data, rates)
    assert result["estimated_usd"] == 2
    assert "observed resolved model" in result["note"]
    rates["llm"]["deepseek-flash-v1"] = {"input_per_million": 3}
    assert dashboard.estimate_call("llm.call.completed", data, rates)["estimated_usd"] == 3
    rates["llm"].pop("deepseek-flash-v1")
    assert dashboard.estimate_call("llm.call.completed", {**data, "routing_evidence": "direct_transport"}, rates)["estimated_usd"] is None
    assert dashboard.estimate_call("llm.call.completed", {**data, "resolved_model": "missing"}, rates)["estimated_usd"] is None


def test_bridge_status_reads_configured_bridge_without_credentials(client, monkeypatch):
    app, _settings = client
    import tomlkit
    config = tomlkit.parse('''
[dream.deduction_model_config]
model = "stage-route"
transport = "openai"
[dream.deduction_model_config.overrides]
base_url = "http://bridge.test/v1"
api_key_env = "BRIDGE_API_KEY"
[dream.deduction_model_config.overrides.provider_params.extra_body]
backend = "@dream_deduction"
[dream.induction_model_config]
model = "stage-route"
transport = "openai"
[dream.induction_model_config.overrides]
base_url = "http://bridge.test/v1"
api_key_env = "BRIDGE_API_KEY"
[dream.induction_model_config.overrides.provider_params.extra_body]
backend = "@dream_induction"
''')
    monkeypatch.setattr(dashboard.honcho_config, "read_document", lambda: (config, False, None))
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            assert limit == 65537
            return json.dumps({"settings_ok": True, "settings_error": "private-key", "calls": 4, "api_key": "private-key", "routing": {"deepseek": {"calls": 4, "errors": 0, "last_call": {"resolved_model": "deepseek-chat", "upstream_model": "deepseek-v4", "billing_route": "api", "ok": True, "api_key": "private-key"}}}}).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://bridge.test/health"
            assert not request.headers and timeout == 2
            return Reply()
    monkeypatch.setattr(dashboard.urllib.request, "build_opener", lambda *args: Opener())
    result = app.get("/api/bridge-status").json()
    assert result["configured"] and len(result["bridges"]) == 1
    health = result["bridges"][0]["health"]
    assert health["routing"]["deepseek"]["last_call"]["upstream_model"] == "deepseek-v4"
    assert "private-key" not in json.dumps(result) and "bridge.test" not in json.dumps(result)
    config["dream"]["deduction_model_config"]["overrides"]["base_url"] = "http://user:secret@bridge.test/v1"
    config["dream"].pop("induction_model_config")
    assert app.get("/api/bridge-status").json()["bridges"][0]["status"] == "invalid_config"


@pytest.mark.parametrize("dream", ["broken", {"deduction_model_config": 1}, {"deduction_model_config": {"overrides": "broken"}}, {"deduction_model_config": {"overrides": {"provider_params": []}}}, {"deduction_model_config": {"overrides": {"provider_params": {"extra_body": 3}}}}])
def test_bridge_status_malformed_config_is_safe(client, monkeypatch, dream):
    app, _settings = client
    monkeypatch.setattr(dashboard.honcho_config, "read_document", lambda: ({"dream": dream}, False, None))
    result = app.get("/api/bridge-status")
    assert result.status_code == 200
    assert not result.json()["configured"] and result.json()["bridges"] == []



def test_rate_loader_accepts_legacy_and_keeps_missing_unknown(client, tmp_path, monkeypatch):
    path = tmp_path / "rates.json"
    monkeypatch.setenv("RATES_FILE", str(path))
    assert dashboard.load_rates()[1]
    path.write_text(json.dumps({"voyage": {"r": 0.02}}), encoding="utf-8")
    rates, error = dashboard.load_rates()
    assert error is None
    result = dashboard.estimate_call("rerank.call.completed", {"provider": "voyage", "model": "r", "tokens": 1000000}, rates)
    assert result["estimated_usd"] == 0.02
    unknown = dashboard.estimate_call("rerank.call.completed", {"provider": "voyage", "model": "missing", "tokens": 1000000}, rates)
    assert unknown["estimated_usd"] is None and unknown["note"] == "no rate"



def test_stage_references_and_cli_model_override(client):
    app, _path = client
    data = valid_settings()
    data["providers"]["stages"] = {"deriver": "codex:new-model", "dream.induction": "codex:other-model"}
    data["defaults"]["backend"] = "codex:new-model"
    assert dashboard.check_settings(data) == []
    data["providers"]["stages"]["summary"] = "absent:model"
    errors = dashboard.check_settings(data)
    assert any("providers.stages.summary" in error for error in errors)
    data["scheduler"]["dream_type"] = "deduction"
    assert dashboard.check_settings(data)



@pytest.mark.parametrize("outcome", ["error", "cancelled"])
def test_zero_usage_failed_llm_call_is_not_priced_as_free(outcome):
    data = {"model": "m", "billing_route": "api", "provider_input_tokens": 0, "provider_output_tokens": 0, "outcome": outcome}
    rates = {"llm": {"m": {"input_per_million": 1, "output_per_million": 2}}}
    estimate = dashboard.estimate_call("llm.call.completed", data, rates)
    assert estimate["estimated_usd"] is None and estimate["tokens"] is None
    assert "usage unavailable" in estimate["note"]
    data["outcome"] = "success"
    assert dashboard.estimate_call("llm.call.completed", data, rates)["estimated_usd"] == 0


def test_rerank_today_counts_only_events_dated_today(tmp_path):
    # Today's file also holds late events dated earlier; they belong to the week, not to Today.
    folder = tmp_path / "telemetry"
    folder.mkdir()
    now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    (folder / "events-2026-10-31.jsonl").write_text(
        _rerank_line("2026-10-31", 9) + "\n" + _rerank_line("2026-10-30", 23, run_id="late") + "\n", encoding="utf-8")
    stats = dashboard.rerank_stats(folder, now)
    assert stats["today"]["total"] == 1
    assert stats["week"]["total"] == 2
