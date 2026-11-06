"""End to end: Honcho config and Models pages -> config.toml / settings.json -> Honcho 3.5.0 request -> bridge -> provider request.

Every layer is the real code: the dashboard (app.py, honcho_config.py and index.html driven through Playwright),
Honcho's own settings classes and LLM backends (honcho_child.py, in Honcho's interpreter), this repository's dream
hook, and the bridge (in-process). Only the ends are fakes: the provider HTTP endpoints (e2e_fakes.CaptureServer)
and the two CLIs (fake_cli.py). Nothing reaches a provider: the bridge's post_json is redirected to the capture
server, which records the URL the bridge meant to call.

Skipped unless all of these are available:
  - a Honcho v3.5.0 source tree: HONCHO_TREE, or a PYTHONPATH entry holding src/config.py
  - an interpreter with Honcho's dependencies: HONCHO_E2E_PYTHON, else HONCHO_RERANK_PYTHON, else this one
  - Playwright with Chromium, and fastapi + uvicorn for the dashboard, in the interpreter running pytest

Each case names the finding of the first end-to-end run it guards against (wrong effort field, unmapped level, a
config Honcho refuses to start with, bridge fields on a Honcho-native call).

The flow under test: config.toml is written for routing once, by "Route all jobs through the bridge" on the Honcho
config page (the job's old primary becomes its Honcho-native fallback); after that a model or per-job effort pick on
the Models page writes only settings.json, and the Honcho config page edits each job's fallback.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))


def _honcho_tree() -> Path | None:
    env = os.environ.get("HONCHO_TREE", "").strip()
    candidates = [Path(env)] if env else []
    candidates += [Path(p) for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    for item in candidates:
        if (item / "src" / "config.py").is_file() and (item / "src" / "llm" / "api.py").is_file():
            return item
    return None


HONCHO_TREE = _honcho_tree()
HONCHO_PYTHON = (os.environ.get("HONCHO_E2E_PYTHON") or os.environ.get("HONCHO_RERANK_PYTHON") or "").strip() or sys.executable

pytestmark = [
    pytest.mark.skipif(HONCHO_TREE is None, reason="no Honcho v3.5.0 tree (HONCHO_TREE or PYTHONPATH)"),
]

BRIDGE_KEY_ENV = "E2E_BRIDGE_HOP_KEY"
PROVIDER_KEYS = ["E2E_OPENROUTER_KEY", "E2E_DEEPSEEK_KEY", "E2E_GLM_KEY", "E2E_ANTHROPIC_KEY", "E2E_OPENAI_KEY",
                 "E2E_PROXY_KEY"]
HAIKU = "claude-haiku-5-5"   # an adaptive-thinking Claude id in Honcho 3.5.0's capability table


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sample_settings() -> dict:
    return {
        "mode": "stock",
        "defaults": {"brief": 50, "deduction_effort": "medium", "induction_effort": "medium", "deduction_rounds": 4, "induction_rounds": 4, "backend": None},
        "peers": {}, "pairs": {},
        "scheduler": {"poll_seconds": 60, "max_parallel": 1, "dream_type": "omni", "skip": []},
        "providers": {
            "active": "deepseek", "concurrency": 2, "timeout_seconds": 60,
            "backends": {
                "codex": {"kind": "cli-codex", "model": "codex-model", "effort": "medium"},
                "claude": {"kind": "cli-claude", "model": "claude-cli-model", "effort": "medium"},
                "openrouter": {"kind": "openai-compat", "base_url": "https://openrouter.ai/api/v1", "key_env": "E2E_OPENROUTER_KEY",
                               "model": "vendor/router-model", "models": ["vendor/router-model"]},
                "deepseek": {"kind": "openai-compat", "base_url": "https://api.deepseek.com", "key_env": "E2E_DEEPSEEK_KEY",
                             "model": "deepseek-flash", "thinking_effort": "low"},
                "glm": {"kind": "openai-compat", "base_url": "https://api.z.ai/api/paas/v4", "key_env": "E2E_GLM_KEY", "model": "glm-5.3"},
                "anthropic": {"kind": "anthropic-api", "key_env": "E2E_ANTHROPIC_KEY", "model": HAIKU},
                "openai": {"kind": "openai-compat", "base_url": "https://api.openai.com/v1", "key_env": "E2E_OPENAI_KEY", "model": "gpt-6-luna"},
                "proxy": {"kind": "openai-compat", "base_url": "http://proxy.invalid:4000/v1", "key_env": "E2E_PROXY_KEY", "model": "proxy-model"},
            },
            "stages": {"summary": "proxy:proxy-model", "dream.deduction": "deepseek:deepseek-flash",
                       "dream.induction": "deepseek:deepseek-flash", "peer_card": "deepseek:deepseek-flash"},
        },
    }


def sample_models() -> dict:
    dream = [("Anthropic", "anthropic", HAIKU), ("Anthropic", "claude", "claude-cli-model"), ("OpenAI", "codex", "codex-model"),
             ("OpenAI", "openai", "gpt-6-luna"), ("DeepSeek", "deepseek", "deepseek-flash"), ("GLM", "glm", "glm-5.3"),
             ("Router", "openrouter", "vendor/router-model"), ("Proxy", "proxy", "proxy-model")]
    return {"dream": [{"vendor": v, "label": m, "backend": b, "model": m} for v, b, m in dream],
            "tasks": {"anthropic": [{"label": "Haiku", "model": HAIKU}], "openai": [{"label": "small", "model": "text-embedding-3-small"}]}}


def sample_toml(bridge: str) -> str:
    # Intake and the dialectic levels are Honcho native (not routed). Summaries and both dream passes are routed as
    # the install step writes them: no thinking_effort on a routed primary, so the per-job effort in settings.json
    # applies. Dream induction carries an openai fallback with the fields only that transport accepts.
    native = f'transport = "anthropic"\nmodel = "{HAIKU}"\nthinking_effort = "medium"\n'
    levels = "".join(f"[dialectic.levels.{lvl}.model_config]\n{native}\n" for lvl in ("minimal", "low", "medium", "high", "max"))
    via = 'transport = "openai"\nmodel = "stage-route"\n'
    return (
        f"[deriver.model_config]\n{native}\n"
        f"[summary.model_config]\n{via}[summary.model_config.overrides]\napi_key_env = \"{BRIDGE_KEY_ENV}\"\nbase_url = \"{bridge}\"\n"
        "provider_params = { extra_body = { backend = \"@summary\" } }\n\n"
        f"{levels}"
        "[embedding.model_config]\ntransport = \"openai\"\nmodel = \"text-embedding-3-small\"\n\n"
        f"[dream.deduction_model_config]\n{via}structured_output_mode = \"json_object\"\n"
        f"[dream.deduction_model_config.overrides]\napi_key_env = \"{BRIDGE_KEY_ENV}\"\nbase_url = \"{bridge}\"\n"
        "provider_params = { extra_body = { backend = \"@dream.deduction\" } }\n\n"
        f"[dream.induction_model_config]\n{via}structured_output_mode = \"json_object\"\n"
        f"[dream.induction_model_config.overrides]\napi_key_env = \"{BRIDGE_KEY_ENV}\"\nbase_url = \"{bridge}\"\n"
        "provider_params = { extra_body = { backend = \"@dream.induction\" } }\n\n"
        "[dream.induction_model_config.fallback]\ntransport = \"openai\"\nmodel = \"gpt-6-luna\"\n"
        "structured_output_mode = \"json_object\"\n\n"
        "[dream.induction_model_config.fallback.overrides]\napi_key_env = \"E2E_OPENAI_KEY\"\nbase_url = \"https://api.openai.com/v1\"\n"
    )


# Row on the Models table: 0 Dreams fallback, then TASKS in index.html order.
ROW = {"deriver": 1, "summary": 2, "dialectic.minimal": 3, "dialectic.low": 4, "dialectic.medium": 5, "dialectic.high": 6,
       "dialectic.max": 7, "dream.deduction": 8, "dream.induction": 9, "peer_card": 10}
# Row on the Honcho config page's fallback table: one per job block, jobBlocks() order (peer card shares deduction).
FALLBACK_ROW = {"deriver": 0, "summary": 1, "dialectic.minimal": 2, "dialectic.low": 3, "dialectic.medium": 4,
                "dialectic.high": 5, "dialectic.max": 6, "dream.deduction": 7, "dream.induction": 8}
VENDOR = {b: v for v, b, _m in [("Anthropic", "anthropic", 0), ("Anthropic", "claude", 0), ("OpenAI", "codex", 0),
                                ("OpenAI", "openai", 0), ("DeepSeek", "deepseek", 0), ("GLM", "glm", 0),
                                ("Router", "openrouter", 0), ("Proxy", "proxy", 0)]}
MODEL = {b: m for _v, b, m in [("", "anthropic", HAIKU), ("", "claude", "claude-cli-model"), ("", "codex", "codex-model"),
                                ("", "openai", "gpt-6-luna"), ("", "deepseek", "deepseek-flash"), ("", "glm", "glm-5.3"),
                                ("", "openrouter", "vendor/router-model"), ("", "proxy", "proxy-model")]}


def bridge_pick(row: str, backend: str):
    return ["pick", ROW[row], VENDOR[backend], f"bridge:{backend}:{MODEL[backend]}"]


def fallback_pick(row: str):
    # The Honcho config page: that job's Honcho-native fallback, from the catalogue's task models by transport.
    return ["fallback_pick", FALLBACK_ROW[row], "anthropic", f"anthropic:{HAIKU}"]


ROUTE_ALL = ["route_all"]
UNDO_ROUTING = ["undo_routing"]

# id -> (steps, tasks, pre). pre edits the files before the page loads: drop_dream_stages removes the dream stage
# entries from settings.json; deriver_effort sets (None: removes) thinking_effort on the Honcho-native deriver.
CASES = {
    # Honcho native: the deriver is not routed, so Honcho calls Anthropic itself with the file's effort. The
    # dashboard offers no picker for it.
    "deriver-native-high": ([], ["deriver"], {"deriver_effort": "high"}),
    "deriver-native-blank": ([], ["deriver"], {"deriver_effort": None}),
    "deriver-native-minimal": ([], ["deriver"], {"deriver_effort": "minimal"}),
    # none in Honcho's own config: its Literal accepts it, the anthropic transport sends no effort and no thinking switch.
    "deriver-native-none": ([], ["deriver"], {"deriver_effort": "none"}),
    # The install step: every job that is not routed gets routed, its old primary becomes its fallback.
    "route-all": ([ROUTE_ALL], ["deriver", "dialectic.max"], None),
    "route-all-undo": ([ROUTE_ALL, ["write"], UNDO_ROUTING], ["deriver"], None),
    # The Honcho-native fallback on the Honcho config page: its effort, and a provider change on an openai fallback.
    "route-all-fallback-effort": ([ROUTE_ALL, ["write"], ["fallback_effort_job", FALLBACK_ROW["deriver"], "high"]], ["deriver"], None),
    # The fallback's effort list offers none on the openai transport only (the anthropic transport drops it, gemini lacks it).
    "induction-fallback-anthropic": ([["effort_labels", FALLBACK_ROW["dream.induction"]], fallback_pick("dream.induction"),
                                      ["effort_labels", FALLBACK_ROW["dream.induction"]],
                                      ["fallback_effort_job", FALLBACK_ROW["dream.induction"], "high"]],
                                     ["dream.induction"], None),
    "summary-blank-after-write": ([bridge_pick("summary", "proxy"), ["write"], ["effort", 2, ""]], ["summary"], None),
    "fallback-effort-openai": ([["fallback_pick", 0, "OpenAI", "openai:gpt-6-luna"], ["fallback_effort", "high"]], ["dream.deduction"],
                               {"drop_dream_stages": True}),
    "alias-then-hot": ([ROUTE_ALL, ["write"], bridge_pick("dialectic.max", "glm"), ["write"], bridge_pick("dialectic.max", "codex")],
                       ["dialectic.max"], None),
}
for _backend in MODEL:
    for _effort in ("medium", "max", "minimal", "none"):
        CASES[f"summary-{_backend}-{_effort}"] = ([bridge_pick("summary", _backend), ["effort", 2, _effort]], ["summary"], None)
CASES["deriver-deepseek"] = ([ROUTE_ALL, ["write"], bridge_pick("deriver", "deepseek")], ["deriver"], None)
CASES["dream-codex-xhigh"] = ([bridge_pick("dream.deduction", "codex"), ["defaults_effort", "xhigh"]], ["dream.deduction"], None)
# A question level set the way a live config sets Anthropic-only fields (effort in output_config, thinking off, prompt
# caching), called natively and after Route all: Anthropic must get the same request both ways.
CASES["minimal-live-native"] = ([], ["dialectic.minimal"], {"minimal_like_live": True})
CASES["minimal-live-routed"] = ([ROUTE_ALL, ["write"]], ["dialectic.minimal"], {"minimal_like_live": True})


# ---------------------------------------------------------------- the stack
class Stack:
    def __init__(self, tmp: Path):
        import bridge_cli  # noqa: F401 (path set below)
        from e2e_fakes import CaptureServer

        self.tmp = tmp
        self.capture = CaptureServer()
        self.bridge_port = free_port()
        self.bridge_url = f"http://127.0.0.1:{self.bridge_port}/v1"
        self.inbound, self.logs, self.state = [], [], {"case": None}
        self.bridge_cli = sys.modules["bridge_cli"]
        original = self.bridge_cli.post_json
        capture = self.capture

        def redirected(url, headers, payload, timeout):
            return original(capture.url + "/upstream" + urlparse(url).path, dict(headers, **{"X-E2E-Original-URL": url}), payload, timeout)

        self._original_post = original
        self.bridge_cli.post_json = redirected
        self.bridge = self.bridge_cli.Bridge(path=tmp / "none.json", log_file=tmp / "bridge-calls.jsonl")
        chat, write_log = self.bridge.chat, self.bridge.write_log

        def chat_wrap(body):
            self.inbound.append({"case": self.state["case"], "body": copy.deepcopy(body)})
            status, payload = chat(body)
            self.inbound[-1]["status"] = status
            return status, payload

        def log_wrap(record):
            self.logs.append({"case": self.state["case"], **record})
            write_log(record)

        self.bridge.chat, self.bridge.write_log = chat_wrap, log_wrap
        self.server = self.bridge_cli.serve(self.bridge, "127.0.0.1", self.bridge_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        stack = self

        class Control(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stack.state["case"] = body["case"]
                stack.capture.case = body["case"]
                stack.bridge.store = stack.bridge_cli.SettingsStore(Path(body["settings"]))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        threading.Thread(target=self.control.serve_forever, daemon=True).start()

    def close(self):
        self.bridge_cli.post_json = self._original_post
        self.server.shutdown()
        self.control.shutdown()
        self.capture.httpd.shutdown()


def restore(work: Path, base: Path, pre):
    (work / "honcho").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base / "settings.json", work / "settings.json")
    shutil.copyfile(base / "models.json", work / "models.json")
    shutil.copyfile(base / "config.toml", work / "honcho" / "config.toml")
    if pre and pre.get("drop_dream_stages"):
        data = json.loads((work / "settings.json").read_text(encoding="utf-8"))
        for stage in ("dream.deduction", "dream.induction", "peer_card"):
            data["providers"]["stages"].pop(stage, None)
        (work / "settings.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    if pre and pre.get("minimal_like_live"):
        toml_path = work / "honcho" / "config.toml"
        text = toml_path.read_text(encoding="utf-8")
        old = f'[dialectic.levels.minimal.model_config]\ntransport = "anthropic"\nmodel = "{HAIKU}"\nthinking_effort = "medium"\n'
        assert old in text
        new = (f'[dialectic.levels.minimal.model_config]\ntransport = "anthropic"\nmodel = "{HAIKU}"\n\n'
               "[dialectic.levels.minimal.model_config.overrides]\nprovider_params = { extra_body = { output_config = "
               '{ effort = "medium" }, thinking = { type = "disabled" }, cache_control = { type = "ephemeral" } } }\n')
        toml_path.write_text(text.replace(old, new, 1), encoding="utf-8")
    if pre and "deriver_effort" in pre:
        toml_path = work / "honcho" / "config.toml"
        text = toml_path.read_text(encoding="utf-8")
        head, _sep, tail = text.partition("[summary.model_config]")
        line = 'thinking_effort = "medium"\n'
        assert line in head
        wanted = "" if pre["deriver_effort"] is None else f'thinking_effort = "{pre["deriver_effort"]}"\n'
        toml_path.write_text(head.replace(line, wanted, 1) + _sep + tail, encoding="utf-8")


def drive_dashboard(tmp: Path, base: Path, out: Path):
    sync_api = pytest.importorskip("playwright.sync_api")
    pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    work = tmp / "work"
    restore(work, base, None)
    port = free_port()
    env = dict(os.environ)
    env.update({"CONTROL_FILE": str(work / "settings.json"), "HONCHO_CONFIG_FILE": str(work / "honcho" / "config.toml"),
                "HONCHO_CONFIG_BAK_DIR": str(tmp / "bak"), "DATA_DIR": str(tmp / "data"), "TELEMETRY_DIR": str(tmp / "telemetry"),
                "SCHEMA_FILE": str(ROOT / "src/config/settings.schema.json"), "EXAMPLE_FILE": str(ROOT / "src/config/settings.example.json"),
                "HONCHO_SCHEMA_FILE": str(ROOT / "src/config/honcho-config.schema.json"), "HONCHO_API_URL": "http://127.0.0.1:9",
                "CONTROL_PASSWORD": "", "POLICY_LOG": str(tmp / "logs" / "policy-used.log"),
                "POLICY_STATUS": str(tmp / "logs" / "hook-status.json"), "SCHEDULER_LOG": str(tmp / "logs" / "dream-nas.log"),
                "RESTART_LOG": str(tmp / "logs" / "restart.log")})
    for key in ("DOCKER_PROXY_URL", "DOCKER_PROXY_ACTION_URL"):
        env.pop(key, None)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--app-dir", str(ROOT / "src/dashboard/app"),
                             "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=2).read()
                break
            except Exception:
                time.sleep(0.2)
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                pytest.skip(f"Playwright Chromium is unavailable: {type(exc).__name__}")
            page = browser.new_page()
            page.on("dialog", lambda d: d.accept())
            for n, (cid, (steps, _tasks, pre)) in enumerate(CASES.items()):
                restore(work, base, pre)
                page.goto(f"http://127.0.0.1:{port}/?case={n}#models")
                page.wait_for_function("() => S.settings && S.honcho && document.querySelector('main table')")
                record = {"errors": []}
                for step in steps:
                    _do_step(page, step, record)
                record["write"] = _write(page)
                record["rows"] = page.evaluate("() => TASKS.map((t) => ({stage: t.stage, job: readJob(t.path, t.stage)}))")
                folder = out / cid
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(work / "settings.json", folder / "settings.json")
                shutil.copyfile(work / "honcho" / "config.toml", folder / "config.toml")
                (folder / "dash.json").write_text(json.dumps(record), encoding="utf-8")
            browser.close()
    finally:
        proc.terminate()


def _row(page, index):
    return page.locator("main table").first.locator("tbody > tr").nth(index)


def _show(page, name):
    """Open a page of the dashboard by its hash; the Models and Honcho config pages each start with a table."""
    if page.evaluate("() => S.page") != name:
        page.evaluate("(id) => { location.hash = '#' + id; }", name)
    page.wait_for_function("(id) => S.page === id && document.querySelector('main table')", arg=name)


def _pick(page, index, group, value):
    if _row(page, index).locator("select").nth(0).input_value() != group:
        _row(page, index).locator("select").nth(0).select_option(group)
    if _row(page, index).locator("select").nth(1).input_value() != value:
        _row(page, index).locator("select").nth(1).select_option(value)


def _write(page):
    page.evaluate("() => { S.notice = ''; }")
    page.locator("#review-btn").click()
    if page.locator("#write-btn").count() == 0:
        page.evaluate("() => closeModal()")
        return {"nothing": True}
    page.locator("#write-btn").click()
    page.wait_for_function("() => S.notice && S.notice.text", timeout=30000)
    result = page.evaluate("() => S.notice")
    page.wait_for_function("() => S.settings && S.honcho && document.querySelector('main table')")
    return result


def _do_step(page, step, record):
    op = step[0]
    if op == "pick":
        _show(page, "models")
        _pick(page, step[1], step[2], step[3])
    elif op == "effort":
        _show(page, "models")
        _row(page, step[1]).locator("select").nth(2).select_option(step[2])
        record["effort_labels"] = _row(page, step[1]).locator("select").nth(2).locator("option").all_text_contents()
    elif op == "effort_labels":
        # The labels the effort list on a Honcho config row offers.
        _show(page, "honcho")
        record.setdefault("labels", []).append(_row(page, step[1]).locator("select").nth(2).locator("option").all_text_contents())
    elif op == "fallback_pick":
        # The Dreams fallback row on Models (settings.json), or a job's Honcho-native fallback on Honcho config.
        if step[1] == 0:
            _show(page, "models")
        else:
            _show(page, "honcho")
        _pick(page, step[1], step[2], step[3])
    elif op == "fallback_effort":
        _show(page, "models")
        _row(page, 0).locator("select").nth(2).select_option(step[1])
    elif op == "fallback_effort_job":
        _show(page, "honcho")
        _row(page, step[1]).locator("select").nth(2).select_option(step[2])
    elif op == "route_all":
        _show(page, "honcho")
        page.get_by_role("button", name="Route all jobs through the bridge").click()
    elif op == "undo_routing":
        # Server-side: the job tables come back from the copy the install write took.
        _show(page, "honcho")
        page.evaluate("() => { S.notice = ''; }")
        page.get_by_role("button", name="Undo routing (jobs only)").click()
        page.get_by_role("button", name="Click again to undo").click()  # the button arms first
        page.wait_for_function("() => S.notice && S.notice.text", timeout=30000)
        assert "back as they were" in page.evaluate("() => S.notice.text")
    elif op == "defaults_effort":
        page.evaluate("(v) => { S.settings.defaults.effort = v; render(); }", step[1])
    elif op == "write":
        record.setdefault("mid", []).append(_write(page))


def run_honcho(stack: Stack, out: Path, tmp: Path) -> dict:
    jobs = [{"case": cid, "config_toml": str(out / cid / "config.toml"), "settings_json": str(out / cid / "settings.json"),
             "tasks": tasks, "hook": True} for cid, (_s, tasks, _p) in CASES.items()]
    (tmp / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    env = dict(os.environ)
    env.update({"LLM_ANTHROPIC_BASE_URL": stack.capture.url + "/anthropic-direct", "LLM_ANTHROPIC_API_KEY": "fake-direct",
                BRIDGE_KEY_ENV: "fake-hop", "LLM_OPENAI_API_KEY": "fake-openai", "PYTHONIOENCODING": "utf-8",
                "INSTRUCTIONS_DIR": str(tmp / "instructions"), "POLICY_LOG": str(tmp / "logs" / "policy-used.log"),
                "POLICY_STATUS": str(tmp / "logs" / "hook-status.json")})
    child = subprocess.run([HONCHO_PYTHON, str(HERE / "honcho_child.py"), str(tmp / "jobs.json"), str(tmp / "child.json"),
                            str(HONCHO_TREE), str(ROOT / "src" / "hooks"), f"http://127.0.0.1:{stack.control.server_address[1]}/case"],
                           env=env, cwd=str(tmp), capture_output=True, text=True, encoding="utf-8", timeout=600)
    if child.returncode != 0:
        if "ModuleNotFoundError" in child.stderr:
            pytest.skip("the Honcho interpreter lacks Honcho's dependencies (set HONCHO_E2E_PYTHON)")
        raise AssertionError(child.stderr[-3000:])
    return {r["case"]: r for r in json.loads((tmp / "child.json").read_text(encoding="utf-8"))}


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("routing-e2e")
    sys.path.insert(0, str(ROOT / "src" / "bridge"))
    saved = {name: os.environ.get(name) for name in [*PROVIDER_KEYS, "BRIDGE_EXE", "BRIDGE_CLAUDE", "BRIDGE_CWD", "E2E_CLI_LOG", "ANTHROPIC_API_URL"]}
    for name in PROVIDER_KEYS:
        os.environ[name] = "fake-" + name.lower()
    os.environ["BRIDGE_EXE"] = json.dumps([sys.executable, str(HERE / "fake_cli.py"), "codex"])
    os.environ["BRIDGE_CLAUDE"] = json.dumps([sys.executable, str(HERE / "fake_cli.py"), "claude"])
    os.environ["BRIDGE_CWD"] = str(tmp / "cwd")
    os.environ["E2E_CLI_LOG"] = str(tmp / "cli.jsonl")
    os.environ.pop("ANTHROPIC_API_URL", None)
    stack = Stack(tmp)
    try:
        base = tmp / "base"
        base.mkdir()
        (base / "settings.json").write_text(json.dumps(sample_settings(), indent=2), encoding="utf-8")
        (base / "models.json").write_text(json.dumps(sample_models(), indent=2), encoding="utf-8")
        (base / "config.toml").write_text(sample_toml(stack.bridge_url), encoding="utf-8")
        out = tmp / "cases"
        drive_dashboard(tmp, base, out)
        results = run_honcho(stack, out, tmp)
        cli = [json.loads(line) for line in (tmp / "cli.jsonl").read_text(encoding="utf-8").splitlines()] if (tmp / "cli.jsonl").exists() else []
        yield {"out": out, "results": results, "stack": stack, "cli": cli}
    finally:
        stack.close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def observed(e2e, cid, task):
    key = f"{cid}/{task}"
    stack = e2e["stack"]
    return {
        "dash": json.loads((e2e["out"] / cid / "dash.json").read_text(encoding="utf-8")),
        "toml": tomllib.loads((e2e["out"] / cid / "config.toml").read_text(encoding="utf-8")),
        "settings": json.loads((e2e["out"] / cid / "settings.json").read_text(encoding="utf-8")),
        "honcho": e2e["results"][cid],
        "inbound": [i for i in stack.inbound if i["case"] == key],
        "log": [r for r in stack.logs if r["case"] == key],
        "upstream": [r for r in stack.capture.records if r["case"] == key],
        "cli": [c for c in e2e["cli"] if c["case"] == key],
    }


def provider_effort(up):
    """The effort value in the field the provider documents."""
    body, url = up["body"], up["original_url"] or up["path"]
    if up["path"].endswith("/messages"):
        return (body.get("output_config") or {}).get("effort")
    if "openrouter.ai" in url:
        return (body.get("reasoning") or {}).get("effort")
    return body.get("reasoning_effort")


def cli_effort(argv):
    for i, arg in enumerate(argv):
        if arg == "--effort":
            return argv[i + 1]
        m = re.match(r'model_reasoning_effort="(.*)"', arg)
        if m:
            return m.group(1)
    return None


def single_call(o):
    assert o["honcho"].get("loaded"), o["honcho"].get("load_error")
    task = next(iter(o["honcho"]["tasks"].values()))
    assert task.get("ok"), task.get("error")
    return task


# ---------------------------------------------------------------- tests
EFFORT_SETS = {  # documented values
    # https://platform.claude.com/docs/en/build-with-claude/effort
    "anthropic": {"low", "medium", "high", "xhigh", "max"},
    # https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
    "openrouter": {"max", "xhigh", "high", "medium", "low", "minimal", "none"},
    # gpt-6-luna model page: none, low, medium, high, xhigh, max
    "openai": {"none", "low", "medium", "high", "xhigh", "max"},
    # https://api-docs.deepseek.com/api/create-chat-completion (minimal, medium, xhigh are remapped by DeepSeek)
    "deepseek": {"none", "low", "high", "max"},
    # https://developers.openai.com/codex/config-reference (levels depend on the model)
    "codex": {"low", "medium", "high", "xhigh", "max"},
    # https://code.claude.com/docs/en/cli-reference --effort
    "claude": {"low", "medium", "high", "xhigh", "max"},
    # https://docs.z.ai/api-reference/llm/chat-completion: GLM-5.3 supports low, high, max
    "glm": {"low", "high", "max"},
}
# The level each provider receives for the level asked: the nearest documented one.
NEAREST = {
    "anthropic": {"minimal": "low"}, "claude": {"minimal": "low"}, "codex": {"minimal": "low"},
    "openai": {"minimal": "low"}, "deepseek": {"minimal": "low", "medium": "high"},
    "glm": {"minimal": "low", "medium": "high"},
}
SUMMARY_CASES = [pytest.param(b, e, id=f"{b}-{e}") for b in MODEL for e in ("medium", "max", "minimal", "none")]


def assert_thinking_off(backend, body):
    """none is each provider's own off switch, with no effort level next to it."""
    if backend == "anthropic":  # https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting
        assert body["thinking"] == {"type": "disabled"} and "output_config" not in body
    elif backend == "deepseek":  # https://api-docs.deepseek.com/api/create-chat-completion
        assert body["reasoning_effort"] == "none" and "thinking" not in body
    elif backend == "glm":  # glm-5.3: https://docs.z.ai/api-reference/llm/chat-completion says thinking can only be enabled
        assert "thinking" not in body and "reasoning_effort" not in body
    elif backend == "openrouter":  # https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
        assert body["reasoning"] == {"effort": "none"} and "reasoning_effort" not in body
    else:  # openai (https://developers.openai.com/api/docs/guides/reasoning) and an unknown host
        assert body["reasoning_effort"] == "none" and "thinking" not in body


