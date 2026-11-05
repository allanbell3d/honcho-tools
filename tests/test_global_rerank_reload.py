"""Global rerank reloads are atomic and use the wrapper or the schema offline."""
import copy
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "hooks"))
import policies_runtime as policies


def test_bad_global_rerank_keeps_last_good_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    # Force the documented offline schema path, regardless of installed Honcho modules.
    monkeypatch.setitem(sys.modules, "src.honcho_rerank.settings", None)
    path = tmp_path / "settings.json"
    log = tmp_path / "policies.log"
    good = {"mode": "stock", "rerank": {"enabled": True, "candidates": 40}}
    path.write_text(json.dumps(good), encoding="utf-8")
    store = policies.Policies(path, log)
    applied_mtime = store.mtime
    bad = copy.deepcopy(good)
    bad["mode"] = "scheduler"
    bad["rerank"]["candidates"] = 5000
    path.write_text(json.dumps(bad), encoding="utf-8")
    os.utime(path, ns=(applied_mtime + 1_000_000_000,) * 2)
    store.reload()
    assert store.data["mode"] == "scheduler"
    assert store.data["rerank"]["candidates"] == 40
    assert store.data["rerank"]["enabled"] is True
    assert store.mtime != applied_mtime
    assert store.last_reload_error is None
    text = log.read_text(encoding="utf-8")
    assert "rerank settings not applied:" in text
    assert "5000" not in text
    assert "policies reloaded not applied:" not in text
    good["rerank"]["candidates"] = 60
    path.write_text(json.dumps(good), encoding="utf-8")
    os.utime(path, ns=(applied_mtime + 2_000_000_000,) * 2)
    store.reload()
    assert store.data == good
    assert store.last_reload_error is None


@pytest.mark.parametrize("bad", [
    {"callers": ["unknown"]}, {"callers": "dream"},
    {"max_query_chars": 0}, {"failures_before_cooldown": 0},
    {"cooldown_seconds": -1}, {"cooldown_seconds": float("nan")},
    {"api_key": "do-not-log-this"}, {"unknown": None}, [],
])
def test_bad_global_fields_are_rejected_by_offline_schema(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    monkeypatch.setitem(sys.modules, "src.honcho_rerank.settings", None)
    path = tmp_path / "settings.json"
    good = {"rerank": {"enabled": True, "callers": ["dream"],
                       "failures_before_cooldown": 2, "cooldown_seconds": 0,
                       "max_query_chars": None}}
    path.write_text(json.dumps(good), encoding="utf-8")
    store = policies.Policies(path, tmp_path / "policies.log")
    assert store.data == good
    stamp = store.mtime
    path.write_text(json.dumps({"rerank": bad}), encoding="utf-8")
    os.utime(path, ns=(stamp + 1_000_000_000,) * 2)
    store.reload()
    assert store.data == good
    assert store.last_reload_error is None
    assert "do-not-log-this" not in store.log_path.read_text(encoding="utf-8")
    assert "rerank settings not applied:" in store.log_path.read_text(encoding="utf-8")


def test_wrapper_class_rejection_keeps_last_good(tmp_path, monkeypatch):
    calls = []

    class WrapperSettings:
        @classmethod
        def model_validate(cls, values):
            calls.append(values)
            if values.get("MODEL") == "refused-model":
                raise ValueError("wrapper-only constraint")

    monkeypatch.setitem(sys.modules, "src.honcho_rerank.settings",
                        types.SimpleNamespace(RerankSettings=WrapperSettings))
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    path = tmp_path / "settings.json"
    good = {"rerank": {"candidates": 40, "cooldown_seconds": 0}}
    path.write_text(json.dumps(good), encoding="utf-8")
    store = policies.Policies(path, tmp_path / "policies.log")
    assert calls == [{"CANDIDATES": 40, "COOLDOWN_SECONDS": 0}]
    stamp = store.mtime
    path.write_text(json.dumps({"rerank": {"model": "refused-model"}}), encoding="utf-8")
    os.utime(path, ns=(stamp + 1_000_000_000,) * 2)
    store.reload()
    assert calls[-1] == {"MODEL": "refused-model"}
    assert store.data == good
    assert store.last_reload_error is None
    assert "refused-model" not in store.log_path.read_text(encoding="utf-8")
    assert "rerank settings not applied:" in store.log_path.read_text(encoding="utf-8")
