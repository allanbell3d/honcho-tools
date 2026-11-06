"""Hook tests. Fake Honcho modules only. No database and no live settings file."""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
import re
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "hooks"))

import policies_runtime as policies  # noqa: E402
from support import sample_settings, write_settings  # noqa: E402

# The archive regex (DATED_PEER_RE) anchors the JSON at the end of the line.
# One optional non-capturing group keeps groups 1..4 stable: date, time, peer, body.
OLD_DATED_PEER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) (\S+) <- \S+ (\{.*\})\s*$"
)
NEW_DATED_PEER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})Z? (\S+) <- \S+ (\{.*\})(?: mode=\S+ backend=\S+ gate=\S+)?\s*$"
)


@pytest.fixture(autouse=True)
def _restore_src_modules():
    # The hook imports src.config and src.utils.tokens on the way (instruction_cap, token_count); with the repository
    # root on sys.path those resolve to namespace packages that stay in sys.modules and confuse the next test file.
    saved = {name: module for name, module in sys.modules.items() if name == "src" or name.startswith("src.")}
    yield
    for name in [name for name in sys.modules if name == "src" or name.startswith("src.")]:
        if name not in saved:
            del sys.modules[name]
    sys.modules.update(saved)


def test_lookup_order_pair_hides_peers():
    data = sample_settings()
    merged, source = policies.lookup_block(data, "observer", "observed")
    assert merged["brief"] == 11
    assert merged["deduction_effort"] == "low"
    assert merged["deduction_rounds"] == 2
    assert "backend" not in merged
    assert source == "observed"
    data["pairs"]["observer/observed"]["deduction_effort"] = "high"
    merged, source = policies.lookup_block(data, "observer", "observed")
    assert source == "observer/observed"
    assert merged["deduction_effort"] == "high"
    assert merged["brief"] == 11


def test_empty_pair_uses_defaults_not_peer_fields():
    data = sample_settings()
    data["pairs"]["observer/observed"] = {}
    merged, source = policies.lookup_block(data, "observer", "observed")
    assert source == "observed"
    assert merged["brief"] == 8
    assert merged["deduction_effort"] == "low"


def test_peer_observer_then_observed_then_defaults():
    data = sample_settings()
    data["pairs"] = {}
    merged, source = policies.lookup_block(data, "observer", "observed")
    assert merged["brief"] == 8
    assert merged["deduction_effort"] == "low"
    assert source == "observed"
    data["peers"]["observer"]["deduction_effort"] = "high"
    merged, source = policies.lookup_block(data, "observer", "observed")
    assert source == "observer"
    assert merged["brief"] == 8
    assert merged["deduction_effort"] == "high"
    data["peers"]["observer"].pop("deduction_effort")
    data["peers"].pop("observer")
    merged, source = policies.lookup_block(data, "observer", "observed")
    assert source == "observed"
    assert merged["deduction_effort"] == "low"
    assert merged["brief"] == 4
    data["peers"] = {}
    merged, source = policies.lookup_block(data, "observer", "observed")
    assert source == "default"
    assert merged["induction_rounds"] == 3


def test_backend_chain_is_field_level():
    data = sample_settings()
    assert policies.lookup_backend(data, "observer", "observed") == "deepseek"
    data["pairs"]["observer/observed"]["backend"] = "openrouter:or-listed"
    assert policies.lookup_backend(data, "observer", "observed") == "openrouter:or-listed"
    data["pairs"]["observer/observed"]["backend"] = None
    data["peers"]["observer"]["backend"] = "codex"
    assert policies.lookup_backend(data, "observer", "observed") == "codex"
    data["peers"]["observer"]["backend"] = None
    data["peers"]["observed"]["backend"] = None
    data["defaults"]["backend"] = "claude"
    assert policies.lookup_backend(data, "observer", "observed") == "claude"
    data["defaults"]["backend"] = None
    assert policies.lookup_backend(data, "observer", "observed") == "codex"


def test_mode_and_enabled_gates():
    assert policies.decide_gate("stock", None, True, True) == "ran"
    assert policies.decide_gate("stock", "other", True, True) == "ran"
    assert policies.decide_gate("scheduler", None, True, True) == "skipped-mode"
    assert policies.decide_gate("scheduler", None, True, True, "manual") == "ran"
    assert policies.decide_gate("scheduler", None, True, True, "document_threshold") == "skipped-mode"
    assert policies.decide_gate("stock", None, True, True, "document_threshold") == "ran"
    assert policies.decide_gate("scheduler", "honcho-scheduler", True, True) == "ran"
    assert policies.decide_gate("scheduler", "honcho-scheduler", True, True, "document_threshold") == "ran"
    assert policies.decide_gate("scheduler", "other", True, True, "manual") == "skipped-mode"
    assert policies.decide_gate("stock", "honcho-scheduler", False, True) == "skipped-disabled"
    assert policies.decide_gate("scheduler", "honcho-scheduler", True, False) == "skipped-disabled"
    assert policies.peer_allows({"peers": {}}, "missing") is True
    assert policies.peer_allows({"peers": {"a": {"enabled": False}}}, "a") is False
    assert policies.peer_allows({"peers": {"a": {}}}, "a") is True


def test_used_line_keeps_old_json_keys_and_suffix():
    line = policies.format_used_line(
        "observer",
        "observed",
        "observer/observed",
        {"brief": 11, "deduction_effort": "max", "induction_effort": "low", "deduction_rounds": 2, "induction_rounds": 3},
        "stock",
        "openrouter:or-listed",
        "ran",
    )
    dated = "2026-10-30 12:00:00 " + line
    assert OLD_DATED_PEER_RE.match(dated) is None
    match = NEW_DATED_PEER_RE.match(dated)
    assert match is not None
    body = ast.literal_eval(match.group(4))
    assert body == {
        "max_conclusions": 11,
        "deduction_effort": "max",
        "induction_effort": "low",
        "deduction_rounds": 2,
        "induction_rounds": 3,
    }
    assert "true" not in match.group(4)
    assert "false" not in match.group(4)
    assert dated.endswith("mode=stock backend=openrouter:or-listed gate=ran")


def test_reload_on_mtime_and_secret_is_not_applied(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    log_path = tmp_path / "policy-used.log"
    write_settings(path, {"mode": "stock", "defaults": {"brief": 4, "deduction_effort": "low"}})
    store = policies.Policies(path, log_path)
    assert store.data["defaults"]["brief"] == 4
    write_settings(path, {"mode": "scheduler", "defaults": {"brief": 9, "deduction_effort": "high"}})
    os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2)
    store.reload()
    assert store.data["mode"] == "scheduler"
    assert store.data["defaults"]["brief"] == 9
    write_settings(path, {"mode": "stock", "defaults": {"deduction_effort": "sk-nope"}})
    os.utime(path, ns=(path.stat().st_mtime_ns + 2_000_000_000,) * 2)
    store.reload()
    assert store.data["mode"] == "scheduler"
    text = log_path.read_text(encoding="utf-8")
    assert "policies reloaded" in text
    assert "policies reloaded not applied:" in text
    assert "{" not in text
    assert "sk-" not in text
    monkeypatch.delenv("CONTROL_FILE", raising=False)