@pytest.mark.parametrize("backend,effort", SUMMARY_CASES)
def test_summary_model_and_effort_reach_the_provider(e2e, backend, effort):
    # A routed job: the model and the per-job effort are the stage entry in settings.json; config.toml is untouched.
    cid = f"summary-{backend}-{effort}"
    o = observed(e2e, cid, "summary")
    assert o["dash"]["write"].get("bad") is None, o["dash"]["write"]
    assert o["dash"]["write"]["text"].startswith("Written: dream settings.") and "Honcho config" not in o["dash"]["write"]["text"]
    assert o["settings"]["providers"]["stages"]["summary"] == {"model": f"{backend}:{MODEL[backend]}", "effort": effort}
    assert "thinking_effort" not in o["toml"]["summary"]["model_config"]
    single_call(o)
    assert o["inbound"] and "reasoning_effort" not in o["inbound"][0]["body"]
    assert o["log"][0]["asked_effort"] is None and o["log"][0]["effort"] == effort
    assert o["log"][0]["backend"] == backend and o["log"][0]["resolved_model"] == MODEL[backend]
    if backend in ("codex", "claude"):
        argv = o["cli"][0]["argv"]
        assert MODEL[backend] in argv
        got = cli_effort(argv)
        assert not [n for n in o["cli"][0]["env_names"] if n in PROVIDER_KEYS], "provider keys reached the CLI"
        if effort == "none":
            assert got == "low"  # the command line has no off level
            return
    else:
        up = o["upstream"][0]
        assert up["body"]["model"] == MODEL[backend]
        if effort == "none":
            assert_thinking_off(backend, up["body"])
            assert "none (thinking off)" in o["dash"]["effort_labels"]
            return
        got = provider_effort(up)
    if backend == "proxy":
        assert got == effort          # no documented value list: pass-through is what we can check
    else:
        assert got == NEAREST.get(backend, {}).get(effort, effort), (got, effort)
        assert got in EFFORT_SETS[backend], (got, effort)


