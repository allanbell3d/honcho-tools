"""Scheduler tests. Pure functions and a temp settings file. No database."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scheduler"))

import dream_nas  # noqa: E402
from support import sample_settings  # noqa: E402


def test_import_does_not_load_drivers():
    # A fresh interpreter: other test modules (the dashboard's TestClient) import httpx.
    import subprocess

    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import dream_nas; "
        "assert 'httpx' not in sys.modules; assert 'psycopg' not in sys.modules"
    )
    scheduler_dir = str(Path(__file__).resolve().parents[1] / "src" / "scheduler")
    subprocess.run([sys.executable, "-c", code, scheduler_dir], check=True)


def test_eligible_is_pairs_plus_diagonal():
    data = sample_settings()
    data["peers"]["solo"] = {"enabled": True}
    data["scheduler"]["skip"] = ["observer/observed"]
    found = set(dream_nas.eligible(data))
    assert ("observer", "observer") in found
    assert ("observed", "observed") in found
    assert ("solo", "solo") in found
    assert ("observer", "observed") not in found
    assert ("observer", "off") not in found
    assert ("off", "off") not in found
    assert ("observer", "solo") not in found
    assert ("missing", "missing") not in found


def test_discover_appends_disabled_and_drops_secret_names(tmp_path):
    data = {"peers": {"kept": {"enabled": True}}, "pairs": {}}
    added, skipped = dream_nas.discover(data, ["kept", "new-peer", "bad name", "a/b", "sk-secret", "Bearer token"])
    assert added == ["new-peer"]
    assert skipped == 4
    assert data["peers"]["new-peer"] == {"enabled": False}
    assert "_discover_skipped" not in data
    path = tmp_path / "settings.json"
    dream_nas.atomic_write(path, data)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "_discover_skipped" not in saved
    assert "sk-" not in path.read_text(encoding="utf-8")
    assert "Bearer" not in path.read_text(encoding="utf-8")


def test_ordinary_words_are_not_taken_for_keys():
    # "task-" or "risk-" contain "sk-"; only a key-shaped start is refused.
    data = {"peers": {}, "pairs": {}}
    added, skipped = dream_nas.discover(data, ["task-runner", "risk-model", "sk-live", "AIzaKey"])
    assert added == ["risk-model", "task-runner"]
    assert skipped == 2


def test_bak_rotation_keeps_twenty(tmp_path):
    path = tmp_path / "settings.json"
    for number in range(21):
        dream_nas.atomic_write(path, {"n": number})
    baks = sorted((path.parent / "backups" / "settings").glob("settings.json.bak-*"), key=lambda item: item.name)
    assert len(baks) == 20
    assert json.loads(baks[0].read_text(encoding="utf-8"))["n"] == 0
    assert json.loads(baks[-1].read_text(encoding="utf-8"))["n"] == 19
    assert json.loads(path.read_text(encoding="utf-8"))["n"] == 20
    assert list(path.parent.glob("settings.json.bak-*")) == []
    dream_nas.atomic_write(path, {"n": 21})
    baks = sorted((path.parent / "backups" / "settings").glob("settings.json.bak-*"), key=lambda item: item.name)
    assert len(baks) == 20
    assert json.loads(baks[0].read_text(encoding="utf-8"))["n"] == 1
    assert json.loads(path.read_text(encoding="utf-8"))["n"] == 21


def test_schedule_pass_sends_mark_and_stock_sleeps():
    data = sample_settings()
    data["mode"] = "stock"
    data["scheduler"]["skip"] = []
    data["pairs"] = {}
    assert dream_nas.scheduling_on(data) is False
    unread = [("observer", "observer", 3, "2020-01-01")]
    assert dream_nas.schedule_pass(data, unread, set(), lambda payload: (200, "ok")) == (0, [])

    data["mode"] = "scheduler"
    seen = []

    def post(payload):
        seen.append(payload)
        return 200, "ok"

    count, messages = dream_nas.schedule_pass(data, unread, set(), post)
    assert count == 1
    assert seen[0]["scheduled_by"] == "honcho-scheduler"
    assert seen[0]["observer"] == "observer"
    assert seen[0]["dream_type"] == "omni"
    assert messages == ["scheduled observer/observer (3 unread)"]
    data["scheduler"]["max_parallel"] = 1
    older = []

    def post_one(payload):
        older.append(payload)
        return 200, "ok"

    count, _messages = dream_nas.schedule_pass(
        data,
        [("observer", "observer", 2, "2020-01-02"), ("observed", "observed", 4, "2020-01-01")],
        set(),
        post_one,
    )
    assert count == 1
    assert older[0]["observed"] == "observed"
    failed = []

    def reject(payload):
        failed.append(payload)
        return 422, "no"

    count, messages = dream_nas.schedule_pass(data, unread, set(), reject)
    assert count == 0
    assert messages[0].startswith("schedule observer/observer -> 422 ")
    assert dream_nas.format_line(messages[0], "scheduler").endswith("mode=scheduler")


def test_a_dream_that_changes_nothing_is_not_queued_at_every_poll():
    # A failed dream or a card refresh leaves the cursor, so the unread rows stay the same.
    data = sample_settings()
    data["mode"] = "scheduler"
    data["scheduler"]["skip"] = []
    data["pairs"] = {}
    unread = [("observer", "observer", 3, "2020-01-01")]
    seen = []

    def post(payload):
        seen.append(payload)
        return 200, "ok"

    holds = dream_nas.Holds(first=900, longest=3600)
    assert dream_nas.schedule_pass(data, unread, set(), post, holds, now=0)[0] == 1
    assert dream_nas.schedule_pass(data, unread, set(), post, holds, now=15)[0] == 0
    assert dream_nas.schedule_pass(data, unread, set(), post, holds, now=899)[0] == 0
    count, messages = dream_nas.schedule_pass(data, unread, set(), post, holds, now=900)
    assert count == 1 and "unchanged by the last dream" in messages[0]
    assert dream_nas.schedule_pass(data, unread, set(), post, holds, now=900 + 1799)[0] == 0
    assert dream_nas.schedule_pass(data, unread, set(), post, holds, now=900 + 1800)[0] == 1
    assert holds.queued[("observer", "observer")][1] == 3600
    # Progress (fewer unread) or new conclusions release the pair at once.
    moved = [("observer", "observer", 2, "2020-01-02")]
    assert dream_nas.schedule_pass(data, moved, set(), post, holds, now=2701)[0] == 1
    assert len(seen) == 4


def test_bad_providers_still_applies_mode(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "mode": "scheduler",
        "peers": {"a": {"enabled": True}},
        "providers": {"token": "sk-should-not-block"},
        "rerank": "not-an-object",
    }), encoding="utf-8")
    store = dream_nas.SettingsStore(path)
    loaded = store.reload()
    assert loaded is not None
    assert loaded["mode"] == "scheduler"
    assert loaded["peers"]["a"]["enabled"] is True


def test_settings_store_reloads_on_mtime(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mode": "stock", "peers": {}}) + "\n", encoding="utf-8")
    store = dream_nas.SettingsStore(path)
    assert store.reload()["mode"] == "stock"
    path.write_text(json.dumps({"mode": "scheduler", "peers": {}}) + "\n", encoding="utf-8")
    os_mtime = path.stat().st_mtime_ns + 1_000_000_000
    import os
    os.utime(path, ns=(os_mtime, os_mtime))
    assert store.reload()["mode"] == "scheduler"
    path.write_text("{", encoding="utf-8")
    os.utime(path, ns=(os_mtime + 1_000_000_000, os_mtime + 1_000_000_000))
    assert store.reload()["mode"] == "scheduler"
    assert store.rejected.startswith("JSONDecodeError")
    # A list where an object belongs crashed the poll loop; the file is refused instead, with the reason.
    path.write_text(json.dumps({"mode": "stock", "pairs": ["a/b"]}), encoding="utf-8")
    os.utime(path, ns=(os_mtime + 2_000_000_000, os_mtime + 2_000_000_000))
    assert store.reload()["mode"] == "scheduler"
    assert store.rejected == "ValueError: pairs must be an object"
    path.write_text(json.dumps({"mode": "stock", "peers": {}}), encoding="utf-8")
    os.utime(path, ns=(os_mtime + 3_000_000_000, os_mtime + 3_000_000_000))
    assert store.reload()["mode"] == "stock" and store.rejected is None


def test_discovery_does_not_rewrite_when_nothing_is_new(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"peers": {"kept": {"enabled": True}}}) + "\n", encoding="utf-8")
    store = dream_nas.SettingsStore(path)
    store.reload()
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    assert dream_nas.commit_discovery(store, ["kept"]) == ([], 0)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    assert list(path.parent.glob("settings.json.bak-*")) == []


def test_discovery_keeps_a_newer_dashboard_save(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mode": "stock", "peers": {}}) + "\n", encoding="utf-8")
    store = dream_nas.SettingsStore(path)
    store.reload()
    path.write_text(json.dumps({"mode": "scheduler", "peers": {}, "scheduler": {"max_parallel": 4}}) + "\n", encoding="utf-8")
    stamp = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    result = dream_nas.commit_discovery(store, ["new-peer"])
    assert result is not None
    added, skipped = result
    assert added == ["new-peer"]
    assert skipped == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["mode"] == "scheduler"
    assert saved["scheduler"]["max_parallel"] == 4
    assert saved["peers"]["new-peer"] == {"enabled": False}


def test_change_between_check_and_replace_is_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mode": "stock", "peers": {}}) + "\n", encoding="utf-8")
    store = dream_nas.SettingsStore(path)
    store.reload()
    intruder = json.dumps({"mode": "scheduler", "marker": "kept"}) + "\n"
    original = Path.write_text

    def plant(self, text, *args, **kwargs):
        result = original(self, text, *args, **kwargs)
        if self.name.startswith("settings.json.tmp-"):
            original(path, intruder, encoding="utf-8")
            stamp = path.stat().st_mtime_ns + 1_000_000_000
            os.utime(path, ns=(stamp, stamp))
        return result

    monkeypatch.setattr(Path, "write_text", plant)
    assert dream_nas.commit_discovery(store, ["new-peer"]) is None
    assert path.read_text(encoding="utf-8") == intruder
    assert "new-peer" not in (store.data.get("peers") or {})
    assert store.mtime != path.stat().st_mtime_ns


def test_discovery_does_not_disable_a_peer_the_dashboard_just_added(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mode": "stock", "peers": {}}) + "\n", encoding="utf-8")
    store = dream_nas.SettingsStore(path)
    store.reload()
    dashboard = {"mode": "scheduler", "peers": {"new-peer": {"enabled": True}}, "marker": 7}
    path.write_text(json.dumps(dashboard) + "\n", encoding="utf-8")
    stamp = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    before = path.read_bytes()
    assert dream_nas.commit_discovery(store, ["new-peer"]) == ([], 0)
    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8"))["peers"]["new-peer"]["enabled"] is True


def test_config_line_lists_skip_and_env_is_required(monkeypatch):
    text = dream_nas.describe_change({
        "scheduler": {"skip": ["b/a", "a/b"], "max_parallel": 2, "dream_type": "omni"},
        "pairs": {"a/b": {}},
    })
    assert "skip ['a/b', 'b/a']" in text
    assert "max_parallel 2" in text
    for name in ("PGHOST", "PGUSER", "PGDATABASE", "POSTGRES_PASSWORD", "HONCHO_API", "HONCHO_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret-value")
    try:
        dream_nas.database_kwargs()
        raise AssertionError("missing database env was accepted")
    except RuntimeError as exc:
        assert "PGHOST" in str(exc)
        assert "secret-value" not in str(exc)
    try:
        dream_nas.api_base()
        raise AssertionError("missing API url was accepted")
    except RuntimeError as exc:
        assert "HONCHO_API_URL" in str(exc)
    monkeypatch.setenv("HONCHO_API_URL", "http://example.invalid:8000")
    assert dream_nas.api_base() == "http://example.invalid:8000/v3"
    monkeypatch.setenv("HONCHO_API", "http://example.invalid/v3")
    assert dream_nas.api_base() == "http://example.invalid/v3"



def test_stock_main_does_not_discover_peers(tmp_path, monkeypatch):
    data = sample_settings()
    data["mode"] = "stock"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CONTROL_FILE", str(path))
    monkeypatch.setenv("HONCHO_WORKSPACE", "fixture")
    monkeypatch.setenv("SCHEDULER_LOG", str(tmp_path / "scheduler.log"))
    handlers = {}
    monkeypatch.setattr(dream_nas.signal, "signal", lambda signal, handler: handlers.update({signal: handler}))
    monkeypatch.setattr(dream_nas, "_sleep", lambda seconds, halted: next(iter(handlers.values()))())
    seen = []
    class Database:
        def peer_names(self, workspace):
            seen.append(workspace)
            return []
    monkeypatch.setattr(dream_nas, "Database", Database)
    dream_nas.main()
    assert seen == []


def test_scheduler_log_rotates_at_its_size_limit(tmp_path):
    log = tmp_path / "dream-nas.log"
    log.write_text("x" * 100, encoding="utf-8")
    dream_nas.rotate_log(log, 50)
    assert not log.exists() and (tmp_path / "dream-nas.log.1").read_text(encoding="utf-8") == "x" * 100
    log.write_text("y", encoding="utf-8")
    dream_nas.rotate_log(log, 50)
    assert log.read_text(encoding="utf-8") == "y"


def test_runtime_env_keeps_only_what_the_scheduler_reads(monkeypatch):
    honcho = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "LC_ALL": "C",
        "PYTHONUNBUFFERED": "1",
        "https_proxy": "http://proxy.example:3128",
        "AUTH_JWT_SECRET": "x",
        "DB_CONNECTION_URI": "x",
        "CACHE_URL": "x",
        "DEEPSEEK_API_KEY": "x",
        "RERANK_API_KEY": "x",
        "TELEMETRY_HEADERS": "x",
    }
    mine = {
        "CONTROL_FILE": "/config/settings.json", "HONCHO_API_URL": "http://api:8000", "HONCHO_WORKSPACE": "w",
        "HONCHO_API_TOKEN": "t", "POSTGRES_PASSWORD": "p", "PGHOST": "database", "PGSSLMODE": "disable", "TZ": "UTC",
    }
    kept = dream_nas.runtime_env({**honcho, **mine})
    assert kept == {
        "PATH": "/usr/bin", "HOME": "/root", "LC_ALL": "C", "PYTHONUNBUFFERED": "1",
        "https_proxy": "http://proxy.example:3128", **mine,
    }
    calls = []
    monkeypatch.setattr(dream_nas.os, "execve", lambda *args: calls.append(args))
    monkeypatch.setattr(dream_nas.os, "environ", {**honcho, **mine})
    dream_nas.drop_unused_env()
    assert len(calls) == 1 and calls[0][2] == kept
    calls.clear()
    monkeypatch.setattr(dream_nas.os, "environ", dict(kept))
    dream_nas.drop_unused_env()
    assert calls == []


def test_failing_connection_is_closed():
    class Conn:
        def __init__(self, fail_close=False):
            self.calls = 0
            self.fail_close = fail_close

        def close(self):
            self.calls += 1
            if self.fail_close:
                raise OSError("close failed")

        def execute(self, sql, params):
            raise RuntimeError("boom")

    db = dream_nas.Database()
    conn = Conn()
    db._conn = conn
    try:
        db._rows("select 1", ())
    except RuntimeError as exc:
        assert type(exc.__cause__).__name__ == "NoneType" or exc.__cause__ is None
        assert "RuntimeError" in str(exc)
        assert "boom" not in str(exc)
        assert "close failed" not in str(exc)
    else:
        raise AssertionError("the query error was swallowed")
    assert conn.calls == 1
    assert db._conn is None

    loud = Conn(fail_close=True)
    db._conn = loud
    try:
        db._rows("select 1", ())
    except RuntimeError as exc:
        assert "RuntimeError" in str(exc)
        assert "close failed" not in str(exc)
        assert "boom" not in str(exc)
    else:
        raise AssertionError("a failing close replaced the query error")
    assert loud.calls == 1
    assert db._conn is None


def test_discover_adds_notebooks_as_pairs_that_start_off():
    data = {"peers": {"A": {"enabled": True}}, "pairs": {"A/A": {"brief": 9}}, "scheduler": {"skip": []}}
    pairs = [("A", "A"), ("A", "B"), ("B", "B"), ("B", "sk-secret")]
    added, skipped = dream_nas.discover(data, ["A", "B", "sk-secret"], pairs)
    assert added == ["B", "A/B", "B/B"]
    assert skipped == 2
    assert data["pairs"] == {"A/A": {"brief": 9}, "A/B": {}, "B/B": {}}
    assert data["scheduler"]["skip"] == ["A/B", "B/B"]
    assert data["peers"]["B"] == {"enabled": False}
    assert dream_nas.discover(data, ["A", "B"], pairs) == ([], 1)