def test_hook_secret_scan_covers_active_not_backends(tmp_path):
    path = tmp_path / "settings.json"
    log_path = tmp_path / "policy-used.log"
    data = sample_settings()
    write_settings(path, data)
    store = policies.Policies(path, log_path)
    secret = "codex:sk-ABCDEFGHIJKLMNOPQRSTUVWX"
    data["providers"]["active"] = secret
    write_settings(path, data)
    os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2)
    store.reload()
    assert store.data["providers"]["active"] == "codex"
    text = log_path.read_text(encoding="utf-8")
    assert "policies reloaded not applied:" in text
    assert "sk-" not in text
    data["providers"]["active"] = "codex"
    data["providers"]["backends"]["codex"]["model"] = "sk-ABCDEFGHIJKLMNOPQRSTUVWX"
    write_settings(path, data)
    os.utime(path, ns=(path.stat().st_mtime_ns + 2_000_000_000,) * 2)
    store.reload()
    assert store.applied is True
    assert store.data["providers"]["backends"]["codex"]["model"] == "sk-ABCDEFGHIJKLMNOPQRSTUVWX"
    data["providers"]["backends"]["codex"]["model"] = "codex-model"
    data["providers"]["stages"] = {"dream.deduction": secret}
    write_settings(path, data)
    os.utime(path, ns=(path.stat().st_mtime_ns + 3_000_000_000,) * 2)
    store.reload()
    assert "stages" not in store.data.get("providers", {})
    assert "sk-" not in log_path.read_text(encoding="utf-8")