def test_native_effort_high_reaches_anthropic(e2e):
    # Honcho calls Anthropic itself for a job that is not routed; the file's effort goes out in the documented field.
    o = observed(e2e, "deriver-native-high", "deriver")
    assert o["dash"]["write"] == {"nothing": True}
    assert next(r for r in o["dash"]["rows"] if r["stage"] == "deriver")["job"] == ""
    single_call(o)
    assert not o["inbound"], "a Honcho-native call reached the bridge"
    body = o["upstream"][0]["body"]
    assert body["output_config"] == {"effort": "high"} and body["thinking"] == {"type": "adaptive"}


def test_native_blank_effort_sends_no_effort(e2e):
    o = observed(e2e, "deriver-native-blank", "deriver")
    single_call(o)
    assert "output_config" not in o["upstream"][0]["body"]


def test_native_none_is_accepted_by_honcho_and_sends_no_effort(e2e):
    o = observed(e2e, "deriver-native-none", "deriver")
    single_call(o)
    body = o["upstream"][0]["body"]
    assert "output_config" not in body and "thinking" not in body


def test_fallback_effort_offers_none_on_the_openai_transport_only(e2e):
    o = observed(e2e, "induction-fallback-anthropic", "dream.induction")
    openai_labels, anthropic_labels = o["dash"]["labels"]
    assert "none (thinking off)" in openai_labels and "none (thinking off)" not in anthropic_labels


