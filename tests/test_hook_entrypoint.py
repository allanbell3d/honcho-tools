"""Deriver entrypoint when install() fails. A fake src.deriver stands in for Honcho.

No Honcho import. The fake module records DREAM_ENABLED at the moment it runs.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "src" / "hooks"

sys.path.insert(0, str(HOOKS))
import policies_runtime  # noqa: E402

_ENTRY = None


def _entry():
    global _ENTRY
    if _ENTRY is None:
        spec = importlib.util.spec_from_file_location("hook_deriver_main", HOOKS / "deriver_main.py")
        module = importlib.util.module_from_spec(spec)
        saved = sys.path.copy()
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = saved
        _ENTRY = module
    return _ENTRY


def _settings(**changes) -> dict:
    data = {
        "mode": "stock",
        "peers": {"observer": {"enabled": True}, "observed": {}},
        "pairs": {"observer/observed": {"brief": 4}},
    }
    data.update(changes)
    return data


@pytest.fixture
def deriver(tmp_path, monkeypatch):
    saved_path = sys.path.copy()
    saved_modules = {name: sys.modules[name] for name in list(sys.modules) if name == "src" or name.startswith("src.")}
    root = tmp_path / "honcho"
    package = root / "src"
    package.mkdir(parents=True)
    script = package / "deriver.py"
    marker = tmp_path / "deriver-ran.txt"
    script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "seen = os.environ['DREAM_ENABLED'] if 'DREAM_ENABLED' in os.environ else 'UNSET'\n"
        "Path(os.environ['FAKE_DERIVER_MARKER']).write_text(seen, encoding='utf-8')\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(root))
    monkeypatch.setenv("FAKE_DERIVER_MARKER", str(marker))
    monkeypatch.setenv("CONTROL_FILE", str(tmp_path / "settings.json"))
    monkeypatch.delenv("DREAM_ENABLED", raising=False)
    module = _entry()
    if sys.path[0] != str(root):
        sys.path.insert(0, str(root))
    try:
        yield module, marker, script
    finally:
        for name in list(sys.modules):
            if name == "src" or name.startswith("src."):
                del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


def _install_raises(monkeypatch) -> None:
    def boom():
        raise RuntimeError("sk-do-not-store")

    monkeypatch.setattr(policies_runtime, "install", boom)


def _run_failed(module, monkeypatch, caplog):
    _install_raises(monkeypatch)
    caplog.set_level(logging.ERROR)
    module.main()
    assert [record.message for record in caplog.records] == ["RuntimeError"]
    status_path = Path(os.environ["POLICY_STATUS"])
    text = status_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "sk-do-not-store" not in text
    assert list(status_path.parent.glob(status_path.name + ".tmp-*")) == []
    status = json.loads(text)
    assert status["checks_passed"] is False
    assert status["hook_failed"] == "RuntimeError"
    return status


def test_install_failure_stock_mode_runs_honcho_dreams(deriver, tmp_path, monkeypatch, caplog):
    module, marker, _script = deriver
    (tmp_path / "settings.json").write_text(json.dumps(_settings()) + "\n", encoding="utf-8")
    status = _run_failed(module, monkeypatch, caplog)
    assert marker.read_text(encoding="utf-8") == "UNSET"
    assert "DREAM_ENABLED" not in os.environ
    assert status["dreams_blocked"] is False


def test_install_failure_absent_mode_runs_honcho_dreams(deriver, tmp_path, monkeypatch, caplog):
    module, marker, _script = deriver
    data = _settings()
    del data["mode"]
    (tmp_path / "settings.json").write_text(json.dumps(data) + "\n", encoding="utf-8")
    status = _run_failed(module, monkeypatch, caplog)
    assert marker.read_text(encoding="utf-8") == "UNSET"
    assert "DREAM_ENABLED" not in os.environ
    assert status["dreams_blocked"] is False


@pytest.mark.parametrize("section,key", [("peers", "observer"), ("pairs", "observer/observed")])
def test_install_failure_disabled_entry_blocks_dreams(deriver, tmp_path, monkeypatch, caplog, section, key):
    module, marker, _script = deriver
    data = _settings()
    data[section][key]["enabled"] = False
    (tmp_path / "settings.json").write_text(json.dumps(data) + "\n", encoding="utf-8")
    status = _run_failed(module, monkeypatch, caplog)
    assert marker.read_text(encoding="utf-8") == "false"
    assert os.environ["DREAM_ENABLED"] == "false"
    assert status["dreams_blocked"] is True


def test_install_failure_scheduler_mode_blocks_dreams(deriver, tmp_path, monkeypatch, caplog):
    module, marker, _script = deriver
    (tmp_path / "settings.json").write_text(json.dumps(_settings(mode="scheduler")) + "\n", encoding="utf-8")
    status = _run_failed(module, monkeypatch, caplog)
    assert marker.read_text(encoding="utf-8") == "false"
    assert os.environ["DREAM_ENABLED"] == "false"
    assert status["dreams_blocked"] is True


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid", "array"])
def test_install_failure_unreadable_settings_blocks_dreams(deriver, tmp_path, monkeypatch, caplog, kind):
    module, marker, _script = deriver
    path = tmp_path / "settings.json"
    if kind == "missing":
        pass
    elif kind == "directory":
        path.mkdir()
    elif kind == "invalid":
        path.write_text("{", encoding="utf-8")
    else:
        path.write_text("[]\n", encoding="utf-8")
    status = _run_failed(module, monkeypatch, caplog)
    assert marker.read_text(encoding="utf-8") == "false"
    assert os.environ["DREAM_ENABLED"] == "false"
    assert status["dreams_blocked"] is True


def test_install_success_runs_stock_deriver_unchanged(deriver, monkeypatch):
    module, marker, _script = deriver
    called = []

    def ok():
        called.append(True)

    monkeypatch.setattr(policies_runtime, "install", ok)
    module.main()
    assert called == [True]
    assert marker.read_text(encoding="utf-8") == "UNSET"
    assert "DREAM_ENABLED" not in os.environ
    assert not Path(os.environ["POLICY_STATUS"]).exists()


def test_stock_deriver_error_is_not_caught(deriver, monkeypatch):
    module, _marker, script = deriver
    script.write_text("raise RuntimeError('deriver failed')\n", encoding="utf-8")
    monkeypatch.setattr(policies_runtime, "install", lambda: None)
    with pytest.raises(RuntimeError, match="deriver failed"):
        module.main()
    assert "DREAM_ENABLED" not in os.environ
    assert not Path(os.environ["POLICY_STATUS"]).exists()


@pytest.mark.parametrize("mode,blocked", [("scheduler", True), ("stock", False)])
def test_already_loaded_honcho_settings_follow_the_block(deriver, tmp_path, monkeypatch, caplog, mode, blocked):
    # The rerank entrypoint, or install() itself, imports Honcho's settings before install() can fail; the
    # environment alone would be read too late, so the loaded flag must change as well.
    import types

    module, _marker, _script = deriver
    (tmp_path / "settings.json").write_text(json.dumps(_settings(mode=mode)) + "\n", encoding="utf-8")
    dream = types.SimpleNamespace(ENABLED=True)
    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(settings=types.SimpleNamespace(DREAM=dream)))
    status = _run_failed(module, monkeypatch, caplog)
    assert dream.ENABLED is (not blocked)
    assert status["dreams_blocked"] is blocked


def test_a_hook_module_that_cannot_be_imported_still_runs_intake(deriver, tmp_path, monkeypatch, caplog):
    # An import error in the hook itself is a hook failure: the stock deriver still runs, and with a peer switched off
    # dreams are blocked.
    module, marker, _script = deriver
    (tmp_path / "settings.json").write_text(json.dumps(_settings(peers={"observer": {"enabled": False}})) + "\n",
                                            encoding="utf-8")
    monkeypatch.setitem(sys.modules, "policies_runtime", None)
    monkeypatch.setattr(module, "policies_runtime", None)
    caplog.set_level(logging.ERROR)
    module.main()
    assert marker.read_text(encoding="utf-8") == "false"
    assert json.loads(Path(os.environ["POLICY_STATUS"]).read_text(encoding="utf-8"))["dreams_blocked"] is True


def test_passes_after_the_policies_tests_in_the_same_run():
    # The policies tests used to leave a namespace src.config in sys.modules, which this file's entrypoint read as
    # Honcho's loaded settings. Run the two in that order in one pytest process.
    import subprocess
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "tests/test_policies.py::test_instruction_files",
               "tests/test_hook_entrypoint.py::test_install_failure_scheduler_mode_blocks_dreams"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-500:]