def test_instruction_files(tmp_path, monkeypatch):
    directory = tmp_path / "instructions"
    directory.mkdir()
    logs = []

    class Block:
        def __init__(self):
            self.custom_instructions = "from-db"
            self.custom_instructions_source = "database"

    class Config:
        def __init__(self):
            self.dream = Block()
            self.peer_card = Block()

    missing = tmp_path / "absent"
    config = Config()
    policies.apply_instructions(config, "codex", missing, logs.append)
    assert logs == []
    assert config.dream.custom_instructions == "from-db"

    (directory / "default-dream-instructions.md").write_text("default dream\n", encoding="utf-8")
    (directory / "default-peer-card-instructions.md").write_text("default card\n", encoding="utf-8")
    config = Config()
    policies.apply_instructions(config, "codex", directory, logs.append)
    assert config.dream.custom_instructions == "default dream\n"
    assert config.dream.custom_instructions_source == "settings.default.dream"
    assert config.peer_card.custom_instructions_source == "settings.default.peer-card"
    assert "instructions deprecated file=default-dream-instructions.md" in logs
    assert "instructions deprecated file=default-peer-card-instructions.md" in logs

    (directory / "codex-dream-instructions.md").write_text("codex dream\n", encoding="utf-8")
    config = Config()
    policies.apply_instructions(config, "codex", directory, logs.append)
    assert config.dream.custom_instructions == "codex dream\n"
    assert config.dream.custom_instructions_source == "settings.backend.codex.dream"
    assert config.peer_card.custom_instructions == "default card\n"
    assert "instructions deprecated file=codex-dream-instructions.md" in logs

    (directory / "codex-peer-card-instructions.md").write_text("   \n", encoding="utf-8")
    config = Config()
    policies.apply_instructions(config, "codex", directory, logs.append)
    assert config.peer_card.custom_instructions == ""
    assert config.peer_card.custom_instructions_source == "settings.backend.codex.peer-card"

    (directory / "codex-dream-instructions.md").write_text("too big\n", encoding="utf-8")
    monkeypatch.setattr(policies, "token_count", lambda text: 5000)
    monkeypatch.setattr(policies, "instruction_cap", lambda: 10)
    config = Config()
    policies.apply_instructions(config, "codex", directory, logs.append)
    assert config.dream.custom_instructions == "too big\n"
    warning = [line for line in logs if line.startswith("instructions over cap")]
    assert warning == ["instructions over cap category=dream backend=codex tokens=5000 cap=10"]
    assert "too big" not in warning[0]

    cached = directory / "codex-dream-instructions.md"
    cached.write_text("first\n", encoding="utf-8")
    policies._text_cache.clear()
    text, source = policies.resolve_instruction(directory, "codex", "dream")
    assert text == "first\n" and source == "settings.backend.codex.dream"
    stamp = cached.stat().st_mtime_ns
    cached.write_text("second\n", encoding="utf-8")
    os.utime(cached, ns=(stamp, stamp))
    again, _source = policies.resolve_instruction(directory, "codex", "dream")
    assert again == "first\n"
    os.utime(cached, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
    third, _source = policies.resolve_instruction(directory, "codex", "dream")
    assert third == "second\n"


def test_instruction_resolution_order(tmp_path):
    directory = tmp_path / "instructions"
    directory.mkdir()
    logs = []

    class Block:
        def __init__(self):
            self.custom_instructions = "from-db"
            self.custom_instructions_source = "database"

    class Config:
        def __init__(self):
            self.dream = Block()
            self.peer_card = Block()

    config = Config()
    policies.apply_instructions(config, "codex", directory, logs.append, "observer", "observed")
    assert config.dream.custom_instructions == "from-db"
    assert logs == []
    assert policies.resolve_instruction(directory, "codex", "reasoning", "observer", "observed") == (None, None)

    (directory / "default").mkdir()
    (directory / "default" / "dream.md").write_text("default dream\n", encoding="utf-8")
    config = Config()
    logs.clear()
    policies.apply_instructions(config, "codex", directory, logs.append, "observer", "observed")
    assert config.dream.custom_instructions == "default dream\n"
    assert config.dream.custom_instructions_source == "settings.default.dream"
    assert logs == []

    (directory / "codex-dream-instructions.md").write_text("flat dream\n", encoding="utf-8")
    config = Config()
    logs.clear()
    policies.apply_instructions(config, "codex", directory, logs.append, "observer", "observed")
    assert config.dream.custom_instructions == "flat dream\n"
    assert config.dream.custom_instructions_source == "settings.backend.codex.dream"
    assert logs == ["instructions deprecated file=codex-dream-instructions.md"]

    (directory / "backends" / "codex").mkdir(parents=True)
    (directory / "backends" / "codex" / "dream.md").write_text("backend dream\n", encoding="utf-8")
    config = Config()
    logs.clear()
    policies.apply_instructions(config, "codex", directory, logs.append, "observer", "observed")
    assert config.dream.custom_instructions == "backend dream\n"
    assert config.dream.custom_instructions_source == "settings.backend.codex.dream"
    assert logs == []

    (directory / "peers" / "observed").mkdir(parents=True)
    (directory / "peers" / "observed" / "dream.md").write_text("observed dream\n", encoding="utf-8")
    config = Config()
    logs.clear()
    policies.apply_instructions(config, "codex", directory, logs.append, "observer", "observed")
    assert config.dream.custom_instructions == "observed dream\n"
    assert config.dream.custom_instructions_source == "settings.peer.observed.dream"
    assert logs == []

    (directory / "peers" / "observer").mkdir(parents=True)
    (directory / "peers" / "observer" / "dream.md").write_text("   \n", encoding="utf-8")
    config = Config()
    logs.clear()
    policies.apply_instructions(config, "codex", directory, logs.append, "observer", "observed")
    assert config.dream.custom_instructions == ""
    assert config.dream.custom_instructions_source == "settings.peer.observer.dream"
    assert config.peer_card.custom_instructions == "from-db"
    assert logs == []


class _Overrides:
    def __init__(self):
        self.provider_params = {}


class _Model:
    def __init__(self, transport="openai"):
        self.transport = transport
        self.thinking_effort = "low"
        self.overrides = _Overrides()

    def model_copy(self, deep=False):
        copied = _Model(self.transport)
        copied.thinking_effort = self.thinking_effort
        if deep:
            copied.overrides.provider_params = copy.deepcopy(self.overrides.provider_params)
        return copied


class _Dream:
    BRIEF_MAX_NEW_CONCLUSION_TOKENS = 8000
    MAX_TOOL_ITERATIONS = 20
    NOTES_MODE = "off"
    NOTES_MAX_TOKENS = 3000
    BUDGET_ENABLED = False
    WRITE_WINDOW_ITERATIONS = None
    BRIEF_MAX_NEW_CONCLUSIONS = 1
    DEDUCTION_MAX_TOOL_ITERATIONS = 1
    INDUCTION_MAX_TOOL_ITERATIONS = 1
    DEDUCTION_MODEL_CONFIG = _Model()
    INDUCTION_MODEL_CONFIG = _Model()


class _Settings:
    def __init__(self):
        self.DREAM = _Dream()


class _Block:
    def __init__(self):
        self.custom_instructions = "from-db"
        self.custom_instructions_source = "database"


class _Config:
    def __init__(self):
        self.dream = _Block()
        self.peer_card = _Block()


class _Payload:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeRerankSettings:
    """The shape the hook relies on: model_dump, and a model_validate that refuses bad values."""

    DEFAULTS = dict(
        ENABLED=False, PROVIDER="voyage", MODEL="rerank-3-lite", BASE_URL=None, API_KEY=None, CANDIDATES=100,
        MAX_DOCUMENTS=None, TIMEOUT_SECONDS=5.0, CALLERS=["dream", "dialectic", "prefetch"], MIN_SCORE=None,
        DAILY_REQUEST_CAP=None, FAILURES_BEFORE_COOLDOWN=3, COOLDOWN_SECONDS=60.0,
    )
    model_fields = {name: None for name in DEFAULTS}

    def __init__(self, **values):
        self.__dict__.update(self.DEFAULTS)
        self.__dict__.update(values)

    def model_dump(self):
        return dict(self.__dict__)

    @classmethod
    def model_validate(cls, data):
        if not 1 <= data["CANDIDATES"] <= 1000:
            raise ValueError("CANDIDATES")
        if data.get("BASE_URL") and "[" in data["BASE_URL"]:
            raise ValueError("BASE_URL")
        return cls(**data)


class _AppSettings:
    def __init__(self):
        self.RERANK = _FakeRerankSettings()
        self.CACHE = types.SimpleNamespace(ENABLED=True)


def _install_fakes(tmp_path, monkeypatch, data, without_rerank=False, install=True):
    path = write_settings(tmp_path / "settings.json", data)
    (tmp_path / "instructions" / "backends" / "deepseek").mkdir(parents=True)
    (tmp_path / "instructions" / "backends" / "deepseek" / "dream.md").write_text("deepseek dream\n", encoding="utf-8")
    monkeypatch.setenv("CONTROL_FILE", str(path))
    monkeypatch.setenv("POLICY_LOG", str(tmp_path / "policy-used.log"))
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "hook-status.json"))
    names = (
        "src",
        "src.dreamer",
        "src.dreamer.orchestrator",
        "src.dreamer.specialists",
        "src.dreamer.working_memory",
        "src.honcho_rerank",
    )
    saved = {name: sys.modules.get(name) for name in names}
    src = types.ModuleType("src")
    dreamer = types.ModuleType("src.dreamer")
    orch = types.ModuleType("src.dreamer.orchestrator")
    spec = types.ModuleType("src.dreamer.specialists")
    work = types.ModuleType("src.dreamer.working_memory")
    rerank = types.ModuleType("src.honcho_rerank")
    rerank.settings = _AppSettings()
    seen = {"runs": [], "kwargs": []}
    settings = _Settings()

    def get_configuration(message_configuration=None, session=None, workspace=None):
        return _Config()

    async def run_dream(workspace_name, observer, observed, *args, **kwargs):
        seen["runs"].append(("omni", observer, observed))
        seen["kwargs"].append(dict(kwargs))
        seen["config"] = orch.get_configuration()
        seen["brief"] = orch.settings.DREAM.BRIEF_MAX_NEW_CONCLUSIONS
        seen["model"] = orch.settings.DREAM.DEDUCTION_MODEL_CONFIG
        seen["rerank"] = rerank.settings.RERANK
        seen["cache"] = rerank.settings.CACHE.ENABLED
        return "ran"

    async def run_card(workspace_name, observer, observed, *args, **kwargs):
        seen["runs"].append(("card", observer, observed))
        seen["kwargs"].append(dict(kwargs))
        return "card"

    async def process_dream(payload, workspace_name="ws", *, queue_item_id=None):
        # Honcho reads working_memory_enabled() again after run_dream, in its bookkeeping.
        seen.setdefault("process_notes", []).append(orch.settings.DREAM.NOTES_MODE)
        common = dict(
            dream_type=getattr(payload, "dream_type", "omni"),
            trigger_reason=getattr(payload, "trigger_reason", None),
            delay_reason=None,
            documents_since_last_dream_at_schedule=0,
            document_threshold=1,
            queue_item_id="q",
        )
        if common["dream_type"] == "omni":
            return await orch.run_dream("ws", payload.observer, payload.observed, **common)
        return await orch.run_card_refresh_dream("ws", payload.observer, payload.observed, **common)

    orch.get_configuration = get_configuration
    orch.run_dream = run_dream
    orch.run_card_refresh_dream = run_card
    orch.process_dream = process_dream
    orch.settings = settings
    spec.settings = settings
    work.settings = settings
    dreamer.orchestrator = orch
    dreamer.specialists = spec
    dreamer.working_memory = work
    dreamer.process_dream = process_dream
    src.dreamer = dreamer
    src.honcho_rerank = rerank
    for name, module in (
        ("src", src),
        ("src.dreamer", dreamer),
        ("src.dreamer.orchestrator", orch),
        ("src.dreamer.specialists", spec),
        ("src.dreamer.working_memory", work),
        ("src.honcho_rerank", rerank),
    ):
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    if without_rerank:
        del src.honcho_rerank
        sys.modules.pop("src.honcho_rerank", None)
    if install:
        policies.install()
    return saved, seen, dreamer, orch, path