def test_native_minimal_is_mapped_for_anthropic(e2e):
    o = observed(e2e, "deriver-native-minimal", "deriver")
    single_call(o)
    assert o["upstream"][0]["body"]["output_config"]["effort"] in EFFORT_SETS["anthropic"]


def test_route_all_moves_the_primary_into_the_fallback(e2e):
    # The install step: the deriver and every dialectic level get routed through the summary's bridge, their old
    # primaries become their fallbacks, each gets a stage entry, and the routed blocks and embedding stay as they were.
    o = observed(e2e, "route-all", "deriver")
    assert o["dash"]["write"].get("bad") is None, o["dash"]["write"]
    assert "Honcho config" in o["dash"]["write"]["text"]
    bridge = e2e["stack"].bridge_url
    for block in [o["toml"]["deriver"]["model_config"]] + [o["toml"]["dialectic"]["levels"][lvl]["model_config"] for lvl in ("minimal", "low", "medium", "high", "max")]:
        assert block["transport"] == "openai" and block["model"] == "stage-route" and "thinking_effort" not in block
        assert block["overrides"]["base_url"] == bridge and block["overrides"]["api_key_env"] == BRIDGE_KEY_ENV
        assert "structured_output_mode" not in block
        assert block["fallback"] == {"model": HAIKU, "transport": "anthropic", "thinking_effort": "medium"}
    assert o["toml"]["deriver"]["model_config"]["overrides"]["provider_params"]["extra_body"]["backend"] == "@deriver"
    assert o["toml"]["dialectic"]["levels"]["max"]["model_config"]["overrides"]["provider_params"]["extra_body"]["backend"] == "@dialectic.max"
    assert o["toml"]["summary"]["model_config"] == tomllib.loads(sample_toml(bridge))["summary"]["model_config"]
    assert o["toml"]["embedding"]["model_config"] == {"transport": "openai", "model": "text-embedding-3-small"}
    stages = o["settings"]["providers"]["stages"]
    # Each routed job keeps its model and its effort (medium) in its stage.
    assert stages["deriver"] == {"model": f"anthropic:{HAIKU}", "effort": "medium"}
    assert stages["dialectic.max"] == {"model": f"anthropic:{HAIKU}", "effort": "medium"}
    assert stages["summary"] == "proxy:proxy-model" and "peer_card" in stages
    assert next(r for r in o["dash"]["rows"] if r["stage"] == "deriver")["job"] == f"bridge:anthropic:{HAIKU}"
    task = single_call(o)
    assert task["config"]["model"] == "stage-route"
    assert o["inbound"] and o["inbound"][0]["body"]["backend"] == "@deriver"
    assert o["log"][0]["backend"] == "anthropic" and o["log"][0]["resolved_model"] == HAIKU
    assert o["upstream"][0]["path"].endswith("/messages") and o["upstream"][0]["body"]["model"] == HAIKU
    level = observed(e2e, "route-all", "dialectic.max")
    assert level["inbound"][0]["body"]["backend"] == "@dialectic.max" and level["log"][0]["backend"] == "anthropic"


def test_route_all_then_undo_restores_the_file(e2e):
    # Undo after the install Write puts every job table back from the copy taken before Route all.
    o = observed(e2e, "route-all-undo", "deriver")
    bridge = e2e["stack"].bridge_url
    assert "Honcho config" in o["dash"]["mid"][0]["text"]
    assert o["toml"] == tomllib.loads(sample_toml(bridge))
    assert "Honcho config" not in o["dash"]["write"].get("text", "")
    single_call(o)
    assert not o["inbound"] and o["upstream"][0]["path"].endswith("/messages")
    assert o["upstream"][0]["body"]["output_config"] == {"effort": "medium"}


def test_fallback_effort_is_written_to_the_fallback_only(e2e):
    # After the install the Honcho config page edits the deriver's Honcho-native fallback: its effort lands in the
    # fallback table, the routed primary keeps no effort of its own, and the routed call keeps the job's original
    # effort (medium, from its stage), not the fallback's new one.
    o = observed(e2e, "route-all-fallback-effort", "deriver")
    assert o["dash"]["mid"][0].get("bad") is None and "Honcho config" in o["dash"]["write"]["text"]
    block = o["toml"]["deriver"]["model_config"]
    assert block["fallback"] == {"model": HAIKU, "transport": "anthropic", "thinking_effort": "high"}
    assert block["model"] == "stage-route" and "thinking_effort" not in block
    single_call(o)
    assert o["upstream"][0]["body"]["output_config"]["effort"] == "medium"