def _restore(saved):
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def test_install_gates_mark_and_instructions(tmp_path, monkeypatch):
    data = sample_settings()
    data["pairs"]["observer/observed"]["deduction_effort"] = "medium"
    data["pairs"]["observer/observed"]["induction_effort"] = "medium"
    saved, seen, dreamer, orch, path = _install_fakes(tmp_path, monkeypatch, data)
    try:
        outside = orch.get_configuration()
        assert outside.dream.custom_instructions == "from-db"
        assert outside.dream.custom_instructions_source == "database"

        result = asyncio.run(dreamer.process_dream(_Payload(observer="observer", observed="observed", dream_type="omni")))
        assert result == "ran"
        assert "scheduled_by" not in seen["kwargs"][-1]
        assert seen["brief"] == 11
        extra = seen["model"].overrides.provider_params["extra_body"]
        assert extra["backend"] == "deepseek"
        assert extra["observer"] == "observer"
        assert extra["observed"] == "observed"
        assert extra["output_config"]["effort"] == "medium"
        assert seen["model"].thinking_effort == "medium"
        assert seen["config"].dream.custom_instructions == "deepseek dream\n"
        assert seen["config"].dream.custom_instructions_source == "settings.backend.deepseek.dream"

        asyncio.run(dreamer.process_dream(_Payload(observer="observer", observed="observed", dream_type="card_refresh")))
        assert seen["runs"][-1][0] == "card"

        data["mode"] = "scheduler"
        write_settings(path, data)
        os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2)
        before = len(seen["runs"])
        skipped = asyncio.run(dreamer.process_dream(_Payload(observer="observer", observed="observed")))
        assert skipped is None
        assert len(seen["runs"]) == before
        marked = asyncio.run(
            dreamer.process_dream(_Payload(observer="observer", observed="observed", scheduled_by="honcho-scheduler"))
        )
        assert marked == "ran"
        direct = asyncio.run(
            orch.run_dream(
                "ws",
                "observer",
                "observed",
                scheduled_by="honcho-scheduler",
                dream_type="omni",
                trigger_reason="manual",
                delay_reason=None,
                documents_since_last_dream_at_schedule=0,
                document_threshold=1,
                queue_item_id="q",
            )
        )
        assert direct == "ran"
        assert "scheduled_by" not in seen["kwargs"][-1]

        api = asyncio.run(
            dreamer.process_dream(
                _Payload(observer="observer", observed="observed", trigger_reason="manual")
            )
        )
        assert api == "ran"
        before = len(seen["runs"])
        internal = asyncio.run(
            dreamer.process_dream(
                _Payload(
                    observer="observer",
                    observed="observed",
                    trigger_reason="document_threshold",
                )
            )
        )
        assert internal is None
        assert len(seen["runs"]) == before

        data["mode"] = "stock"
        write_settings(path, data)
        os.utime(path, ns=(path.stat().st_mtime_ns + 2_000_000_000,) * 2)
        stock_internal = asyncio.run(
            dreamer.process_dream(
                _Payload(
                    observer="observer",
                    observed="observed",
                    trigger_reason="document_threshold",
                )
            )
        )
        assert stock_internal == "ran"
        before = len(seen["runs"])
        disabled = asyncio.run(dreamer.process_dream(_Payload(observer="observer", observed="off")))
        assert disabled is None
        assert len(seen["runs"]) == before

        policies.install()
        assert getattr(orch.run_dream, "__policies_installed__", False)
        log_text = (tmp_path / "policy-used.log").read_text(encoding="utf-8")
        assert "gate=ran" in log_text
        assert "gate=skipped-mode" in log_text
        assert "gate=skipped-disabled" in log_text
        assert "mode=scheduler" in log_text
        assert "unmarked dreams are skipped" not in log_text
        assert "{" in log_text
        for line in log_text.splitlines():
            if "policies reloaded" in line:
                assert "{" not in line
    finally:
        _restore(saved)


def test_archive_skip_line_has_no_braces(tmp_path):
    path = write_settings(tmp_path / "settings.json", sample_settings())
    store = policies.Policies(path, tmp_path / "policy-used.log")
    store.log_decision(store.decide("observer", "observed", None))
    lines = (tmp_path / "policy-used.log").read_text(encoding="utf-8").splitlines()
    reloads = [line for line in lines if "policies reloaded" in line]
    assert reloads
    assert all("{" not in line for line in reloads)
    peer = [line for line in lines if "<-" in line][0]
    assert NEW_DATED_PEER_RE.match(peer)
    assert OLD_DATED_PEER_RE.match(peer) is None


def test_log_line_timestamp_is_utc(tmp_path, monkeypatch):
    epoch = time.gmtime(0)
    monkeypatch.setattr(policies.time, "gmtime", lambda *args: epoch)
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    path = write_settings(tmp_path / "settings.json", sample_settings())
    log_path = tmp_path / "policy-used.log"
    policies.Policies(path, log_path)
    line = log_path.read_text(encoding="utf-8").splitlines()[-1]
    assert line.startswith("1970-01-01 00:00:00Z ")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scheduler"))
    import dream_nas
    monkeypatch.setattr(dream_nas.time, "gmtime", lambda *args: epoch)
    data = sample_settings()
    data["mode"] = "stock"
    settings = tmp_path / "scheduler-settings.json"
    settings.write_text(json.dumps(data), encoding="utf-8")
    dream_log = tmp_path / "dream-nas.log"
    monkeypatch.setenv("CONTROL_FILE", str(settings))
    monkeypatch.setenv("SCHEDULER_LOG", str(dream_log))
    monkeypatch.delenv("HONCHO_WORKSPACE", raising=False)
    handlers = {}
    monkeypatch.setattr(dream_nas.signal, "signal", lambda *_args: handlers.update(handler=_args[1]))
    monkeypatch.setattr(dream_nas, "_sleep", lambda seconds, halted: handlers["handler"]())
    dream_nas.main()
    dream_line = dream_log.read_text(encoding="utf-8").splitlines()[-1]
    assert dream_line.startswith("1970-01-01 00:00:00Z ")


def test_dream_limits_resolve_per_dream_and_fall_back_to_honcho(tmp_path):
    data = sample_settings()
    data["defaults"]["notes"] = "on"
    data["peers"].setdefault("Ann", {"enabled": True})["brief_tokens"] = 20000
    data["pairs"]["Ann/Ann"] = {"tool_calls": 12, "budget": True}
    policy, _source = policies.lookup_block(data, "Ann", "Ann")
    assert policy["brief_tokens"] == 20000
    assert policy["tool_calls"] == 12
    assert policy["budget"] is True
    assert policy["notes"] == "on"
    assert "notes_tokens" not in policy

    class Dream:
        BRIEF_MAX_NEW_CONCLUSION_TOKENS = 8000
        MAX_TOOL_ITERATIONS = 20
        NOTES_MODE = "off"
        NOTES_MAX_TOKENS = 3000
        BUDGET_ENABLED = False
        WRITE_WINDOW_ITERATIONS = None

    proxy = policies.DreamSettingsProxy(Dream())
    decision = policies.Decision(policy=policy, source="Ann/Ann", mode="stock", backend="none", backend_name=None, gate="ran", observer="Ann", observed="Ann")
    token = policies._decision.set(decision)
    try:
        assert proxy.BRIEF_MAX_NEW_CONCLUSION_TOKENS == 20000
        assert proxy.MAX_TOOL_ITERATIONS == 12
        assert proxy.BUDGET_ENABLED is True
        assert proxy.NOTES_MODE == "on"
        assert proxy.NOTES_MAX_TOKENS == 3000
        assert proxy.WRITE_WINDOW_ITERATIONS is None
    finally:
        policies._decision.reset(token)
    assert proxy.BRIEF_MAX_NEW_CONCLUSION_TOKENS == 8000
    line = policies.format_used_line("Ann", "Ann", "Ann/Ann", policy, "stock", "none", "ran")
    assert '"brief_tokens": 20000' in line and '"tool_calls": 12' in line