def test_fallback_provider_change_leaves_a_config_honcho_starts_with(e2e):
    # An openai fallback switched to an Anthropic model on the Honcho config page: structured_output_mode (refused on
    # that transport) and the old provider's overrides are gone, and Honcho loads the file.
    o = observed(e2e, "induction-fallback-anthropic", "dream.induction")
    assert o["dash"]["write"].get("bad") is None, o["dash"]["write"]
    assert o["toml"]["dream"]["induction_model_config"]["fallback"] == {"model": HAIKU, "transport": "anthropic", "thinking_effort": "high"}
    assert o["toml"]["dream"]["induction_model_config"]["structured_output_mode"] == "json_object"
    assert o["honcho"].get("loaded"), o["honcho"].get("load_error")
    single_call(o)


def test_blank_effort_on_bridge_row_sends_no_effort(e2e):
    o = observed(e2e, "summary-blank-after-write", "summary")
    assert o["settings"]["providers"]["stages"]["summary"] == "proxy:proxy-model"
    single_call(o)
    assert o["log"][0]["asked_effort"] is None and o["log"][0]["effort"] == "" and o["log"][0]["sent_effort"] is None


def test_fallback_effort_keeps_the_openai_wire_shape(e2e):
    o = observed(e2e, "fallback-effort-openai", "dream.deduction")
    single_call(o)
    body = o["upstream"][0]["body"]
    assert "thinking" not in body and body.get("reasoning_effort")