def test_dream_limit_out_of_range_is_not_applied(tmp_path):
    data = sample_settings()
    data["defaults"]["tool_calls"] = 500
    path = tmp_path / "settings.json"
    write_settings(path, data)
    loaded = policies.Policies(path=path, log_path=tmp_path / "policy.log")
    assert "tool_calls" not in loaded.data.get("defaults", {})
    assert "not applied: ValueError" in (tmp_path / "policy.log").read_text(encoding="utf-8")


def test_lookup_rerank_resolves_key_by_key_and_names_the_block():
    data = sample_settings()
    assert policies.lookup_rerank(data, "observer", "observed") == ({}, None)
    data["defaults"]["rerank"] = {"candidates": 40, "daily_request_cap": 10, "enabled": False}
    data["peers"]["observed"]["rerank"] = {"provider": "tei", "base_url": "http://reranker.example:8080", "model": "bge"}
    data["peers"]["observer"]["rerank"] = {"enabled": True, "candidates": None}
    data["pairs"]["observer/observed"]["rerank"] = {"candidates": 7, "api_key": "ignored-here"}
    merged, source = policies.lookup_rerank(data, "observer", "observed")
    assert merged == {
        "enabled": True, "provider": "tei", "model": "bge", "base_url": "http://reranker.example:8080",
        "candidates": 7, "daily_request_cap": 10,
    }
    assert source == "observer/observed"
    merged, source = policies.lookup_rerank(data, "observed", "observed")
    assert merged["candidates"] == 40 and merged["enabled"] is False and merged["provider"] == "tei"
    assert source == "observed"
    line = policies.format_rerank_line("observer", "observed", source, merged)
    assert "{" not in line and "<-" not in line
    assert NEW_DATED_PEER_RE.match("2026-10-31 10:00:00 " + line) is None
    assert line.startswith("rerank for observer/observed from observed: enabled=False provider=tei")
    assert "reranker.example" not in line
    assert "base_url=set" in line


def test_base_url_follows_the_forks_rule_and_is_never_logged():
    good = ["http://reranker.example:8080", "https://api.cohere.com/v2", "https://reranker.example/v1/", "http://10.0.0.1:7997"]
    for url in good:
        assert policies.valid_base_url(url), url
    bad = [
        "https://user:ordinary-password@reranker.example/v1",  # userinfo
        "https://reranker.example/v1?api_key=ordinary-password",  # query
        "https://reranker.example/v1#token",  # fragment
        "ftp://reranker.example", "reranker.example:8080", "https://", "https:///v1",
        "https://reranker.example/v1\n", "https://reranker.example/a b", "https://reranker.\x00example", "", None, 7,
    ]
    for url in bad:
        assert not policies.valid_base_url(url), url
        if url is None:
            continue  # null means absent, like every other key
        with pytest.raises(ValueError):
            policies.Policies._check_rerank({"base_url": url})
    line = policies.format_rerank_line("a", "b", "default", {"base_url": "https://reranker.example/v1?api_key=ordinary-password", "enabled": True})
    assert line == "rerank for a/b from default: enabled=True base_url=set"
    assert "ordinary-password" not in line and "reranker.example" not in line


def test_rerank_block_checks_follow_rerank_settings(tmp_path):
    good = {
        "enabled": True, "provider": "cohere", "model": "rerank-v3.5", "base_url": "https://api.cohere.com/v2",
        "candidates": 1000, "max_documents": 10000, "timeout_seconds": 0.5, "min_score": -1.5,
        "daily_request_cap": 0,
    }
    policies.Policies._check_rerank(good)
    policies.Policies._check_rerank(None)
    policies.Policies._check_rerank({"candidates": None})
    bad = [
        {"api_key": "x"}, {"API_KEY": "x"}, {"key_env": "RERANK_API_KEY"}, {"callers": ["dream"]}, "on",
        {"enabled": 1}, {"provider": "openai"}, {"model": " "}, {"base_url": "reranker:8080"},
        {"candidates": 0}, {"candidates": 1001}, {"candidates": 10.0}, {"max_documents": 0},
        {"timeout_seconds": 0}, {"timeout_seconds": float("inf")}, {"min_score": float("nan")},
        {"daily_request_cap": -1}, {"failures_before_cooldown": 0}, {"cooldown_seconds": -1},
    ]
    for block in bad:
        with pytest.raises(ValueError):
            policies.Policies._check_rerank(block)
    data = sample_settings()
    data["pairs"]["observer/observed"]["rerank"] = {"candidates": 5000}
    path = write_settings(tmp_path / "settings.json", data)
    loaded = policies.Policies(path=path, log_path=tmp_path / "policy.log")
    assert loaded.data["defaults"]["brief"] == 4
    assert "rerank" not in loaded.data.get("pairs", {}).get("observer/observed", {})
    assert "rerank settings not applied:" in (tmp_path / "policy.log").read_text(encoding="utf-8")


def test_bad_rerank_block_still_lets_dreams_run(tmp_path):
    data = sample_settings()
    data["mode"] = "scheduler"
    data["rerank"] = {"enabled": False, "candidates": 40}
    path = write_settings(tmp_path / "settings.json", data)
    loaded = policies.Policies(path=path, log_path=tmp_path / "policy.log")
    assert loaded.decide("observer", "observed", "honcho-scheduler").gate == "ran"
    data["mode"] = "stock"
    data["providers"]["backends"]["codex"]["model"] = "sk-not-for-the-hook"
    data["rerank"] = {"candidates": 5000}
    write_settings(path, data)
    os.utime(path, ns=(path.stat().st_mtime_ns + 1_000_000_000,) * 2)
    loaded.reload()
    decision = loaded.decide("observer", "observed", None, "document_threshold")
    assert decision.gate == "ran"
    assert decision.mode == "stock"
    assert loaded.data["rerank"]["candidates"] == 40


def test_rerank_proxy_applies_only_inside_a_dream_and_keeps_base_on_a_bad_value():
    lines = []
    app = _AppSettings()
    app.RERANK = _FakeRerankSettings(ENABLED=True)
    proxy = policies.RerankSettingsProxy(app, lines.append)
    assert proxy.RERANK is app.RERANK
    assert proxy.CACHE.ENABLED is True
    decision = policies.Decision(
        policy={}, source="default", mode="stock", backend="none", backend_name=None, gate="ran",
        observer="a", observed="b", rerank={"enabled": True, "candidates": 7, "provider": "tei"}, rerank_source="default",
    )
    token = policies._decision.set(decision)
    try:
        inside = proxy.RERANK
        assert inside is not app.RERANK
        assert inside.ENABLED is True and inside.CANDIDATES == 7 and inside.PROVIDER == "tei"
        assert inside.MODEL == "rerank-3-lite" and inside.TIMEOUT_SECONDS == 5.0
        assert proxy.RERANK is inside
    finally:
        policies._decision.reset(token)
    assert proxy.RERANK is app.RERANK
    assert app.RERANK.CANDIDATES == 100 and app.RERANK.ENABLED is True
    assert lines == []
    empty = policies.Decision(policy={}, source="default", mode="stock", backend="none", backend_name=None, gate="ran", observer="a", observed="b")
    token = policies._decision.set(empty)
    try:
        assert proxy.RERANK is app.RERANK
    finally:
        policies._decision.reset(token)
    bad = policies.Decision(
        policy={}, source="default", mode="stock", backend="none", backend_name=None, gate="ran",
        observer="a", observed="b", rerank={"base_url": "http://[bad", "candidates": 9}, rerank_source="a/b",
    )
    token = policies._decision.set(bad)
    try:
        assert proxy.RERANK is app.RERANK
        assert proxy.RERANK.CANDIDATES == 100
    finally:
        policies._decision.reset(token)
    assert lines == ["rerank not applied a/b from a/b: ValueError"]


def test_a_dream_cannot_turn_rerank_on_or_take_the_key_elsewhere():
    lines = []
    off = policies.Decision(
        policy={}, source="default", mode="stock", backend="none", backend_name=None, gate="ran",
        observer="a", observed="b", rerank={"enabled": True}, rerank_source="a/b",
    )
    resolved = policies.resolve_rerank(_FakeRerankSettings(ENABLED=False, API_KEY="test-key"), off, lines.append)
    assert resolved.ENABLED is False
    assert lines == ["rerank kept off: the global switch is off"]
    elsewhere = policies.Decision(
        policy={}, source="default", mode="stock", backend="none", backend_name=None, gate="ran",
        observer="a", observed="b", rerank={"provider": "tei", "base_url": "http://reranker.example:8080"},
        rerank_source="a/b",
    )
    base = _FakeRerankSettings(ENABLED=True, API_KEY="test-key")
    assert policies.resolve_rerank(base, elsewhere, lines.append).API_KEY is None
    same = policies.Decision(
        policy={}, source="default", mode="stock", backend="none", backend_name=None, gate="ran",
        observer="a", observed="b", rerank={"candidates": 7}, rerank_source="a/b",
    )
    assert policies.resolve_rerank(base, same, lines.append).API_KEY == "test-key"


def test_rejected_settings_at_start_skip_every_dream(tmp_path):
    # Falling back to stock would let Honcho's own dreams run in a deployment set to scheduler.
    path = tmp_path / "settings.json"
    path.write_text('{"mode": "scheduler", "defaults": {"induction_effort": "huge"}}', encoding="utf-8")
    loaded = policies.Policies(path=path, log_path=tmp_path / "policy.log")
    assert loaded.decide("a", "a", None, "document_threshold").gate == "skipped-settings"
    missing = policies.Policies(path=tmp_path / "none.json", log_path=tmp_path / "policy.log")
    assert missing.decide("a", "a", None, "document_threshold").gate == "ran"


def test_install_replaces_wrapper_rerank_settings_per_dream(tmp_path, monkeypatch):
    monkeypatch.setitem(_FakeRerankSettings.DEFAULTS, "ENABLED", True)
    data = sample_settings()
    data["defaults"]["rerank"] = {"candidates": 40, "daily_request_cap": 10}
    data["peers"]["observer"]["rerank"] = {"enabled": True}
    data["pairs"]["observer/observed"]["rerank"] = {"candidates": 7}
    saved, seen, dreamer, orch, path = _install_fakes(tmp_path, monkeypatch, data)
    try:
        rerank = sys.modules["src.honcho_rerank"]
        assert isinstance(rerank.settings, policies.RerankSettingsProxy)
        base = rerank.settings.RERANK
        assert isinstance(base, _FakeRerankSettings) and base.CANDIDATES == 100
        assert asyncio.run(dreamer.process_dream(_Payload(observer="observer", observed="observed", dream_type="omni"))) == "ran"
        assert seen["rerank"].CANDIDATES == 7
        assert seen["rerank"].ENABLED is True
        assert seen["rerank"].DAILY_REQUEST_CAP == 10
        assert seen["rerank"].API_KEY is None
        assert seen["cache"] is True
        assert rerank.settings.RERANK is base
        assert asyncio.run(dreamer.process_dream(_Payload(observer="off", observed="off", dream_type="omni"))) is None
        policies.install()
        assert isinstance(rerank.settings, policies.RerankSettingsProxy) and rerank.settings._base is not rerank.settings
        log_text = (tmp_path / "policy-used.log").read_text(encoding="utf-8")
        assert "rerank hook installed" in log_text
        assert "rerank for observer/observed from observer/observed: enabled=True candidates=7 daily_request_cap=10" in log_text
        assert "rerank for off/off" not in log_text
        for line in log_text.splitlines():
            if "rerank" in line:
                assert "{" not in line and "<-" not in line
    finally:
        _restore(saved)


def test_install_without_rerank_package_gates_a_dream(tmp_path, monkeypatch):
    data = sample_settings()
    data["mode"] = "scheduler"
    data["rerank"] = {"candidates": 5000}
    saved, seen, dreamer, orch, path = _install_fakes(tmp_path, monkeypatch, data, without_rerank=True, install=False)
    try:
        policies.install()
        assert getattr(orch.run_dream, "__policies_installed__", False)
        skipped = asyncio.run(dreamer.process_dream(_Payload(observer="observer", observed="observed")))
        assert skipped is None
        marked = asyncio.run(
            dreamer.process_dream(_Payload(observer="observer", observed="observed", scheduled_by="honcho-scheduler"))
        )
        assert marked == "ran"
        log_text = (tmp_path / "policy-used.log").read_text(encoding="utf-8")
        assert "rerank package not installed; dream hook runs without it" in log_text
        assert "FATAL" not in log_text
    finally:
        _restore(saved)


def test_absent_rerank_package_writes_no_rerank_line(tmp_path):
    data = sample_settings()
    data["defaults"]["rerank"] = {"model": "bge"}
    path = write_settings(tmp_path / "settings.json", data)
    loaded = policies.Policies(path, tmp_path / "policy-used.log", check_rerank=False)
    decision = loaded.decide("observer", "observed", None)
    loaded.log_decision(decision)
    log_text = (tmp_path / "policy-used.log").read_text(encoding="utf-8")
    assert "rerank for" not in log_text
    assert decision.rerank == {}


@pytest.mark.parametrize("base_cap,override,expected", [(1000, 5000, 1000), (None, 5000, 5000), (1000, 500, 500), (0, 5000, 0)])
def test_rerank_cap_clamp(base_cap, override, expected):
    base = _FakeRerankSettings(DAILY_REQUEST_CAP=base_cap)
    decision = policies.Decision(policy={}, source="default", mode="stock", backend="none", backend_name=None,
                                 gate="ran", observer="a", observed="b", rerank={"daily_request_cap": override})
    lines = []
    resolved = policies.resolve_rerank(base, decision, lines.append)
    assert resolved.DAILY_REQUEST_CAP == expected
    assert base.DAILY_REQUEST_CAP == base_cap
    assert policies.resolve_rerank(base, decision, lines.append) is resolved
    assert lines == ([f"rerank cap clamped: dream={override} base={base_cap}"] if expected != override else [])


@pytest.mark.parametrize("name", ["failures_before_cooldown", "cooldown_seconds"])
def test_rerank_cooldown_is_not_a_dream_override(name):
    with pytest.raises(ValueError, match=name):
        policies.Policies._check_rerank({name: 3})
    assert policies.lookup_rerank({"defaults": {"rerank": {name: 3}}}, "a", "b") == ({}, None)