def test_openai_reasoning_model_gets_max_completion_tokens(e2e):
    o = observed(e2e, "summary-openai-medium", "summary")
    single_call(o)
    body = o["upstream"][0]["body"]
    assert "max_tokens" not in body and "max_completion_tokens" in body


def test_intake_to_deepseek_uses_json_object(e2e):
    o = observed(e2e, "deriver-deepseek", "deriver")
    assert o["dash"]["mid"][0].get("bad") is None
    assert o["dash"]["write"]["text"].startswith("Written: dream settings.")
    stage = o["settings"]["providers"]["stages"]["deriver"]
    assert (stage["model"] if isinstance(stage, dict) else stage) == "deepseek:deepseek-flash"
    single_call(o)
    body = o["upstream"][0]["body"]
    assert body["response_format"]["type"] in ("json_object", "text")
    system = next(m["content"] for m in body["messages"] if m["role"] == "system")
    assert "json" in system and "JSON schema" in system


def test_install_then_provider_change_is_settings_only(e2e):
    o = observed(e2e, "alias-then-hot", "dialectic.max")
    assert o["dash"]["mid"][0].get("bad") is None and o["dash"]["mid"][1].get("bad") is None
    assert "Honcho config" in o["dash"]["mid"][0]["text"] and "Honcho config" not in o["dash"]["mid"][1]["text"]
    assert o["dash"]["write"]["text"].startswith("Written: dream settings.")
    assert o["toml"]["dialectic"]["levels"]["max"]["model_config"]["overrides"]["provider_params"]["extra_body"]["backend"] == "@dialectic.max"
    single_call(o)
    assert o["log"][0]["backend"] == "codex" and o["cli"][0]["argv"][o["cli"][0]["argv"].index("-m") + 1] == "codex-model"