@pytest.mark.parametrize("name", ["deduction_rounds", "induction_rounds"])
def test_rounds_above_schema_limit_are_logged_and_not_applied(tmp_path, name):
    data = sample_settings()
    data["defaults"][name] = 101
    path = write_settings(tmp_path / "settings.json", data)
    loaded = policies.Policies(path, tmp_path / "policy.log")
    assert name not in loaded.data.get("defaults", {})
    assert f"{name}: must be between 1 and 100" in (tmp_path / "policy.log").read_text(encoding="utf-8")
    data["defaults"][name] = 100
    write_settings(path, data)
    loaded = policies.Policies(path, tmp_path / "accepted.log")
    assert loaded.data["defaults"][name] == 100


@pytest.mark.parametrize("missing", ["NOTES_MODE", "CANDIDATES", "run_card_refresh_dream", "process_dream.signature", "run_dream.async"])
def test_install_checks_before_patching(tmp_path, monkeypatch, missing):
    saved, seen, dreamer, orch, path = _install_fakes(tmp_path, monkeypatch, sample_settings(), install=False)
    try:
        if missing == "NOTES_MODE":
            monkeypatch.delattr(_Dream, missing)
        elif missing == "CANDIDATES":
            monkeypatch.setattr(_FakeRerankSettings, "model_fields", {k: None for k in _FakeRerankSettings.DEFAULTS if k != missing})
        elif missing == "run_card_refresh_dream":
            del orch.run_card_refresh_dream
        elif missing == "process_dream.signature":
            async def changed(renamed):
                pass
            orch.process_dream = changed
        else:
            orch.run_dream = lambda workspace_name, observer, observed: None
        with pytest.raises(RuntimeError, match=missing.split(".")[0]):
            policies.install()
        assert not isinstance(orch.settings, policies.SettingsProxy)
    finally:
        _restore(saved)


def test_hook_status_preserves_last_good_mtime(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    monkeypatch.setenv("HONCHO_FORK_SHA", "a" * 40)
    path = write_settings(tmp_path / "settings.json", sample_settings())
    store = policies.Policies(path, tmp_path / "policy.log")
    store.mark_installed()
    first = json.loads((tmp_path / "status.json").read_text())
    assert first["checks_passed"] is True and first["installed_at"]
    assert first["fork_sha"] == "a" * 40
    assert first["settings_mtime_applied"] == path.stat().st_mtime_ns
    path.write_text('{"defaults":{"deduction_effort":"sk-private-value"}}', encoding="utf-8")
    os.utime(path, ns=(path.stat().st_mtime_ns + 2_000_000_000,) * 2)
    store.reload()
    failed_text = (tmp_path / "status.json").read_text()
    failed = json.loads(failed_text)
    assert failed["settings_mtime_applied"] == first["settings_mtime_applied"]
    assert failed["last_reload_error"] == "ValueError"
    assert "sk-private-value" not in failed_text
    write_settings(path, sample_settings())
    os.utime(path, ns=(path.stat().st_mtime_ns + 4_000_000_000,) * 2)
    store.reload()
    recovered = json.loads((tmp_path / "status.json").read_text())
    assert recovered["last_reload_error"] is None
    assert recovered["settings_mtime_applied"] == path.stat().st_mtime_ns



def test_hook_status_write_failure_logs_error_and_keeps_running(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("POLICY_STATUS", str(blocker / "status.json"))
    path = write_settings(tmp_path / "settings.json", sample_settings())
    store = policies.Policies(path, tmp_path / "policy.log")
    store.mark_installed()
    assert store.installed_at
    assert any(record.levelname == "ERROR" and "hook status" in record.message for record in caplog.records)



def test_completed_run_records_effective_settings_without_url_or_key(tmp_path, monkeypatch):
    data = sample_settings()
    data["defaults"]["rerank"] = {"candidates": 9, "daily_request_cap": 5000}
    path = write_settings(tmp_path / "settings.json", data)
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    store = policies.Policies(path, tmp_path / "policy.log")
    store.base_dream = _Dream()
    store.rerank_settings = types.SimpleNamespace(RERANK=_FakeRerankSettings(
        DAILY_REQUEST_CAP=1000, BASE_URL="https://private.example", API_KEY="private-value",
    ))
    async def run(workspace_name, observer, observed):
        return types.SimpleNamespace(run_id="run-123")
    result = asyncio.run(policies._wrap_run(run, store)("ws", "observer", "observed"))
    assert result.run_id == "run-123"
    text = (tmp_path / "runs.log").read_text()
    row = json.loads(text)
    assert row["run_id"] == "run-123" and row["observer"] == "observer"
    assert row["brief"] == data["pairs"]["observer/observed"]["brief"]
    assert row["rerank"]["candidates"] == 9 and row["rerank"]["daily_request_cap"] == 1000
    assert row["rerank"]["base_url_set"] is True
    assert row["started"] <= row["finished"]
    assert "private.example" not in text and "private-value" not in text
    assert policies._decision.get() is None



def test_run_observation_failure_keeps_successful_result(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    path = write_settings(tmp_path / "settings.json", sample_settings())
    store = policies.Policies(path, tmp_path / "policy.log")
    expected = types.SimpleNamespace(run_id="run-123")
    async def run(workspace_name, observer, observed):
        return expected
    def broken(*args):
        raise ValueError("private-value")
    monkeypatch.setattr(store, "log_run", broken)
    assert asyncio.run(policies._wrap_run(run, store)("ws", "observer", "observed")) is expected
    assert "private-value" not in caplog.text
    assert "could not be collected" in caplog.text
    assert policies._decision.get() is None



@pytest.mark.parametrize("override", [None, "default", "peer", "pair"])
def test_dream_stage_aliases_preserve_explicit_overrides(tmp_path, monkeypatch, override):
    data = sample_settings()
    for block in [data["defaults"], *data["peers"].values(), *data["pairs"].values()]:
        block.pop("backend", None)
    data["providers"]["stages"] = {"dream.deduction": "codex:one", "dream.induction": "codex:two", "peer_card": "codex:three"}
    if override:
        block = {"default": data["defaults"], "peer": data["peers"]["observer"], "pair": data["pairs"]["observer/observed"]}[override]
        block["backend"] = "codex:explicit"
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    store = policies.Policies(write_settings(tmp_path / "settings.json", data), tmp_path / "policy.log")
    decision = store.decide("observer", "observed", None)
    token = policies._decision.set(decision)
    try:
        proxy = policies.DreamSettingsProxy(_Dream())
        def backend(name):
            return getattr(proxy, name).overrides.provider_params["extra_body"]["backend"]
        assert backend("DEDUCTION_MODEL_CONFIG") == ("codex:explicit" if override else "@dream.deduction")
        assert backend("INDUCTION_MODEL_CONFIG") == ("codex:explicit" if override else "@dream.induction")
        decision.card_refresh = True
        assert backend("DEDUCTION_MODEL_CONFIG") == ("codex:explicit" if override else "@peer_card")
    finally:
        policies._decision.reset(token)


def test_a_providers_value_that_is_not_an_object_does_not_break_a_dream(tmp_path, monkeypatch):
    # The schema refuses it, a hand edit does not: the hook reads it as no stage entries, like the bridge.
    data = sample_settings()
    data["providers"] = ["not", "an", "object"]
    monkeypatch.setenv("POLICY_STATUS", str(tmp_path / "status.json"))
    store = policies.Policies(write_settings(tmp_path / "settings.json", data), tmp_path / "policy.log")
    decision = store.decide("observer", "observed", None)
    assert decision.stage_backends == {}


def test_install_wraps_consumer_imported_before_the_hook(tmp_path, monkeypatch):
    data = sample_settings()
    data["mode"] = "scheduler"
    saved, seen, dreamer, orch, path = _install_fakes(tmp_path, monkeypatch, data, install=False)
    try:
        deriver = types.ModuleType("src.deriver")
        consumer_module = types.ModuleType("src.deriver.consumer")
        exec("from src.dreamer import process_dream", consumer_module.__dict__)
        deriver.consumer = consumer_module
        monkeypatch.setitem(sys.modules, "src.deriver", deriver)
        monkeypatch.setitem(sys.modules, "src.deriver.consumer", consumer_module)
        from src.deriver import consumer

        original = consumer.process_dream
        policies.install()
        assert consumer.process_dream is dreamer.process_dream is orch.process_dream
        assert consumer.process_dream is not original
        payload = _Payload(observer="observer", observed="observed", scheduled_by="honcho-scheduler")
        assert asyncio.run(consumer.process_dream(payload)) == "ran"
        assert seen["runs"] == [("omni", "observer", "observed")]
        assert policies._payload_mark.get() is None
        assert policies._payload_reason.get() is policies._UNSET
        policies.install()
        assert consumer.process_dream is dreamer.process_dream
    finally:
        _restore(saved)



@pytest.mark.parametrize("overrides", [{}, {"daily_request_cap": 5000}])
def test_run_log_uses_reloaded_base_rerank_settings(tmp_path, monkeypatch, overrides):
    data = sample_settings()
    data["defaults"]["rerank"] = overrides
    saved, seen, dreamer, orch, path = _install_fakes(tmp_path, monkeypatch, data, install=False)
    try:
        base_settings = sys.modules["src.honcho_rerank"].settings
        base_settings.RERANK = _FakeRerankSettings(DAILY_REQUEST_CAP=1000)

        async def completed(workspace_name, observer, observed):
            # A dream without a search leaves rerank resolution to the run logger.
            return types.SimpleNamespace(run_id="run-reload")

        orch.run_dream = completed
        policies.install()
        asyncio.run(orch.run_dream("ws", "observer", "observed"))
        # The wrapper's loader replaces RERANK when the settings file changes.
        base_settings.RERANK = _FakeRerankSettings(
            ENABLED=True, PROVIDER="jina", MODEL="reloaded-model", CANDIDATES=60,
            MIN_SCORE=0.5, DAILY_REQUEST_CAP=50,
        )
        asyncio.run(orch.run_dream("ws", "observer", "observed"))
        rows = [json.loads(line) for line in (tmp_path / "runs.log").read_text().splitlines()]
        assert rows[0]["rerank"]["candidates"] == 100
        assert rows[0]["rerank"]["daily_request_cap"] == 1000
        assert rows[1]["rerank"] == {
            "source": "default" if overrides else None,
            "enabled": True, "provider": "jina", "model": "reloaded-model",
            "candidates": 60, "min_score": 0.5, "daily_request_cap": 50,
            "base_url_set": False,
        }
    finally:
        _restore(saved)


def test_per_dream_notes_hold_for_the_bookkeeping_after_the_dream(tmp_path, monkeypatch):
    # The pair's notes value was seen inside run_dream but not in process_dream's bookkeeping,
    # which then cleared the stored cursor after every dream.
    data = sample_settings()
    data["pairs"]["observer/observed"]["notes"] = "on"
    saved, seen, dreamer, orch, path = _install_fakes(tmp_path, monkeypatch, data)
    try:
        asyncio.run(dreamer.process_dream(_Payload(observer="observer", observed="observed", dream_type="omni")))
        assert seen["process_notes"] == ["on"]
        assert orch.settings.DREAM.NOTES_MODE == "off"  # outside a dream the base value holds
    finally:
        _restore(saved)


def test_direct_transport_gets_no_bridge_fields_and_no_raw_effort():
    # A direct Anthropic dream received backend/observer/observed and a raw output_config effort ("minimal" unmapped).
    decision = policies.Decision(policy={}, source="default", mode="stock", backend="deepseek", backend_name="deepseek",
                                 gate="ran", observer="a", observed="b")
    direct = _Model("anthropic")
    direct.overrides.provider_params = {"extra_body": {"cache_control": {"type": "ephemeral"}, "output_config": {"effort": "high"}}}
    stamped = policies._stamp_model(direct, decision, {"deduction_effort": "minimal"}, "dream.deduction")
    assert stamped.thinking_effort == "minimal"
    assert stamped.overrides.provider_params["extra_body"] == {"cache_control": {"type": "ephemeral"}}
    bridged = policies._stamp_model(_Model("openai"), decision, {"deduction_effort": "low"}, "dream.deduction")
    extra = bridged.overrides.provider_params["extra_body"]
    assert (extra["backend"], extra["observer"], extra["observed"]) == ("deepseek", "a", "b")
    assert extra["output_config"] == {"effort": "low"}


def test_none_effort_is_stamped_for_the_bridge_and_left_off_a_direct_provider():
    # Honcho's anthropic transport drops none and its gemini transport would send an unlisted thinking_level.
    decision = policies.Decision(policy={}, source="default", mode="stock", backend="deepseek", backend_name="deepseek",
                                 gate="ran", observer="a", observed="b")
    two = {"deduction_effort": "low", "induction_effort": "high"}
    assert policies._stamp_model(_Model("openai"), decision, two, "dream.deduction").thinking_effort == "low"
    assert policies._stamp_model(_Model("openai"), decision, two, "dream.induction").thinking_effort == "high"
    # A peer card refresh keeps its job's effort: nothing per dream reaches it.
    card = policies._stamp_model(_Model("openai"), decision, two, "peer_card")
    assert "output_config" not in card.overrides.provider_params["extra_body"]
    bridged = policies._stamp_model(_Model("openai"), decision, {"deduction_effort": "none"}, "dream.deduction")
    assert bridged.thinking_effort == "none"
    assert bridged.overrides.provider_params["extra_body"]["output_config"] == {"effort": "none"}
    for transport in ("anthropic", "gemini"):
        stamped = policies._stamp_model(_Model(transport), decision, {"deduction_effort": "none"}, "dream.deduction")
        assert stamped.thinking_effort == "low", transport  # the model's own setting, untouched
        assert "output_config" not in stamped.overrides.provider_params["extra_body"], transport


def test_policy_file_accepts_none_effort(tmp_path):
    path = tmp_path / "settings.json"
    write_settings(path, {"mode": "stock", "defaults": {"deduction_effort": "none"}, "peers": {"p": {"induction_effort": "none"}}})
    store = policies.Policies(path, tmp_path / "policies.log")
    assert store.data["defaults"]["deduction_effort"] == "none"