def test_dream_effort_comes_from_the_dream_settings(e2e):
    o = observed(e2e, "dream-codex-xhigh", "dream.deduction")
    task = single_call(o)
    assert task["hooked"]["thinking_effort"] == "xhigh"
    assert o["inbound"][0]["body"]["backend"] == "@dream.deduction"
    assert cli_effort(o["cli"][0]["argv"]) == "xhigh"


def test_routed_anthropic_job_reaches_anthropic_as_the_native_one_does(e2e):
    # Prompt caching, thinking switched off and the effort survive the bridge: the request Anthropic gets after Route
    # all carries the same fields as the one Honcho sent itself before.
    native = observed(e2e, "minimal-live-native", "dialectic.minimal")
    routed = observed(e2e, "minimal-live-routed", "dialectic.minimal")
    single_call(native)
    single_call(routed)
    assert not native["inbound"] and routed["inbound"], "the routed call did not go through the bridge"
    before, after = native["upstream"][0]["body"], routed["upstream"][0]["body"]
    for key in ("cache_control", "thinking", "model"):
        assert after.get(key) == before.get(key), (key, before.get(key), after.get(key))
    assert (after.get("output_config") or {}).get("effort") == (before.get("output_config") or {}).get("effort") == "medium"
    assert after["system"][0].get("cache_control") == before["system"][0].get("cache_control") == {"type": "ephemeral"}


def test_routed_deriver_thinks_as_the_native_one_does(e2e):
    # The native deriver with an effort runs with adaptive thinking; routed, it keeps it.
    native = observed(e2e, "deriver-native-high", "deriver")
    routed = observed(e2e, "route-all", "deriver")
    single_call(native)
    single_call(routed)
    assert native["upstream"][0]["body"]["thinking"] == routed["upstream"][0]["body"]["thinking"] == {"type": "adaptive"}
